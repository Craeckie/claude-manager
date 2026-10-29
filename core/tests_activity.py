import json
import tempfile
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import activity, services
from .models import Event, Idea, Project


def _rpc_tool(client, name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return client.post("/mcp/", data=json.dumps(payload), content_type="application/json")


class RecordingTests(TestCase):
    """What lands in the log, by write path. Recording is done by signals, so every
    path is covered by the same code; these pin the source and the field diff."""

    def setUp(self):
        self.project = Project.objects.create(name="Alpha", folder="alpha")
        self.other = Project.objects.create(name="Beta", folder="beta")
        self.idea = Idea.objects.create(project=self.project, title="Thing")

    def test_writes_outside_a_request_are_not_recorded(self):
        # The setUp writes ran with no source: management commands and tests stay silent.
        self.assertEqual(Event.objects.count(), 0)

    def test_mcp_update_and_transition_record_source_and_fields(self):
        _rpc_tool(self.client, "update_idea", {"idea_id": self.idea.pk, "plan": "1. Do it"})
        _rpc_tool(self.client, "transition_idea", {"idea_id": self.idea.pk, "target": "planned"})
        changed, moved = Event.objects.order_by("pk")
        self.assertEqual(changed.source, Event.Source.MCP)
        self.assertEqual(changed.action, Event.Action.IDEA_CHANGED)
        self.assertEqual(changed.fields, ["plan"])
        self.assertEqual(changed.idea_id, self.idea.pk)
        self.assertEqual(changed.idea_title, "Thing")
        self.assertEqual(changed.project_id, self.project.pk)
        self.assertEqual((moved.from_status, moved.to_status), ("new", "planned"))
        self.assertEqual(moved.fields, [])

    def test_api_writes_record_api_source(self):
        self.client.patch(
            f"/api/ideas/{self.idea.pk}/",
            data=json.dumps({"report": "Done."}),
            content_type="application/json",
        )
        self.client.post(
            "/api/projects/", data=json.dumps({"name": "Gamma", "folder": "gamma"}),
            content_type="application/json",
        )
        changed, created = Event.objects.order_by("pk")
        self.assertEqual(changed.source, Event.Source.API)
        self.assertEqual(changed.fields, ["report"])
        self.assertEqual(created.action, Event.Action.PROJECT_CREATED)
        self.assertEqual(created.project_name, "Gamma")

    def test_unchanged_update_records_nothing(self):
        self.idea.plan = "same"
        self.idea.save()
        _rpc_tool(self.client, "update_idea", {"idea_id": self.idea.pk, "plan": "same"})
        self.assertEqual(Event.objects.count(), 0)

    def test_move_records_project_field_and_detail(self):
        _rpc_tool(self.client, "update_idea", {"idea_id": self.idea.pk, "project": "beta"})
        event = Event.objects.get()
        self.assertEqual(event.fields, ["project"])
        self.assertEqual(event.project_id, self.other.pk)
        self.assertEqual(event.detail, "Alpha → Beta")

    def test_web_edit_transition_and_create_record_web_source(self):
        self.client.post(
            reverse("core:idea_edit", args=[self.idea.pk]),
            {"project": self.project.pk, "title": "Thing", "kind": "other", "goal": "New goal"},
        )
        self.client.post(reverse("core:idea_transition", args=[self.idea.pk]), {"target": "planned"})
        self.client.post(reverse("core:idea_new", args=[self.project.pk]), {"title": "Fresh"})
        edited, moved, created = Event.objects.order_by("pk")
        self.assertEqual({e.source for e in (edited, moved, created)}, {Event.Source.WEB})
        self.assertEqual(edited.fields, ["goal"])
        self.assertEqual(moved.to_status, "planned")
        self.assertEqual(created.action, Event.Action.IDEA_CREATED)
        self.assertEqual(created.idea_title, "Fresh")

    def test_git_status_sync_is_not_activity(self):
        with activity.source(Event.Source.API):
            self.project.git_status = Project.GitStatus.DIRTY
            self.project.git_detail = "1 modified"
            self.project.save()
        self.assertEqual(Event.objects.count(), 0)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_artifact_upload_and_delete(self):
        upload = SimpleUploadedFile("out.log", b"hello", content_type="text/plain")
        self.client.post(f"/api/ideas/{self.idea.pk}/artifacts/", {"file": upload})
        artifact = self.idea.artifacts.get()
        self.client.delete(f"/api/artifacts/{artifact.pk}/")
        added, removed = Event.objects.order_by("pk")
        self.assertEqual(added.action, Event.Action.ARTIFACT_ADDED)
        self.assertEqual(added.detail, "out.log")
        self.assertEqual(removed.action, Event.Action.ARTIFACT_DELETED)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_deleting_an_idea_with_files_does_not_break(self):
        # Cascade-deleting the files fires their post_delete inside the idea's own
        # delete; the event must not need the idea row to still exist.
        services.add_screenshot(self.idea.pk, "a.png", b"\x89PNG")
        with activity.source(Event.Source.ADMIN):
            self.idea.delete()
        self.assertTrue(Event.objects.filter(action=Event.Action.SCREENSHOT_DELETED).exists())


class GroupingTests(TestCase):
    def setUp(self):
        self.t0 = timezone.now() - timedelta(hours=1)

    def event(self, minutes, idea_id=1, source=Event.Source.MCP, **kw):
        kw.setdefault("action", Event.Action.IDEA_CHANGED)
        return Event(
            created_at=self.t0 + timedelta(minutes=minutes),
            source=source,
            idea_id=idea_id,
            idea_title=f"Idea {idea_id}",
            project_id=1,
            project_name="Alpha",
            **kw,
        )

    def test_plan_update_and_transition_are_one_entry(self):
        groups = activity.group_events([
            self.event(0, fields=["plan"]),
            self.event(1, fields=[], from_status="new", to_status="planned"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].summary, ["plan edited", "new → planned"])

    def test_other_source_in_between_splits(self):
        groups = activity.group_events([
            self.event(0, fields=["plan"]),
            self.event(1, fields=["goal"], source=Event.Source.WEB),
            self.event(2, fields=[], from_status="new", to_status="planned"),
        ])
        self.assertEqual(len(groups), 3)

    def test_gap_over_window_splits(self):
        groups = activity.group_events([
            self.event(0, fields=["plan"]),
            self.event(activity.GROUP_WINDOW_MINUTES + 1, fields=["plan"]),
        ])
        self.assertEqual(len(groups), 2)

    def test_interleaved_ideas_still_group(self):
        groups = activity.group_events([
            self.event(0, idea_id=1, fields=["plan"]),
            self.event(1, idea_id=2, fields=["plan"]),
            self.event(2, idea_id=1, fields=[], from_status="new", to_status="planned"),
        ])
        self.assertEqual(len(groups), 2)
        # Newest-ending group first.
        self.assertEqual(groups[0].idea_id, 1)
        self.assertEqual(groups[0].summary, ["plan edited", "new → planned"])

    def test_summary_folds_repeats_and_counts_files(self):
        groups = activity.group_events([
            self.event(0, fields=["report", "commits"]),
            self.event(1, fields=["report"]),
            self.event(2, action=Event.Action.SCREENSHOT_ADDED, detail="a.png"),
            self.event(3, action=Event.Action.SCREENSHOT_ADDED, detail="b.png"),
            self.event(4, fields=[], from_status="planned", to_status="verify"),
        ])
        self.assertEqual(
            groups[0].summary, ["report, commits edited", "2 screenshots added", "planned → verify"]
        )

    def test_status_chain_collapses(self):
        groups = activity.group_events([
            self.event(0, from_status="new", to_status="planned"),
            self.event(1, from_status="planned", to_status="verify"),
        ])
        self.assertEqual(groups[0].summary, ["new → planned → verify"])


class ActivityPageTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Alpha", folder="alpha")
        self.other = Project.objects.create(name="Beta", folder="beta")
        self.idea = Idea.objects.create(project=self.project, title="Agent work")
        self.mine = Idea.objects.create(project=self.other, title="My work")
        _rpc_tool(self.client, "update_idea", {"idea_id": self.idea.pk, "plan": "1. x"})
        _rpc_tool(self.client, "transition_idea", {"idea_id": self.idea.pk, "target": "planned"})
        self.client.post(reverse("core:idea_transition", args=[self.mine.pk]), {"target": "planned"})

    def test_page_shows_grouped_entries_with_links(self):
        html = self.client.get(reverse("core:activity")).content.decode()
        self.assertEqual(html.count('class="activity-entry'), 2)
        self.assertIn(f'href="{self.idea.detail_url}"', html)
        self.assertIn("plan edited", html)
        self.assertIn("Agent · MCP", html)
        self.assertIn("You", html)

    def test_who_filter(self):
        html = self.client.get(reverse("core:activity"), {"who": "agents"}).content.decode()
        self.assertIn("Agent work", html)
        self.assertNotIn("My work", html)
        html = self.client.get(reverse("core:activity"), {"who": "me"}).content.decode()
        self.assertNotIn("Agent work", html)
        self.assertIn("My work", html)

    def test_project_filter_by_folder(self):
        html = self.client.get(reverse("core:activity"), {"project": "beta"}).content.decode()
        self.assertIn("My work", html)
        self.assertNotIn("Agent work", html)

    def test_htmx_request_gets_the_partial(self):
        response = self.client.get(reverse("core:activity"), HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "<html")
        self.assertContains(response, "activity-entry")

    def test_shows_current_title_after_rename(self):
        self.idea.title = "Renamed"
        self.idea.save()
        html = self.client.get(reverse("core:activity")).content.decode()
        self.assertIn("Renamed", html)

    def test_reading_records_nothing(self):
        before = Event.objects.count()
        self.client.get(reverse("core:activity"))
        _rpc_tool(self.client, "get_idea", {"idea_id": self.idea.pk})
        self.assertEqual(Event.objects.count(), before)

    @override_settings(DISPLAY_TIME_ZONE="Asia/Kolkata")
    def test_times_are_shown_in_the_display_time_zone(self):
        # +05:30, so a UTC clock can never match it by accident.
        event = Event.objects.latest("created_at")
        local = event.created_at.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
        utc = event.created_at.strftime("%H:%M")
        html = self.client.get(reverse("core:activity")).content.decode()
        self.assertIn(f">{local}</time>", html)
        self.assertNotIn(f">{utc}</time>", html)

    def test_nav_links_to_activity(self):
        self.assertContains(self.client.get(reverse("core:home")), f'href="{reverse("core:activity")}"')
