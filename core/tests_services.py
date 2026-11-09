import json
import os
import subprocess
import tempfile
from unittest import mock

from django.test import TestCase, override_settings

from . import services
from .forms import IdeaForm
from .models import Idea, OPEN_IDEA_STATUSES, Project, Screenshot
from .transitions import IDEA_TRANSITIONS, PROJECT_TRANSITIONS


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


class ResolveProjectTests(TestCase):
    def test_accepts_pk_int(self):
        project = Project.objects.create(name="A", folder="a")
        self.assertEqual(services._resolve_project(project.pk).pk, project.pk)

    def test_accepts_digit_string(self):
        project = Project.objects.create(name="A", folder="a")
        self.assertEqual(services._resolve_project(str(project.pk)).pk, project.pk)

    def test_accepts_folder_string(self):
        project = Project.objects.create(name="A", folder="a")
        self.assertEqual(services._resolve_project("a").pk, project.pk)

    def test_missing_raises_not_found(self):
        with self.assertRaises(services.NotFound):
            services._resolve_project("nope")
        with self.assertRaises(services.NotFound):
            services._resolve_project(999)


class PartialUpdateTests(TestCase):
    def test_project_update_leaves_other_fields_intact(self):
        project = Project.objects.create(name="Name", folder="folder", description="desc")
        result = services.update_project(project.pk, {"name": "New Name"})
        self.assertEqual(result["name"], "New Name")
        self.assertEqual(result["folder"], "folder")
        self.assertEqual(result["description"], "desc")
        project.refresh_from_db()
        self.assertEqual(project.folder, "folder")
        self.assertEqual(project.description, "desc")

    def test_idea_update_leaves_other_fields_intact(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(
            project=project,
            title="Title",
            goal="Goal",
            context="Context",
            notes="Notes",
        )
        result = services.update_idea(idea.pk, {"title": "New Title"})
        self.assertEqual(result["title"], "New Title")
        self.assertEqual(result["goal"], "Goal")
        self.assertEqual(result["context"], "Context")
        self.assertEqual(result["notes"], "Notes")
        idea.refresh_from_db()
        self.assertEqual(idea.goal, "Goal")
        self.assertEqual(idea.context, "Context")
        self.assertEqual(idea.notes, "Notes")


class IdeaPlanFieldTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="A", folder="a")

    def test_create_idea_stores_and_returns_the_plan(self):
        result = services.create_idea(
            self.project.pk, {"title": "T", "plan": "1. Step one"}
        )
        self.assertEqual(result["plan"], "1. Step one")
        self.assertEqual(Idea.objects.get(pk=result["id"]).plan, "1. Step one")

    def test_update_idea_sets_the_plan_without_touching_other_fields(self):
        idea = Idea.objects.create(project=self.project, title="T", goal="Goal")
        result = services.update_idea(idea.pk, {"plan": "1. Step one"})
        self.assertEqual(result["plan"], "1. Step one")
        self.assertEqual(result["goal"], "Goal")

    def test_serialized_idea_prompt_carries_the_plan(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="1. Step one")
        self.assertIn("### Implementation plan", services.get_idea(idea.pk)["prompt"])


class UnknownKeyTests(TestCase):
    def test_create_project_rejects_unknown_key(self):
        with self.assertRaises(services.Invalid):
            services.create_project({"name": "A", "folder": "a", "nope": "x"})

    def test_update_project_rejects_unknown_key(self):
        project = Project.objects.create(name="A", folder="a")
        with self.assertRaises(services.Invalid):
            services.update_project(project.pk, {"nope": "x"})

    def test_create_idea_rejects_unknown_key(self):
        project = Project.objects.create(name="A", folder="a")
        with self.assertRaises(services.Invalid):
            services.create_idea(project.pk, {"title": "T", "nope": "x"})

    def test_update_idea_rejects_unknown_key(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        with self.assertRaises(services.Invalid):
            services.update_idea(idea.pk, {"nope": "x"})


class StatusNotWritableTests(TestCase):
    def test_create_project_rejects_status(self):
        with self.assertRaises(services.Invalid):
            services.create_project({"name": "A", "folder": "a", "status": "implemented"})

    def test_update_project_rejects_status(self):
        project = Project.objects.create(name="A", folder="a")
        with self.assertRaises(services.Invalid):
            services.update_project(project.pk, {"status": "implemented"})

    def test_create_idea_rejects_status(self):
        project = Project.objects.create(name="A", folder="a")
        with self.assertRaises(services.Invalid):
            services.create_idea(project.pk, {"title": "T", "status": "planned"})

    def test_update_idea_rejects_status(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        with self.assertRaises(services.Invalid):
            services.update_idea(idea.pk, {"status": "planned"})


class TransitionTests(TestCase):
    def test_illegal_project_transition_raises_and_does_not_save(self):
        project = Project.objects.create(name="A", folder="a")  # status "idea"
        with self.assertRaises(services.Invalid):
            services.transition_project(project.pk, "implemented")
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.IDEA)

    def test_illegal_idea_transition_raises_and_does_not_save(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")  # status "new"
        with self.assertRaises(services.Invalid):
            services.transition_idea(idea.pk, "implemented")
        idea.refresh_from_db()
        self.assertEqual(idea.status, Idea.Status.NEW)

    def test_every_legal_project_transition_saves(self):
        for from_status, transitions in PROJECT_TRANSITIONS.items():
            for transition in transitions:
                project = Project.objects.create(
                    name=f"P {from_status} {transition.target}",
                    folder=f"p-{from_status}-{transition.target}",
                    status=from_status,
                )
                result = services.transition_project(project.pk, transition.target)
                self.assertEqual(result["status"], transition.target)
                project.refresh_from_db()
                self.assertEqual(project.status, transition.target)

    def test_every_legal_idea_transition_saves(self):
        project = Project.objects.create(name="A", folder="a")
        for from_status, transitions in IDEA_TRANSITIONS.items():
            for transition in transitions:
                idea = Idea.objects.create(
                    project=project,
                    title=f"I {from_status} {transition.target}",
                    status=from_status,
                    needs_verification=True,
                )
                result = services.transition_idea(idea.pk, transition.target)
                self.assertEqual(result["status"], transition.target)
                idea.refresh_from_db()
                self.assertEqual(idea.status, transition.target)


class ValidationFailedTests(TestCase):
    def test_create_project_duplicate_folder_raises_validation_failed(self):
        Project.objects.create(name="A", folder="dup")
        with self.assertRaises(services.ValidationFailed) as ctx:
            services.create_project({"name": "B", "folder": "dup"})
        errors = ctx.exception.errors
        self.assertIn("folder", errors)
        json.dumps(errors)  # must be JSON-serializable

    def test_create_idea_blank_title_raises_validation_failed(self):
        project = Project.objects.create(name="A", folder="a")
        with self.assertRaises(services.ValidationFailed) as ctx:
            services.create_idea(project.pk, {"title": ""})
        errors = ctx.exception.errors
        self.assertIn("title", errors)
        json.dumps(errors)


class IdeaKindFieldTests(TestCase):
    def test_default_kind_is_other(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.assertEqual(idea.kind, Idea.Kind.OTHER)

    def test_list_ideas_filters_by_kind(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Fix", kind=Idea.Kind.FIX)
        Idea.objects.create(project=project, title="Feature", kind=Idea.Kind.FEATURE)

        titles = {i["title"] for i in services.list_ideas(kind=Idea.Kind.FIX)}
        self.assertEqual(titles, {"Fix"})

    def test_list_ideas_unknown_kind_raises_invalid(self):
        with self.assertRaises(services.Invalid):
            services.list_ideas(kind="bogus")

    def test_idea_to_dict_carries_kind_and_kind_display(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T", kind=Idea.Kind.FIX)
        result = services.idea_to_dict(idea)
        self.assertEqual(result["kind"], "fix")
        self.assertEqual(result["kind_display"], "Fix")

    def test_idea_to_dict_blanks_kind_display_for_other(self):
        # "other" is what "no type" means -- kind stays "other" (a real enum value,
        # needed for filtering and editing) but kind_display must be blank, matching
        # the chip and the generated prompt, both of which already hide it.
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        result = services.idea_to_dict(idea)
        self.assertEqual(result["kind"], "other")
        self.assertEqual(result["kind_display"], "")

    def test_blank_kind_is_stored_as_other(self):
        # blank=True makes an omitted kind legal; it must not make a blank one storable,
        # since the API advertises a closed enum and the card would render an unlabelled
        # chip for a value outside it.
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T", kind="")
        idea.refresh_from_db()
        self.assertEqual(idea.kind, Idea.Kind.OTHER)

    def test_create_idea_without_a_kind_defaults_to_other(self):
        project = Project.objects.create(name="A", folder="a")
        result = services.create_idea(project.pk, {"title": "T"})
        self.assertEqual(result["kind"], Idea.Kind.OTHER)

    def test_form_offers_no_blank_kind_choice(self):
        # "Other" is what "no type" means, so a sixth, nameless option would be a lie.
        self.assertNotIn("", [value for value, _ in IdeaForm().fields["kind"].choices])


class SerializationTests(TestCase):
    def test_idea_to_dict_has_prompt_and_root_path_and_transitions(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T", goal="G")
        result = services.idea_to_dict(idea)
        self.assertEqual(result["prompt"], idea.build_prompt())
        self.assertEqual(result["project_root_path"], project.root_path)
        self.assertEqual(
            result["allowed_transitions"],
            [{"target": t.target, "label": t.label} for t in idea.allowed_transitions()],
        )

    def test_project_to_dict_is_json_serializable(self):
        project = Project.objects.create(name="A", folder="a")
        json.dumps(services.project_to_dict(project))

    def test_idea_to_dict_is_json_serializable(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        json.dumps(services.idea_to_dict(idea))


class ListFilterTests(TestCase):
    def test_list_ideas_open_only_excludes_closed(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="New", status=Idea.Status.NEW)
        Idea.objects.create(project=project, title="Planned", status=Idea.Status.PLANNED)
        Idea.objects.create(project=project, title="Implemented", status=Idea.Status.IMPLEMENTED)
        Idea.objects.create(project=project, title="Abandoned", status=Idea.Status.ABANDONED)

        titles = {i["title"] for i in services.list_ideas(open_only=True)}
        self.assertEqual(titles, {"New", "Planned"})

    def test_list_ideas_unknown_status_raises_invalid(self):
        with self.assertRaises(services.Invalid):
            services.list_ideas(status="bogus")

    def test_list_ideas_filters_by_full_commit_hash(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Has it", commits=["abc123def456"])
        Idea.objects.create(project=project, title="No commit", commits=[])

        titles = {i["title"] for i in services.list_ideas(commit="abc123def456")}
        self.assertEqual(titles, {"Has it"})

    def test_list_ideas_filters_by_short_query_against_full_stored_hash(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Has it", commits=["abc123def456"])

        titles = {i["title"] for i in services.list_ideas(commit="abc123")}
        self.assertEqual(titles, {"Has it"})

    def test_list_ideas_filters_by_full_query_against_short_stored_hash(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Has it", commits=["abc123"])

        titles = {i["title"] for i in services.list_ideas(commit="abc123def456")}
        self.assertEqual(titles, {"Has it"})

    def test_list_ideas_commit_no_match_returns_empty(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Has it", commits=["abc123def456"])

        self.assertEqual(services.list_ideas(commit="zzzzzz"), [])

    def test_list_ideas_commit_query_too_short_raises_invalid(self):
        with self.assertRaises(services.Invalid):
            services.list_ideas(commit="abc")

    def _search_fixture(self):
        a = Project.objects.create(name="A", folder="a")
        b = Project.objects.create(name="B", folder="b")
        Idea.objects.create(project=a, title="Retry uploads", report="The USB mic drops samples.")
        Idea.objects.create(project=a, title="Mic gain", goal="Louder recordings")
        Idea.objects.create(project=b, title="Other", review="usb cable is loose")
        return a, b

    def test_list_ideas_q_matches_a_word_found_only_in_the_report(self):
        self._search_fixture()
        titles = {i["title"] for i in services.list_ideas(q="samples")}
        self.assertEqual(titles, {"Retry uploads"})

    def test_list_ideas_q_is_case_insensitive_and_covers_every_text_field(self):
        a = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=a, title="NEEDLE in the title")
        for field in ("goal", "context", "notes", "plan", "report", "review"):
            Idea.objects.create(project=a, title=field, **{field: f"x NEEDLE-{field} y"})
        Idea.objects.create(project=a, title="unrelated")
        self.assertEqual(len(services.list_ideas(q="needle")), 7)

    def test_list_ideas_q_requires_every_word_each_in_any_field(self):
        self._search_fixture()
        # "usb" is in the report, "retry" in the title: both match the same idea.
        titles = {i["title"] for i in services.list_ideas(q="usb retry")}
        self.assertEqual(titles, {"Retry uploads"})
        # "usb" alone also matches the other project's review.
        titles = {i["title"] for i in services.list_ideas(q="usb")}
        self.assertEqual(titles, {"Retry uploads", "Other"})

    def test_list_ideas_q_combines_with_project(self):
        self._search_fixture()
        titles = {i["title"] for i in services.list_ideas(q="usb", project="b")}
        self.assertEqual(titles, {"Other"})

    def test_list_ideas_q_combines_with_commit(self):
        a, _ = self._search_fixture()
        Idea.objects.create(project=a, title="Mic fix", report="usb", commits=["abc123def456"])
        titles = {i["title"] for i in services.list_ideas(q="usb", commit="abc123")}
        self.assertEqual(titles, {"Mic fix"})

    def test_list_ideas_q_without_hits_returns_empty(self):
        self._search_fixture()
        self.assertEqual(services.list_ideas(q="usb nonexistentword"), [])

    def test_list_ideas_blank_q_does_not_filter(self):
        self._search_fixture()
        self.assertEqual(len(services.list_ideas(q="   ")), 3)

    def test_list_projects_filters_by_status(self):
        Project.objects.create(name="A", folder="a", status=Project.Status.IDEA)
        Project.objects.create(name="B", folder="b", status=Project.Status.IMPLEMENTED)

        names = {p["name"] for p in services.list_projects(status=Project.Status.IMPLEMENTED)}
        self.assertEqual(names, {"B"})

    def test_list_projects_filters_by_folder(self):
        Project.objects.create(name="A", folder="a")
        Project.objects.create(name="B", folder="b")

        names = {p["name"] for p in services.list_projects(folder="b")}
        self.assertEqual(names, {"B"})

    def test_list_ideas_no_n_plus_one(self):
        project_a = Project.objects.create(name="A", folder="a")
        project_b = Project.objects.create(name="B", folder="b")
        for i in range(3):
            Idea.objects.create(project=project_a, title=f"A idea {i}")
        for i in range(2):
            Idea.objects.create(project=project_b, title=f"B idea {i}")

        # One query for the ideas (joined to project via select_related), one for
        # their screenshots and one for their artifacts (each prefetch_related,
        # batched across every idea in the queryset) -- allowed_transitions/etc.
        # need no extra query at all, the whole point of select_related is that
        # touching idea.project costs nothing more.
        with self.assertNumQueries(3):
            result = services.list_ideas()
        self.assertEqual(len(result), 5)


class IdeaReportFieldTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="A", folder="a")

    def test_update_idea_sets_the_report(self):
        idea = Idea.objects.create(project=self.project, title="T")
        result = services.update_idea(idea.pk, {"report": "Implemented as planned. Nothing to report."})
        self.assertEqual(result["report"], "Implemented as planned. Nothing to report.")
        idea.refresh_from_db()
        self.assertEqual(idea.report, "Implemented as planned. Nothing to report.")

    def test_report_is_not_a_prompt_section(self):
        # The report is output of the work, not input to it: build_prompt() must be
        # unaffected by whatever gets written there.
        idea = Idea.objects.create(project=self.project, title="T", goal="G")
        before = idea.build_prompt()
        idea.report = "Ran into a snag with the config loader."
        idea.save()
        idea.refresh_from_db()
        self.assertEqual(idea.build_prompt(), before)
        self.assertNotIn("snag", idea.build_prompt())

    def test_idea_to_dict_carries_report(self):
        idea = Idea.objects.create(project=self.project, title="T", report="All good.")
        self.assertEqual(services.idea_to_dict(idea)["report"], "All good.")


class IdeaVerifyTransitionTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="A", folder="a")

    def test_planned_moves_to_verify_not_directly_to_implemented(self):
        idea = Idea.objects.create(
            project=self.project, title="T", status=Idea.Status.PLANNED, needs_verification=True
        )
        with self.assertRaises(services.Invalid):
            services.transition_idea(idea.pk, "implemented")
        result = services.transition_idea(idea.pk, "verify")
        self.assertEqual(result["status"], "verify")

    def test_verify_can_move_to_implemented_planned_or_abandoned(self):
        for target in ("implemented", "planned", "abandoned"):
            idea = Idea.objects.create(
                project=self.project, title=f"T {target}", status=Idea.Status.VERIFY
            )
            result = services.transition_idea(idea.pk, target)
            self.assertEqual(result["status"], target)

    def test_verify_is_an_open_status(self):
        self.assertIn(Idea.Status.VERIFY, OPEN_IDEA_STATUSES)


class NeedsVerificationTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="A", folder="a")

    def _planned(self, needs_verification):
        return Idea.objects.create(
            project=self.project,
            title="T",
            status=Idea.Status.PLANNED,
            needs_verification=needs_verification,
        )

    def test_create_defaults_to_no_verification(self):
        result = services.create_idea("a", {"title": "New"})
        self.assertFalse(result["needs_verification"])
        self.assertFalse(Idea.objects.get(pk=result["id"]).needs_verification)

    def test_create_can_ask_for_verification(self):
        result = services.create_idea("a", {"title": "New", "needs_verification": True})
        self.assertTrue(result["needs_verification"])

    def test_update_toggles_it_and_a_partial_update_keeps_it(self):
        idea = self._planned(False)
        services.update_idea(idea.pk, {"needs_verification": True})
        services.update_idea(idea.pk, {"title": "Renamed"})
        idea.refresh_from_db()
        self.assertTrue(idea.needs_verification)
        services.update_idea(idea.pk, {"needs_verification": False})
        idea.refresh_from_db()
        self.assertFalse(idea.needs_verification)

    def test_planned_without_verification_offers_done_not_verify(self):
        targets = [t["target"] for t in services.get_idea(self._planned(False).pk)["allowed_transitions"]]
        self.assertEqual(targets, ["implemented", "abandoned"])

    def test_verify_target_closes_an_idea_without_verification(self):
        # Agents are told to transition to verify; that must still work, and land on
        # implemented instead.
        idea = self._planned(False)
        result = services.transition_idea(idea.pk, "verify")
        self.assertEqual(result["status"], "implemented")
        idea.refresh_from_db()
        self.assertEqual(idea.status, Idea.Status.IMPLEMENTED)

    def test_implemented_target_works_directly_without_verification(self):
        result = services.transition_idea(self._planned(False).pk, "implemented")
        self.assertEqual(result["status"], "implemented")

    def test_verify_target_unchanged_with_verification(self):
        result = services.transition_idea(self._planned(True).pk, "verify")
        self.assertEqual(result["status"], "verify")

    def test_verify_alias_only_applies_from_planned(self):
        idea = Idea.objects.create(project=self.project, title="T", needs_verification=False)
        with self.assertRaises(services.Invalid):
            services.transition_idea(idea.pk, "verify")
        idea.refresh_from_db()
        self.assertEqual(idea.status, Idea.Status.NEW)

    def test_an_idea_already_in_verify_keeps_its_transitions(self):
        idea = Idea.objects.create(
            project=self.project, title="T", status=Idea.Status.VERIFY, needs_verification=False
        )
        targets = {t.target for t in idea.allowed_transitions()}
        self.assertEqual(targets, {"implemented", "planned", "abandoned"})


class ScreenshotServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="A", folder="a")
        self.idea = Idea.objects.create(project=self.project, title="T")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def test_add_screenshot_stores_file_and_returns_idea(self):
        result = services.add_screenshot(self.idea.pk, "shot.png", b"pngbytes", caption="Before")
        self.assertEqual(len(result["screenshots"]), 1)
        shot = result["screenshots"][0]
        self.assertEqual(shot["caption"], "Before")
        self.assertTrue(shot["url"])
        self.assertEqual(Screenshot.objects.filter(idea=self.idea).count(), 1)

    def test_add_screenshot_rejects_bad_extension(self):
        with self.assertRaises(services.Invalid):
            services.add_screenshot(self.idea.pk, "shot.exe", b"whatever")
        self.assertEqual(Screenshot.objects.count(), 0)

    def test_add_screenshot_rejects_oversized_file(self):
        big = b"x" * (services.SCREENSHOT_MAX_BYTES + 1)
        with self.assertRaises(services.Invalid):
            services.add_screenshot(self.idea.pk, "shot.png", big)
        self.assertEqual(Screenshot.objects.count(), 0)

    def test_add_screenshot_missing_idea_is_not_found(self):
        with self.assertRaises(services.NotFound):
            services.add_screenshot(999999, "shot.png", b"pngbytes")

    def test_delete_screenshot_removes_row_and_file(self):
        result = services.add_screenshot(self.idea.pk, "shot.png", b"pngbytes")
        screenshot_id = result["screenshots"][0]["id"]
        stored = Screenshot.objects.get(pk=screenshot_id)
        file_path = stored.image.path
        self.assertTrue(os.path.exists(file_path))

        result = services.delete_screenshot(screenshot_id)
        self.assertEqual(result["screenshots"], [])
        self.assertFalse(Screenshot.objects.filter(pk=screenshot_id).exists())
        self.assertFalse(os.path.exists(file_path))

    def test_delete_missing_screenshot_is_not_found(self):
        with self.assertRaises(services.NotFound):
            services.delete_screenshot(999999)

    def test_idea_to_dict_screenshots_are_oldest_first(self):
        services.add_screenshot(self.idea.pk, "one.png", b"a")
        services.add_screenshot(self.idea.pk, "two.png", b"b")
        result = services.get_idea(self.idea.pk)
        self.assertEqual(len(result["screenshots"]), 2)
        created_ats = [s["created_at"] for s in result["screenshots"]]
        self.assertEqual(created_ats, sorted(created_ats))


class IdeaCommitsTests(TestCase):
    def test_update_idea_with_commits(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test")
        result = services.update_idea(idea.pk, {
            "title": "Test",
            "commits": ["abc123", "def456"]
        })
        self.assertEqual(result["commits"], ["abc123", "def456"])
        idea.refresh_from_db()
        self.assertEqual(idea.commits, ["abc123", "def456"])

    def test_create_idea_with_commits(self):
        project = Project.objects.create(name="A", folder="a")
        result = services.create_idea(project.pk, {
            "title": "Test",
            "commits": ["hash1", "hash2", "hash3"]
        })
        self.assertEqual(result["commits"], ["hash1", "hash2", "hash3"])

    def test_idea_default_commits_is_empty_list(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test")
        result = services.get_idea(idea.pk)
        self.assertEqual(result["commits"], [])


class IdeaReviewFieldTests(TestCase):
    def test_review_defaults_to_blank(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test")
        self.assertEqual(idea.review, "")

    def test_review_has_help_text(self):
        self.assertTrue(Idea._meta.get_field("review").help_text)

    def test_idea_to_dict_carries_review(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test", review="Fix the edge case")
        self.assertEqual(services.get_idea(idea.pk)["review"], "Fix the edge case")

    def test_update_idea_sets_the_review(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test")
        result = services.update_idea(idea.pk, {"review": "Fix the edge case"})
        self.assertEqual(result["review"], "Fix the edge case")
        idea.refresh_from_db()
        self.assertEqual(idea.review, "Fix the edge case")

    def test_reopen_via_review_moves_verify_to_planned(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="Test", status=Idea.Status.VERIFY)
        services.update_idea(idea.pk, {"review": "Fix the edge case"})
        result = services.transition_idea(idea.pk, "planned")
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["review"], "Fix the edge case")


class CommitUrlTests(TestCase):
    def test_get_commit_url_github_https(self):
        project = Project.objects.create(name="Test GitHub", folder="test-github")
        # Mock the git config to return a GitHub HTTPS URL
        import unittest.mock as mock
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.MagicMock(
                stdout="https://github.com/user/repo.git\n",
                returncode=0
            )
            url = project.get_commit_url("abc123def456")
            self.assertEqual(url, "https://github.com/user/repo/commit/abc123def456")

    def test_get_commit_url_github_ssh(self):
        project = Project.objects.create(name="Test GitHub SSH", folder="test-github-ssh")
        import unittest.mock as mock
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.MagicMock(
                stdout="git@github.com:user/repo.git\n",
                returncode=0
            )
            url = project.get_commit_url("abc123def456")
            self.assertEqual(url, "https://github.com/user/repo/commit/abc123def456")

    @override_settings(GIT_WEB_BASE="https://git.example.org/you")
    def test_get_commit_url_git_web_base_fallback(self):
        project = Project.objects.create(name="Test Forge", folder="test-forge")
        import unittest.mock as mock
        with mock.patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("No git repo")
            url = project.get_commit_url("abc123def456")
            self.assertEqual(url, "https://git.example.org/you/test-forge/commit/abc123def456")

    @override_settings(GIT_WEB_BASE="")
    def test_get_commit_url_none_without_git_web_base(self):
        project = Project.objects.create(name="Test None", folder="test-none")
        import unittest.mock as mock
        with mock.patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("No git repo")
            self.assertIsNone(project.get_commit_url("abc123def456"))

    @override_settings(GIT_WEB_BASE="https://git.example.org/you")
    def test_get_commit_url_empty_remote(self):
        project = Project.objects.create(name="Test Empty", folder="test-empty")
        import unittest.mock as mock
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.MagicMock(
                stdout="",
                returncode=0
            )
            url = project.get_commit_url("abc123def456")
            self.assertEqual(url, "https://git.example.org/you/test-empty/commit/abc123def456")

    def test_idea_commit_urls_property(self):
        project = Project.objects.create(name="Test Project", folder="test-project")
        idea = Idea.objects.create(
            project=project,
            title="Test Idea",
            commits=["abc123", "def456"]
        )
        import unittest.mock as mock
        with mock.patch.object(project, 'get_commit_url') as mock_get_url:
            mock_get_url.side_effect = lambda h: f"https://example.com/commit/{h}"
            urls = idea.commit_urls
            self.assertEqual(len(urls), 2)
            self.assertEqual(urls[0]["hash"], "abc123")
            self.assertEqual(urls[0]["url"], "https://example.com/commit/abc123")
            self.assertEqual(urls[1]["hash"], "def456")
            self.assertEqual(urls[1]["url"], "https://example.com/commit/def456")


class SyncGitStatusServiceTests(TestCase):
    def test_updates_project_and_returns_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            project = Project.objects.create(name="P", folder="p")

            with mock.patch.object(Project, "root_path", new=property(lambda self: tmp)):
                results = services.sync_git_status()

            project.refresh_from_db()
        self.assertEqual(project.git_status, Project.GitStatus.NO_REMOTE)
        self.assertIsNotNone(project.git_checked_at)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["folder"], "p")
        self.assertEqual(results[0]["git_status"], Project.GitStatus.NO_REMOTE)
        self.assertTrue(results[0]["changed"])

    def test_unchanged_status_is_reported_as_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            project = Project.objects.create(
                name="P",
                folder="p",
                git_status=Project.GitStatus.NO_REMOTE,
                git_detail="no upstream branch configured",
            )

            with mock.patch.object(Project, "root_path", new=property(lambda self: tmp)):
                results = services.sync_git_status()

        self.assertFalse(results[0]["changed"])

    def test_no_projects_returns_empty_list(self):
        self.assertEqual(services.sync_git_status(), [])


class MoveIdeaTests(TestCase):
    def setUp(self):
        self.source = Project.objects.create(name="Source", folder="source")
        self.target = Project.objects.create(name="Target", folder="target")
        self.idea = Idea.objects.create(
            project=self.source,
            title="Title",
            goal="Goal",
            plan="1. Step",
            report="Done.",
            review="Finding",
            commits=["abc123"],
            status=Idea.Status.PLANNED,
        )
        self.screenshot = Screenshot.objects.create(idea=self.idea, image="x.png", caption="c")

    def test_moves_by_folder(self):
        result = services.update_idea(self.idea.pk, {"project": "target"})
        self.assertEqual(result["project_id"], self.target.pk)
        self.assertEqual(result["project_folder"], "target")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.target.pk)

    def test_moves_by_id(self):
        services.update_idea(self.idea.pk, {"project": self.target.pk})
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.target.pk)

    def test_moves_by_digit_string(self):
        services.update_idea(self.idea.pk, {"project": str(self.target.pk)})
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.target.pk)

    def test_unknown_project_raises_not_found_and_does_not_move(self):
        with self.assertRaises(services.NotFound):
            services.update_idea(self.idea.pk, {"project": "nope"})
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.source.pk)

    def test_move_keeps_everything_else(self):
        created_at = self.idea.created_at
        services.update_idea(self.idea.pk, {"project": "target"})
        idea = Idea.objects.get(pk=self.idea.pk)
        self.assertEqual(idea.status, Idea.Status.PLANNED)
        self.assertEqual(idea.title, "Title")
        self.assertEqual(idea.goal, "Goal")
        self.assertEqual(idea.plan, "1. Step")
        self.assertEqual(idea.report, "Done.")
        self.assertEqual(idea.review, "Finding")
        self.assertEqual(idea.commits, ["abc123"])
        self.assertEqual(idea.created_at, created_at)
        self.assertEqual(list(idea.screenshots.values_list("pk", flat=True)), [self.screenshot.pk])

    def test_move_and_edit_in_one_call(self):
        result = services.update_idea(self.idea.pk, {"project": "target", "title": "Renamed"})
        self.assertEqual(result["project_folder"], "target")
        self.assertEqual(result["title"], "Renamed")

    def test_plain_update_keeps_the_project(self):
        services.update_idea(self.idea.pk, {"title": "Renamed"})
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.source.pk)

    def test_create_idea_rejects_project_in_body(self):
        with self.assertRaises(services.Invalid):
            services.create_idea("source", {"title": "T", "project": "target"})


class IdeaPriorityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="musicbox")

    def test_default_is_medium_and_can_be_set_on_create_and_update(self):
        plain = services.create_idea("musicbox", {"title": "Plain"})
        urgent = services.create_idea("musicbox", {"title": "Urgent", "priority": "high"})
        self.assertEqual(plain["priority"], "medium")
        self.assertEqual(urgent["priority"], "high")
        self.assertEqual(services.update_idea(plain["id"], {"priority": "low"})["priority"], "low")
        # An update that never mentions it leaves it alone.
        self.assertEqual(services.update_idea(plain["id"], {"title": "Renamed"})["priority"], "low")

    def test_unknown_priority_is_rejected(self):
        with self.assertRaises(services.ValidationFailed):
            services.create_idea("musicbox", {"title": "Bad", "priority": "urgent"})

    def test_ideas_sort_by_priority_then_recency(self):
        low = services.create_idea("musicbox", {"title": "low", "priority": "low"})
        high = services.create_idea("musicbox", {"title": "high", "priority": "high"})
        med_old = services.create_idea("musicbox", {"title": "med old"})
        med_new = services.create_idea("musicbox", {"title": "med new"})
        order = [i["id"] for i in services.list_ideas(project="musicbox")]
        self.assertEqual(order, [high["id"], med_new["id"], med_old["id"], low["id"]])
        # Touching the low idea does not lift it over a higher priority.
        services.update_idea(low["id"], {"notes": "touched"})
        order = [i["id"] for i in services.list_ideas(project="musicbox")]
        self.assertEqual(order[0], high["id"])
        self.assertEqual(order[-1], low["id"])
