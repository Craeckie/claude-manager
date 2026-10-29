"""Tests for the seed_demo command and the local-only guard both seeders share."""

import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .management import local_only
from .management.commands.seed_demo import DEMO_FOLDERS, fake_screenshot_png
from . import activity
from .models import Artifact, Event, Idea, Project, Screenshot


class LocalOnlyGuardTests(TestCase):
    def test_image_workdir_refuses(self):
        with override_settings(BASE_DIR=Path("/app")):
            with self.assertRaises(CommandError) as caught:
                local_only.refuse_outside_local_dev("seed_demo")
        self.assertIn("/app", str(caught.exception))

    def test_a_tree_without_git_metadata_refuses(self):
        # .dockerignore excludes .git/, so the image has none: an unpacked copy of the
        # source with no repository behind it is the image, or near enough.
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BASE_DIR=Path(tmp)):
                with self.assertRaises(CommandError) as caught:
                    local_only.refuse_outside_local_dev("seed_demo")
        self.assertIn("not a git checkout", str(caught.exception))

    def test_production_overlay_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / "claude_manager").mkdir()
            (Path(tmp) / "claude_manager" / "production.py").write_text("DEBUG = False\n")
            with override_settings(BASE_DIR=Path(tmp)):
                with self.assertRaises(CommandError) as caught:
                    local_only.refuse_outside_local_dev("seed_demo")
        self.assertIn("production.py", str(caught.exception))

    def test_a_worktree_is_still_a_checkout(self):
        # In a linked worktree .git is a file pointing at the parent repository, not a
        # directory -- the guard must not read that as "no repository".
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").write_text("gitdir: /elsewhere/.git/worktrees/demo\n")
            with override_settings(BASE_DIR=Path(tmp)):
                self.assertEqual(local_only.local_dev_refusals(), [])

    def test_non_sqlite_database_refuses(self):
        databases = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "live"}}
        with override_settings(DATABASES=databases):
            with self.assertRaises(CommandError) as caught:
                local_only.refuse_outside_local_dev("seed_demo")
        self.assertIn("postgresql", str(caught.exception))

    def test_local_dev_is_allowed(self):
        self.assertEqual(local_only.local_dev_refusals(), [])


class FakeScreenshotTests(TestCase):
    def test_is_a_png_of_the_expected_size(self):
        data = fake_screenshot_png()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        # Width and height live in the IHDR chunk, bytes 16..24.
        self.assertEqual(int.from_bytes(data[16:20], "big"), 480)
        self.assertEqual(int.from_bytes(data[20:24], "big"), 300)

    def test_accents_produce_different_images(self):
        self.assertNotEqual(fake_screenshot_png("blue"), fake_screenshot_png("red"))


class SeedDemoCommandTests(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def seed(self, *args):
        out = StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_seeds_every_idea_status(self):
        self.seed()
        statuses = set(Idea.objects.values_list("status", flat=True))
        self.assertEqual(statuses, set(Idea.Status.values))

    def test_seeds_every_idea_kind(self):
        self.seed()
        kinds = set(Idea.objects.values_list("kind", flat=True))
        self.assertEqual(kinds, set(Idea.Kind.values))

    def test_verify_ideas_carry_a_report_and_screenshots(self):
        self.seed()
        verifying = Idea.objects.filter(status=Idea.Status.VERIFY)
        self.assertTrue(verifying.exists())
        for idea in verifying:
            self.assertTrue(idea.report.strip(), f"{idea.title} has no report")
            self.assertTrue(idea.screenshots.exists(), f"{idea.title} has no screenshots")

    def test_stored_screenshots_are_readable_images(self):
        self.seed()
        screenshot = Screenshot.objects.first()
        with screenshot.image.open("rb") as fh:
            self.assertTrue(fh.read(8).startswith(b"\x89PNG"))

    def test_seeds_at_least_one_text_and_one_log_artifact(self):
        self.seed()
        self.assertTrue(Artifact.objects.exists())
        filenames = {a.filename for a in Artifact.objects.all()}
        self.assertTrue(any(name.endswith(".md") for name in filenames))
        self.assertTrue(any(name.endswith(".log") for name in filenames))

    def test_stored_artifacts_are_readable_text(self):
        self.seed()
        artifact = Artifact.objects.get(file__endswith="spec.md")
        with artifact.file.open("rb") as fh:
            self.assertIn(b"Offline cache", fh.read())

    def test_covers_the_long_plan_and_long_report_clamps(self):
        self.seed()
        self.assertTrue(Idea.objects.filter(status=Idea.Status.PLANNED).exists())
        self.assertTrue(any(i.plan_is_long for i in Idea.objects.all()))
        self.assertTrue(any(i.report_is_long for i in Idea.objects.all()))

    def test_includes_a_project_with_no_ideas(self):
        self.seed()
        self.assertTrue(Project.objects.filter(ideas__isnull=True).exists())

    def test_rerunning_replaces_rather_than_duplicates(self):
        self.seed()
        before = Idea.objects.count()
        self.seed()
        self.assertEqual(Idea.objects.count(), before)
        self.assertEqual(Project.objects.filter(folder__in=DEMO_FOLDERS).count(), len(DEMO_FOLDERS))

    def test_seeds_activity_that_groups(self):
        self.seed()
        count = Event.objects.count()
        self.assertGreater(count, 0)
        groups = activity.group_events(list(Event.objects.all()))
        self.assertLess(len(groups), count)
        self.assertIn(["plan edited", "new → planned"], [g.summary for g in groups])
        self.seed()
        self.assertEqual(Event.objects.count(), count)

    def test_leaves_other_projects_alone(self):
        keeper = Project.objects.create(name="Real work", folder="real-work")
        Idea.objects.create(project=keeper, title="Mine")
        self.seed()
        self.seed()
        self.assertTrue(Project.objects.filter(pk=keeper.pk).exists())
        self.assertEqual(keeper.ideas.count(), 1)

    def test_reset_removes_other_projects_too(self):
        Project.objects.create(name="Real work", folder="real-work")
        self.seed("--reset", "--noinput")
        self.assertFalse(Project.objects.filter(folder="real-work").exists())

    def test_reset_asks_before_deleting_other_projects(self):
        Project.objects.create(name="Real work", folder="real-work")
        with mock.patch("builtins.input", return_value="no"):
            self.seed("--reset")
        self.assertTrue(Project.objects.filter(folder="real-work").exists())
        self.assertFalse(Project.objects.filter(folder__in=DEMO_FOLDERS).exists())

    def test_reseeding_does_not_leave_orphaned_files_behind(self):
        self.seed()
        self.seed()
        stored = list(Path(self._tmpdir.name).rglob("*.png"))
        self.assertEqual(len(stored), Screenshot.objects.count())
