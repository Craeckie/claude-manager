"""Tests for the 0004 data migration that merges idea/caveats/verification into notes.

Runs against the historical model state via Django's MigrationExecutor, per the plan:
never import core.models here, only apps.get_model.
"""

from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TransactionTestCase


class MergeIdeaFieldsIntoNotesTests(TransactionTestCase):
    migrate_from = ("core", "0003_idea_plan")
    migrate_to = ("core", "0004_merge_idea_fields_into_notes")

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])

        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Project = old_apps.get_model("core", "Project")
        Idea = old_apps.get_model("core", "Idea")

        self.project = Project.objects.create(name="Proj", folder="proj")

        self.all_three = Idea.objects.create(
            project=self.project,
            title="All three",
            idea="The idea text",
            caveats="Mind the gap",
            verification="Run the tests",
            notes="Existing notes",
        )
        self.only_caveats = Idea.objects.create(
            project=self.project,
            title="Only caveats",
            idea="",
            caveats="Mind the gap",
            verification="",
            notes="",
        )
        self.none_set = Idea.objects.create(
            project=self.project,
            title="None set",
            idea="",
            caveats="",
            verification="",
            notes="Untouched notes",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.new_apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        # Leave the database on the latest migration state for other tests.
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])

    def _notes_for(self, title):
        Idea = self.new_apps.get_model("core", "Idea")
        return Idea.objects.get(title=title).notes

    def test_all_three_fields_are_merged_in_order_before_existing_notes(self):
        self.assertEqual(
            self._notes_for("All three"),
            "Idea:\nThe idea text\n\n"
            "Caveats:\nMind the gap\n\n"
            "Verification:\nRun the tests\n\n"
            "Existing notes",
        )

    def test_only_the_set_field_gets_a_heading(self):
        self.assertEqual(self._notes_for("Only caveats"), "Caveats:\nMind the gap")

    def test_idea_with_all_three_blank_is_left_untouched(self):
        self.assertEqual(self._notes_for("None set"), "Untouched notes")


class NeedsVerificationMigrationTests(TransactionTestCase):
    # Ideas that existed before the field keep verification; only new ones skip it.
    migrate_from = ("core", "0011_event")
    migrate_to = ("core", "0012_idea_needs_verification")

    def test_existing_ideas_keep_verification_and_new_ones_skip_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        project = old_apps.get_model("core", "Project").objects.create(name="Proj", folder="proj")
        old_apps.get_model("core", "Idea").objects.create(project=project, title="Old")

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        Idea = executor.loader.project_state([self.migrate_to]).apps.get_model("core", "Idea")
        self.assertTrue(Idea.objects.get(title="Old").needs_verification)
        self.assertFalse(Idea.objects.create(project_id=project.pk, title="New").needs_verification)


class PriorityMigrationTests(TransactionTestCase):
    # Ideas that existed before the field become medium.
    migrate_from = ("core", "0012_idea_needs_verification")
    migrate_to = ("core", "0013_idea_priority")

    def test_existing_ideas_become_medium(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        project = old_apps.get_model("core", "Project").objects.create(name="Proj", folder="proj")
        old_apps.get_model("core", "Idea").objects.create(project=project, title="Old")

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        Idea = executor.loader.project_state([self.migrate_to]).apps.get_model("core", "Idea")
        self.assertEqual(Idea.objects.get(title="Old").priority, "medium")
