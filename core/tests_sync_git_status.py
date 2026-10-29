"""Tests for the sync_git_status command's status detection."""

import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from .services import _check_project_git_status
from .models import Project


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


class CheckProjectGitStatusTests(TestCase):
    def test_missing_git_dir_is_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, detail = _check_project_git_status(tmp)
        self.assertEqual(status, Project.GitStatus.NOT_A_REPO)
        self.assertIn(".git", detail)

    def test_no_commits_and_no_upstream_is_no_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            status, _ = _check_project_git_status(tmp)
        self.assertEqual(status, Project.GitStatus.NO_REMOTE)

    def test_clean_repo_with_upstream(self):
        with tempfile.TemporaryDirectory() as base:
            remote = Path(base) / "remote.git"
            work = Path(base) / "work"
            _git("init", "-q", "--bare", str(remote), cwd=base)
            work.mkdir()
            _init_repo(work)
            (work / "f.txt").write_text("hi\n")
            _git("add", "f.txt", cwd=work)
            _git("commit", "-q", "-m", "initial", cwd=work)
            _git("branch", "-M", "main", cwd=work)
            _git("remote", "add", "origin", str(remote), cwd=work)
            _git("push", "-q", "-u", "origin", "main", cwd=work)

            status, detail = _check_project_git_status(work)
        self.assertEqual(status, Project.GitStatus.CLEAN)
        self.assertEqual(detail, "")

    def test_dirty_repo_reports_uncommitted_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            Path(tmp, "f.txt").write_text("hi\n")
            Path(tmp, "g.txt").write_text("there\n")
            status, detail = _check_project_git_status(tmp)
        self.assertEqual(status, Project.GitStatus.DIRTY)
        self.assertIn("2 uncommitted", detail)

    def test_ahead_of_upstream(self):
        with tempfile.TemporaryDirectory() as base:
            remote = Path(base) / "remote.git"
            work = Path(base) / "work"
            _git("init", "-q", "--bare", str(remote), cwd=base)
            work.mkdir()
            _init_repo(work)
            (work / "f.txt").write_text("hi\n")
            _git("add", "f.txt", cwd=work)
            _git("commit", "-q", "-m", "initial", cwd=work)
            _git("branch", "-M", "main", cwd=work)
            _git("remote", "add", "origin", str(remote), cwd=work)
            _git("push", "-q", "-u", "origin", "main", cwd=work)

            (work / "f.txt").write_text("more\n")
            _git("commit", "-q", "-am", "second", cwd=work)

            status, detail = _check_project_git_status(work)
        self.assertEqual(status, Project.GitStatus.AHEAD)
        self.assertIn("1 unpushed", detail)


class SyncGitStatusCommandTests(TestCase):
    def test_updates_project_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            project = Project.objects.create(name="P", folder="p")

            with mock.patch.object(Project, "root_path", new=property(lambda self: tmp)):
                out = StringIO()
                call_command("sync_git_status", stdout=out)

            project.refresh_from_db()
        self.assertEqual(project.git_status, Project.GitStatus.NO_REMOTE)
        self.assertIsNotNone(project.git_checked_at)
        self.assertIn("p:", out.getvalue())
