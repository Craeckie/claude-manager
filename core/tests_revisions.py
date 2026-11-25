"""Safe writes to long idea fields (#704): append/prepend, commits_add, field history
with restore, the stale-write check, and get_idea(fields=, history=)."""

import json

from django.test import TestCase

from . import activity, services
from .models import FieldRevision, Idea, Project, REVISION_KEEP

MCP_URL = "/mcp/"
# Stored text never ends in whitespace: the form strips every field on save.
LONG = "\n".join(f"- 2025-01-15 line {i}: progress noted, nothing else" for i in range(300))


def _tool(client, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = client.post(MCP_URL, data=json.dumps(body), content_type="application/json")
    result = response.json()["result"]
    return result, json.loads(result["content"][0]["text"])


class Base(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="MusicBox")
        self.idea = Idea.objects.create(project=self.project, title="Thing", report=LONG, plan="1. Do it")

    def fresh(self):
        return Idea.objects.get(pk=self.idea.pk)


class AppendPrependTests(Base):
    def test_append_keeps_a_long_field_byte_for_byte(self):
        self.assertGreater(len(LONG), 15_000)
        services.update_idea(self.idea.pk, {"append": {"report": "New line."}})
        self.assertEqual(self.fresh().report, LONG + "\n\nNew line.")

    def test_append_after_text_without_newline_adds_a_blank_line(self):
        services.update_idea(self.idea.pk, {"append": {"plan": "2. Then this"}})
        self.assertEqual(self.fresh().plan, "1. Do it\n\n2. Then this")

    def test_join_adds_only_the_missing_newlines(self):
        # Form-saved text is stripped; an ORM-written one may still end in newlines.
        self.assertEqual(services._join("a\n\n", "b"), "a\n\nb")
        self.assertEqual(services._join("a\n", "b"), "a\n\nb")
        self.assertEqual(services._join("a", "b"), "a\n\nb")

    def test_append_to_empty_field_stores_text_as_is(self):
        services.update_idea(self.idea.pk, {"append": {"notes": "Open question for the user"}})
        self.assertEqual(self.fresh().notes, "Open question for the user")

    def test_prepend_keeps_the_old_text_as_the_suffix(self):
        services.update_idea(self.idea.pk, {"prepend": {"report": "Newest status."}})
        self.assertEqual(self.fresh().report, "Newest status.\n\n" + LONG)

    def test_append_and_prepend_combine_in_one_call(self):
        services.update_idea(
            self.idea.pk,
            {"prepend": {"plan": "0. First"}, "append": {"plan": "2. Last", "notes": "n"}, "title": "T2"},
        )
        idea = self.fresh()
        self.assertEqual(idea.plan, "0. First\n\n1. Do it\n\n2. Last")
        self.assertEqual(idea.notes, "n")
        self.assertEqual(idea.title, "T2")

    def test_replace_and_append_on_one_field_is_refused(self):
        with self.assertRaisesMessage(services.Invalid, "plan"):
            services.update_idea(self.idea.pk, {"plan": "x", "append": {"plan": "y"}})
        self.assertEqual(self.fresh().plan, "1. Do it")

    def test_unknown_or_non_text_keys_are_refused(self):
        for bad in ({"title": "x"}, {"status": "x"}, {"commits": "x"}, {"nope": "x"}):
            with self.subTest(bad=bad), self.assertRaises(services.Invalid):
                services.update_idea(self.idea.pk, {"append": bad})

    def test_non_string_value_and_non_object_are_refused(self):
        for bad in ({"append": {"notes": 3}}, {"prepend": ["notes"]}, {"append": "notes"}):
            with self.subTest(bad=bad), self.assertRaises(services.Invalid):
                services.update_idea(self.idea.pk, bad)

    def test_result_names_written_fields_and_previous_lengths(self):
        result = services.update_idea(self.idea.pk, {"append": {"report": "x"}, "title": "New"})
        self.assertEqual(result["updated"], ["report", "title"])
        self.assertEqual(result["previous_lengths"], {"report": len(LONG), "title": len("Thing")})


class CommitsAddTests(Base):
    def test_adds_new_hashes_and_skips_known_ones_including_abbreviated(self):
        Idea.objects.filter(pk=self.idea.pk).update(commits=["abc1234def5678"])
        services.update_idea(
            self.idea.pk, {"commits_add": ["abc1234", "abc1234def5678", "fff0000", "fff0000"]}
        )
        self.assertEqual(self.fresh().commits, ["abc1234def5678", "fff0000"])

    def test_cannot_combine_with_commits(self):
        with self.assertRaises(services.Invalid):
            services.update_idea(self.idea.pk, {"commits": ["a1b2c3d"], "commits_add": ["e4f5a6b"]})

    def test_must_be_a_list_of_strings(self):
        for bad in ("abc1234", [1234567], {"a": 1}):
            with self.subTest(bad=bad), self.assertRaises(services.Invalid):
                services.update_idea(self.idea.pk, {"commits_add": bad})

    def test_previous_length_counts_hashes(self):
        Idea.objects.filter(pk=self.idea.pk).update(commits=["a1b2c3d"])
        result = services.update_idea(self.idea.pk, {"commits_add": ["e4f5a6b"]})
        self.assertEqual(result["previous_lengths"], {"commits": 1})


class RevisionRecordingTests(Base):
    def test_create_records_nothing(self):
        self.assertEqual(FieldRevision.objects.count(), 0)

    def test_change_without_a_source_is_still_revisioned(self):
        services.update_idea(self.idea.pk, {"report": "short"})
        (rev,) = FieldRevision.objects.all()
        self.assertEqual((rev.idea_id, rev.field, rev.old_value, rev.source), (self.idea.pk, "report", LONG, None))

    def test_revision_carries_the_source(self):
        with activity.source("mcp"):
            services.update_idea(self.idea.pk, {"plan": "2."})
        self.assertEqual(FieldRevision.objects.get().source, "mcp")

    def test_unchanged_fields_and_status_moves_record_nothing(self):
        services.update_idea(self.idea.pk, {"plan": "1. Do it", "report": LONG})
        services.transition_idea(self.idea.pk, "planned")
        self.assertEqual(FieldRevision.objects.count(), 0)

    def test_commits_old_value_is_json(self):
        services.update_idea(self.idea.pk, {"commits": ["a1b2c3d"]})
        rev = FieldRevision.objects.get(field="commits")
        self.assertEqual(rev.old_value, "[]")
        self.assertEqual(rev.value, [])

    def test_keeps_the_newest_revisions_per_field(self):
        for i in range(REVISION_KEEP + 5):
            services.update_idea(self.idea.pk, {"notes": f"v{i}"})
        services.update_idea(self.idea.pk, {"plan": "other"})
        notes = FieldRevision.objects.filter(field="notes")
        self.assertEqual(notes.count(), REVISION_KEEP)
        # The newest old value is the one before the last write; the oldest kept is v4.
        self.assertEqual(notes.first().old_value, f"v{REVISION_KEEP + 3}")
        self.assertEqual(notes.last().old_value, "v4")
        self.assertEqual(FieldRevision.objects.filter(field="plan").count(), 1)

    def test_deleting_the_idea_drops_its_revisions(self):
        services.update_idea(self.idea.pk, {"notes": "x"})
        self.idea.delete()
        self.assertEqual(FieldRevision.objects.count(), 0)


class HistoryAndRestoreTests(Base):
    def test_get_idea_history_lists_old_values_newest_first(self):
        services.update_idea(self.idea.pk, {"report": "second"})
        services.update_idea(self.idea.pk, {"report": "third"})
        data = services.get_idea(self.idea.pk, include_prompt=False, history="report")
        self.assertEqual([h["old_value"] for h in data["history"]], ["second", LONG])
        self.assertEqual(set(data["history"][0]), {"id", "old_value", "created_at", "source"})

    def test_history_of_an_unknown_field_is_refused(self):
        with self.assertRaises(services.Invalid):
            services.get_idea(self.idea.pk, history="status")

    def test_restore_brings_the_old_text_back_and_is_itself_revisioned(self):
        services.update_idea(self.idea.pk, {"report": "oops"})
        rev = FieldRevision.objects.get(field="report")
        services.restore_field(self.idea.pk, rev.pk)
        self.assertEqual(self.fresh().report, LONG)
        self.assertEqual(
            list(FieldRevision.objects.filter(field="report").values_list("old_value", flat=True)),
            ["oops", LONG],
        )

    def test_restore_of_commits(self):
        services.update_idea(self.idea.pk, {"commits": ["a1b2c3d"]})
        services.update_idea(self.idea.pk, {"commits": []})
        rev = FieldRevision.objects.filter(field="commits").first()
        services.restore_field(self.idea.pk, rev.pk)
        self.assertEqual(self.fresh().commits, ["a1b2c3d"])

    def test_restore_of_another_ideas_revision_is_not_found(self):
        other = Idea.objects.create(project=self.project, title="Other")
        services.update_idea(other.pk, {"notes": "x"})
        rev = FieldRevision.objects.get(idea_id=other.pk)
        with self.assertRaises(services.NotFound):
            services.restore_field(self.idea.pk, rev.pk)


class StaleWriteTests(Base):
    def read(self):
        return services.get_idea(self.idea.pk, include_prompt=False)["updated_at"]

    def test_replace_after_another_writers_change_to_that_field_is_refused(self):
        since = self.read()
        with activity.source("web"):
            services.update_idea(self.idea.pk, {"plan": "the user's edit"})
        with self.assertRaises(services.Invalid) as caught:
            services.update_idea(self.idea.pk, {"plan": "mine", "if_unmodified_since": since})
        message = str(caught.exception)
        self.assertIn("plan", message)
        self.assertIn("web", message)
        self.assertIn("re-read", message)
        self.assertEqual(self.fresh().plan, "the user's edit")

    def test_change_to_another_field_does_not_block(self):
        since = self.read()
        services.update_idea(self.idea.pk, {"notes": "someone else"})
        services.update_idea(self.idea.pk, {"plan": "mine", "if_unmodified_since": since})
        self.assertEqual(self.fresh().plan, "mine")

    def test_transition_and_append_do_not_block(self):
        since = self.read()
        services.transition_idea(self.idea.pk, "planned")
        services.update_idea(self.idea.pk, {"append": {"notes": "x"}, "if_unmodified_since": since})
        services.update_idea(self.idea.pk, {"report": "final", "if_unmodified_since": since})
        self.assertEqual(self.fresh().report, "final")

    def test_another_writers_append_blocks_a_replace_of_that_field(self):
        since = self.read()
        services.update_idea(self.idea.pk, {"append": {"plan": "2. theirs"}})
        with self.assertRaises(services.Invalid):
            services.update_idea(self.idea.pk, {"plan": "mine", "if_unmodified_since": since})

    def test_reading_after_ones_own_write_is_not_stale(self):
        services.update_idea(self.idea.pk, {"plan": "first"})
        since = self.read()
        services.update_idea(self.idea.pk, {"plan": "second", "if_unmodified_since": since})
        self.assertEqual(self.fresh().plan, "second")

    def test_without_the_param_nothing_is_checked(self):
        services.update_idea(self.idea.pk, {"plan": "theirs"})
        services.update_idea(self.idea.pk, {"plan": "mine"})
        self.assertEqual(self.fresh().plan, "mine")

    def test_a_bad_timestamp_is_refused(self):
        with self.assertRaises(services.Invalid):
            services.update_idea(self.idea.pk, {"plan": "x", "if_unmodified_since": "yesterday"})


class FieldsSubsetTests(Base):
    def test_returns_named_fields_plus_identity(self):
        data = services.get_idea(self.idea.pk, include_prompt=False, fields=["plan"])
        self.assertEqual(set(data), {"id", "status", "updated_at", "allowed_transitions", "plan"})

    def test_unknown_field_is_refused(self):
        with self.assertRaises(services.Invalid):
            services.get_idea(self.idea.pk, fields=["plan", "nope"])


class McpTests(Base):
    def test_schema_offers_the_new_arguments(self):
        response = self.client.post(
            MCP_URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
        )
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        update = tools["update_idea"]
        props = update["inputSchema"]["properties"]
        for name in ("append", "prepend", "commits_add", "if_unmodified_since"):
            self.assertIn(name, props)
        self.assertIn("replaces", update["description"])
        self.assertEqual(
            set(props["append"]["properties"]), {"goal", "context", "notes", "plan", "report", "review"}
        )
        get_props = tools["get_idea"]["inputSchema"]["properties"]
        self.assertIn("fields", get_props)
        self.assertIn("history", get_props)

    def test_append_through_the_tool(self):
        result, ack = _tool(self.client, "update_idea", {"idea_id": self.idea.pk, "append": {"report": "x"}})
        self.assertNotIn("isError", result)
        self.assertEqual(ack["updated"], ["report"])
        self.assertEqual(ack["previous_lengths"], {"report": len(LONG)})
        self.assertEqual(self.fresh().report, LONG + "\n\nx")
        self.assertEqual(FieldRevision.objects.get().source, "mcp")

    def test_stale_replace_is_an_is_error_result(self):
        _, record = _tool(self.client, "get_idea", {"idea_id": self.idea.pk, "fields": ["plan"]})
        services.update_idea(self.idea.pk, {"plan": "theirs"})
        result, payload = _tool(
            self.client, "update_idea",
            {"idea_id": self.idea.pk, "plan": "mine", "if_unmodified_since": record["updated_at"]},
        )
        self.assertTrue(result["isError"])
        self.assertIn("plan", payload["error"])

    def test_get_idea_fields_and_history(self):
        services.update_idea(self.idea.pk, {"plan": "2."})
        _, data = _tool(self.client, "get_idea", {"idea_id": self.idea.pk, "fields": ["plan"], "history": "plan"})
        self.assertEqual(data["plan"], "2.")
        self.assertNotIn("report", data)
        self.assertEqual(data["history"][0]["old_value"], "1. Do it")


class ApiTests(Base):
    def test_patch_accepts_append(self):
        response = self.client.patch(
            f"/api/ideas/{self.idea.pk}/", data=json.dumps({"append": {"notes": "n"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notes"], "n")
        self.assertEqual(response.json()["updated"], ["notes"])

    def test_history_endpoint(self):
        services.update_idea(self.idea.pk, {"plan": "2."})
        response = self.client.get(f"/api/ideas/{self.idea.pk}/history/?field=plan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([h["old_value"] for h in response.json()], ["1. Do it"])

    def test_history_endpoint_needs_a_known_field(self):
        for query in ("", "?field=status"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/ideas/{self.idea.pk}/history/{query}")
                self.assertEqual(response.status_code, 400)

    def test_openapi_documents_the_history_route(self):
        paths = self.client.get("/api/openapi.json").json()["paths"]
        self.assertIn("/api/ideas/{pk}/history/", paths)


class PageTests(Base):
    def test_idea_page_links_history_only_when_there_is_some(self):
        url = f"/ideas/{self.idea.pk}/"
        self.assertNotContains(self.client.get(url), "History (")
        services.update_idea(self.idea.pk, {"plan": "2."})
        self.assertContains(self.client.get(url), "History (1)")

    def test_history_page_lists_revisions_with_restore_forms(self):
        services.update_idea(self.idea.pk, {"plan": "2."})
        services.update_idea(self.idea.pk, {"report": "short"})
        response = self.client.get(f"/ideas/{self.idea.pk}/history/")
        self.assertContains(response, "1. Do it")
        self.assertContains(response, "progress noted")
        self.assertContains(response, 'method="post"', count=2)
        filtered = self.client.get(f"/ideas/{self.idea.pk}/history/?field=plan")
        self.assertContains(filtered, 'method="post"', count=1)

    def test_restore_posts_and_redirects(self):
        services.update_idea(self.idea.pk, {"plan": "2."})
        rev = FieldRevision.objects.get()
        response = self.client.post(f"/ideas/{self.idea.pk}/history/{rev.pk}/restore/")
        self.assertRedirects(response, f"/ideas/{self.idea.pk}/history/", fetch_redirect_response=False)
        self.assertEqual(self.fresh().plan, "1. Do it")
        self.assertEqual(FieldRevision.objects.filter(field="plan").first().source, "web")

    def test_htmx_restore_returns_the_list_partial(self):
        services.update_idea(self.idea.pk, {"plan": "2."})
        rev = FieldRevision.objects.get()
        response = self.client.post(
            f"/ideas/{self.idea.pk}/history/{rev.pk}/restore/", headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="idea-history-{self.idea.pk}"')
        self.assertNotContains(response, "<html")
        self.assertContains(response, "Restored")

    def test_restore_of_a_foreign_revision_is_404(self):
        other = Idea.objects.create(project=self.project, title="Other")
        services.update_idea(other.pk, {"notes": "x"})
        rev = FieldRevision.objects.get()
        response = self.client.post(f"/ideas/{self.idea.pk}/history/{rev.pk}/restore/")
        self.assertEqual(response.status_code, 404)
