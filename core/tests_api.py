import json
import re
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import services
from .models import Idea, Project
from .urls_api import urlpatterns


class ApiTestCase(TestCase):
    """Shared helpers: the API is JSON in, JSON out, with no auth."""

    def post_json(self, url, data=None, content_type="application/json"):
        body = json.dumps(data) if data is not None else ""
        return self.client.post(url, data=body, content_type=content_type)

    def patch_json(self, url, data=None, content_type="application/json"):
        body = json.dumps(data) if data is not None else ""
        return self.client.patch(url, data=body, content_type=content_type)


class ProjectListViewTests(ApiTestCase):
    def test_get_lists_projects(self):
        Project.objects.create(name="Proj A", folder="a")
        Project.objects.create(name="Proj B", folder="b")
        response = self.client.get(reverse("api:project_list"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual({p["folder"] for p in data}, {"a", "b"})

    def test_get_filters_by_status(self):
        Project.objects.create(name="Proj A", folder="a", status=Project.Status.IDEA)
        Project.objects.create(name="Proj B", folder="b", status=Project.Status.IMPLEMENTED)
        response = self.client.get(reverse("api:project_list"), {"status": "implemented"})
        data = response.json()
        self.assertEqual([p["folder"] for p in data], ["b"])

    def test_get_bad_status_is_400(self):
        response = self.client.get(reverse("api:project_list"), {"status": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_post_creates_project(self):
        response = self.post_json(
            reverse("api:project_list"),
            {"name": "New Proj", "folder": "new-proj", "description": "desc"},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["name"], "New Proj")
        self.assertTrue(Project.objects.filter(folder="new-proj").exists())

    def test_post_validation_failure_is_422(self):
        response = self.post_json(reverse("api:project_list"), {"folder": "no-name"})
        self.assertEqual(response.status_code, 422)
        errors = response.json()["errors"]
        self.assertIn("name", errors)

    def test_post_unknown_field_is_400(self):
        response = self.post_json(
            reverse("api:project_list"), {"name": "X", "folder": "x", "nope": "y"}
        )
        self.assertEqual(response.status_code, 400)

    def test_post_status_in_body_is_400(self):
        response = self.post_json(
            reverse("api:project_list"), {"name": "X", "folder": "x", "status": "implemented"}
        )
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_is_400_not_500(self):
        response = self.client.post(
            reverse("api:project_list"), data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_non_object_json_body_is_400(self):
        response = self.client.post(
            reverse("api:project_list"), data="[1, 2]", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_wrong_method_is_405_with_allow_header(self):
        response = self.client.delete(reverse("api:project_list"))
        self.assertEqual(response.status_code, 405)
        self.assertIn("GET", response["Allow"])
        self.assertIn("POST", response["Allow"])
        self.assertIn("error", response.json())


class ProjectDetailViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj", description="d")

    def test_get_returns_project(self):
        response = self.client.get(reverse("api:project_detail", args=[self.project.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folder"], "proj")

    def test_get_missing_is_404(self):
        response = self.client.get(reverse("api:project_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json())

    def test_patch_updates_single_field_leaves_others(self):
        response = self.patch_json(
            reverse("api:project_detail", args=[self.project.pk]), {"name": "Renamed"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Renamed")
        self.assertEqual(data["folder"], "proj")
        self.assertEqual(data["description"], "d")
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Renamed")
        self.assertEqual(self.project.folder, "proj")

    def test_patch_validation_failure_is_422(self):
        other = Project.objects.create(name="Other", folder="other")
        response = self.patch_json(
            reverse("api:project_detail", args=[self.project.pk]), {"name": other.name}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("errors", response.json())

    def test_wrong_method_is_405(self):
        response = self.client.delete(reverse("api:project_detail", args=[self.project.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertIn("GET", response["Allow"])
        self.assertIn("PATCH", response["Allow"])


class ProjectTransitionViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_legal_transition_succeeds(self):
        response = self.post_json(
            reverse("api:project_transition", args=[self.project.pk]), {"target": "defined"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "defined")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "defined")

    def test_illegal_transition_is_400_and_unchanged(self):
        response = self.post_json(
            reverse("api:project_transition", args=[self.project.pk]), {"target": "discontinued"}
        )
        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "idea")

    def test_missing_project_is_404(self):
        response = self.post_json(
            reverse("api:project_transition", args=[999999]), {"target": "defined"}
        )
        self.assertEqual(response.status_code, 404)

    def test_wrong_method_is_405(self):
        response = self.client.get(reverse("api:project_transition", args=[self.project.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertIn("POST", response["Allow"])


class SyncGitStatusViewTests(ApiTestCase):
    def test_no_projects_returns_empty_list(self):
        response = self.post_json(reverse("api:sync_git_status"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_updates_project_git_status(self):
        import subprocess
        from unittest import mock

        project = Project.objects.create(name="P", folder="p")
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True)
            with mock.patch.object(Project, "root_path", new=property(lambda self: tmp)):
                response = self.post_json(reverse("api:sync_git_status"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["folder"], "p")
        project.refresh_from_db()
        self.assertEqual(project.git_status, Project.GitStatus.NO_REMOTE)

    def test_wrong_method_is_405(self):
        response = self.client.get(reverse("api:sync_git_status"))
        self.assertEqual(response.status_code, 405)
        self.assertIn("POST", response["Allow"])


class ProjectIdeasViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_get_lists_ideas_for_project(self):
        Idea.objects.create(project=self.project, title="One")
        other = Project.objects.create(name="Other", folder="other")
        Idea.objects.create(project=other, title="Two")
        response = self.client.get(reverse("api:project_ideas", args=[self.project.pk]))
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "One")

    def test_post_creates_idea_under_project(self):
        response = self.post_json(
            reverse("api:project_ideas", args=[self.project.pk]), {"title": "New Idea"}
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["title"], "New Idea")
        self.assertEqual(data["project_id"], self.project.pk)
        self.assertTrue(Idea.objects.filter(title="New Idea", project=self.project).exists())

    def test_post_missing_project_is_404(self):
        response = self.post_json(reverse("api:project_ideas", args=[999999]), {"title": "X"})
        self.assertEqual(response.status_code, 404)

    def test_post_creates_idea_with_kind(self):
        response = self.post_json(
            reverse("api:project_ideas", args=[self.project.pk]),
            {"title": "New Idea", "kind": "feature"},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["kind"], "feature")
        self.assertEqual(data["kind_display"], "Feature")


class IdeaListViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.other = Project.objects.create(name="Other", folder="other")

    def test_lists_all_ideas(self):
        Idea.objects.create(project=self.project, title="A")
        Idea.objects.create(project=self.other, title="B")
        response = self.client.get(reverse("api:idea_list"))
        self.assertEqual(len(response.json()), 2)

    def test_filter_by_project_id(self):
        Idea.objects.create(project=self.project, title="A")
        Idea.objects.create(project=self.other, title="B")
        response = self.client.get(reverse("api:idea_list"), {"project": self.project.pk})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["A"])

    def test_filter_by_project_folder(self):
        Idea.objects.create(project=self.project, title="A")
        Idea.objects.create(project=self.other, title="B")
        response = self.client.get(reverse("api:idea_list"), {"project": "other"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["B"])

    def test_filter_by_unknown_project_is_404(self):
        response = self.client.get(reverse("api:idea_list"), {"project": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_filter_by_status(self):
        Idea.objects.create(project=self.project, title="A", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="B", status=Idea.Status.PLANNED)
        response = self.client.get(reverse("api:idea_list"), {"status": "planned"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["B"])

    def test_bad_status_is_400(self):
        response = self.client.get(reverse("api:idea_list"), {"status": "bogus"})
        self.assertEqual(response.status_code, 400)

    def test_open_filter_excludes_closed_ideas(self):
        Idea.objects.create(project=self.project, title="Open", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Closed", status=Idea.Status.IMPLEMENTED)
        response = self.client.get(reverse("api:idea_list"), {"open": "1"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["Open"])

    def test_open_filter_accepts_true_case_insensitive(self):
        Idea.objects.create(project=self.project, title="Open", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Closed", status=Idea.Status.ABANDONED)
        response = self.client.get(reverse("api:idea_list"), {"open": "TRUE"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["Open"])

    def test_open_filter_falsy_value_includes_everything(self):
        Idea.objects.create(project=self.project, title="Open", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Closed", status=Idea.Status.ABANDONED)
        response = self.client.get(reverse("api:idea_list"), {"open": "0"})
        self.assertEqual(len(response.json()), 2)

    def test_filter_by_kind(self):
        Idea.objects.create(project=self.project, title="A", kind=Idea.Kind.FIX)
        Idea.objects.create(project=self.project, title="B", kind=Idea.Kind.FEATURE)
        response = self.client.get(reverse("api:idea_list"), {"kind": "fix"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["A"])

    def test_bad_kind_is_400(self):
        response = self.client.get(reverse("api:idea_list"), {"kind": "bogus"})
        self.assertEqual(response.status_code, 400)

    def test_filter_by_commit(self):
        Idea.objects.create(project=self.project, title="A", commits=["abc123def456"])
        Idea.objects.create(project=self.project, title="B", commits=[])
        response = self.client.get(reverse("api:idea_list"), {"commit": "abc123"})
        data = response.json()
        self.assertEqual([i["title"] for i in data], ["A"])

    def test_filter_by_text(self):
        Idea.objects.create(project=self.project, title="A", plan="Swap the codec")
        Idea.objects.create(project=self.project, title="B")
        response = self.client.get(reverse("api:idea_list"), {"q": "CODEC"})
        self.assertEqual([i["title"] for i in response.json()], ["A"])

    def test_commit_too_short_is_400(self):
        response = self.client.get(reverse("api:idea_list"), {"commit": "abc"})
        self.assertEqual(response.status_code, 400)


class IdeaDetailViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="An idea", goal="g")

    def test_get_returns_idea_with_prompt_and_transitions(self):
        response = self.client.get(reverse("api:idea_detail", args=[self.idea.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["title"], "An idea")
        self.assertIn("prompt", data)
        self.assertIn("allowed_transitions", data)

    def test_get_missing_is_404(self):
        response = self.client.get(reverse("api:idea_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_patch_single_field_leaves_others_intact(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"goal": "new goal"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["goal"], "new goal")
        self.assertEqual(data["title"], "An idea")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.title, "An idea")
        self.assertEqual(self.idea.goal, "new goal")

    def test_patch_validation_failure_is_422(self):
        response = self.patch_json(reverse("api:idea_detail", args=[self.idea.pk]), {"title": ""})
        self.assertEqual(response.status_code, 422)
        self.assertIn("title", response.json()["errors"])

    def test_patch_kind_round_trips(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"kind": "fix"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["kind"], "fix")
        self.assertEqual(data["kind_display"], "Fix")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.kind, "fix")

    def test_patch_review_round_trips(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"review": "Fix the edge case"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["review"], "Fix the edge case")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.review, "Fix the edge case")

    def test_patch_unknown_field_is_400(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"titel": "typo"}
        )
        self.assertEqual(response.status_code, 400)

    def test_wrong_method_is_405(self):
        response = self.client.delete(reverse("api:idea_detail", args=[self.idea.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertIn("GET", response["Allow"])
        self.assertIn("PATCH", response["Allow"])


class IdeaTransitionViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="An idea")

    def test_legal_transition_succeeds(self):
        response = self.post_json(
            reverse("api:idea_transition", args=[self.idea.pk]), {"target": "planned"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "planned")

    def test_illegal_transition_is_400_and_unchanged(self):
        response = self.post_json(
            reverse("api:idea_transition", args=[self.idea.pk]), {"target": "implemented"}
        )
        self.assertEqual(response.status_code, 400)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.status, "new")

    def test_missing_idea_is_404(self):
        response = self.post_json(reverse("api:idea_transition", args=[999999]), {"target": "planned"})
        self.assertEqual(response.status_code, 404)

    def test_planned_moves_to_verify_not_implemented(self):
        self.idea.status = "planned"
        self.idea.needs_verification = True
        self.idea.save()
        response = self.post_json(
            reverse("api:idea_transition", args=[self.idea.pk]), {"target": "verify"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "verify")


    def test_verify_closes_an_idea_without_verification(self):
        self.idea.status = "planned"
        self.idea.save()
        response = self.post_json(
            reverse("api:idea_transition", args=[self.idea.pk]), {"target": "verify"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "implemented")


class IdeaScreenshotViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="An idea")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def test_multipart_upload_attaches_and_returns_the_idea(self):
        upload = SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png")
        response = self.client.post(
            reverse("api:idea_screenshots", args=[self.idea.pk]),
            {"file": upload, "caption": "Before the fix"},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(len(data["screenshots"]), 1)
        self.assertEqual(data["screenshots"][0]["caption"], "Before the fix")

    def test_oversized_file_is_400(self):
        # Refused from the upload's declared size, before its bytes are read into
        # memory -- this endpoint is unauthenticated.
        big = SimpleUploadedFile(
            "big.png", b"x" * (services.SCREENSHOT_MAX_BYTES + 1), content_type="image/png"
        )
        response = self.client.post(
            reverse("api:idea_screenshots", args=[self.idea.pk]), {"file": big}
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_file_part_is_400(self):
        response = self.client.post(reverse("api:idea_screenshots", args=[self.idea.pk]), {})
        self.assertEqual(response.status_code, 400)

    def test_bad_extension_is_400(self):
        upload = SimpleUploadedFile("shot.exe", b"whatever", content_type="application/octet-stream")
        response = self.client.post(
            reverse("api:idea_screenshots", args=[self.idea.pk]), {"file": upload}
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_idea_is_404(self):
        upload = SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png")
        response = self.client.post(
            reverse("api:idea_screenshots", args=[999999]), {"file": upload}
        )
        self.assertEqual(response.status_code, 404)

    def test_get_is_405(self):
        response = self.client.get(reverse("api:idea_screenshots", args=[self.idea.pk]))
        self.assertEqual(response.status_code, 405)

    def test_delete_removes_the_screenshot(self):
        upload = SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png")
        create = self.client.post(
            reverse("api:idea_screenshots", args=[self.idea.pk]), {"file": upload}
        )
        screenshot_id = create.json()["screenshots"][0]["id"]

        response = self.client.delete(reverse("api:screenshot_detail", args=[screenshot_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["screenshots"], [])

    def test_delete_missing_screenshot_is_404(self):
        response = self.client.delete(reverse("api:screenshot_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_post_on_screenshot_detail_is_405(self):
        response = self.client.post(reverse("api:screenshot_detail", args=[999999]))
        self.assertEqual(response.status_code, 405)


class OpenApiSchemaTests(TestCase):
    # Django's <int:pk> vs. OpenAPI's {pk} -- translate one converter syntax to
    # the other so route names from the URLconf can be compared to path keys.
    def _urlconf_paths(self):
        paths = set()
        for pattern in urlpatterns:
            route = "/api/" + str(pattern.pattern)
            route = re.sub(r"<int:(\w+)>", r"{\1}", route)
            paths.add(route)
        return paths

    def test_returns_valid_json_document(self):
        response = self.client.get(reverse("api:openapi"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["openapi"].startswith("3.1"))

    def test_documents_every_route_except_the_doc_routes_themselves(self):
        response = self.client.get(reverse("api:openapi"))
        data = response.json()
        documented = set(data["paths"])

        expected = self._urlconf_paths() - {"/api/openapi.json", "/api/docs/"}
        self.assertTrue(expected, "sanity check: the URLconf import should not be empty")
        self.assertEqual(documented, expected)

    def test_servers_reflect_the_request_host(self):
        response = self.client.get(reverse("api:openapi"))
        data = response.json()
        self.assertTrue(data["servers"][0]["url"])
        self.assertFalse(data["servers"][0]["url"].endswith("/"))

    def test_idea_kind_enum_matches_the_model(self):
        response = self.client.get(reverse("api:openapi"))
        data = response.json()
        kind_enum = data["components"]["schemas"]["Idea"]["properties"]["kind"]["enum"]
        self.assertEqual(set(kind_enum), set(Idea.Kind.values))


class ApiDocsViewTests(TestCase):
    def test_returns_200_and_references_vendored_assets(self):
        response = self.client.get(reverse("api:docs"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("vendor/swagger-ui/swagger-ui.css", content)
        self.assertIn("vendor/swagger-ui/swagger-ui-bundle.js", content)

    def test_no_external_cdn_reference(self):
        response = self.client.get(reverse("api:docs"))
        content = response.content.decode().lower()
        self.assertNotIn("cdn", content)
        self.assertNotIn("unpkg", content)
        self.assertNotIn("http://", content)


class IdeaArtifactViewTests(ApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="An idea")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def test_multipart_upload_attaches_and_returns_the_idea(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        response = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]),
            {"file": upload, "caption": "Spec"},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(len(data["artifacts"]), 1)
        self.assertEqual(data["artifacts"][0]["caption"], "Spec")
        self.assertEqual(data["artifacts"][0]["filename"], "notes.md")

    def test_binary_artifact_uploads_too(self):
        upload = SimpleUploadedFile("app.apk", b"fake-apk-bytes", content_type="application/vnd.android.package-archive")
        response = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json()["artifacts"]), 1)

    def test_oversized_file_is_400(self):
        big = SimpleUploadedFile(
            "big.log", b"x" * (services.ARTIFACT_MAX_BYTES + 1), content_type="text/plain"
        )
        response = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": big}
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_file_part_is_400(self):
        response = self.client.post(reverse("api:idea_artifacts", args=[self.idea.pk]), {})
        self.assertEqual(response.status_code, 400)

    def test_disallowed_extension_is_400(self):
        upload = SimpleUploadedFile("virus.exe", b"whatever", content_type="application/octet-stream")
        response = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_idea_is_404(self):
        upload = SimpleUploadedFile("notes.md", b"hi", content_type="text/markdown")
        response = self.client.post(
            reverse("api:idea_artifacts", args=[999999]), {"file": upload}
        )
        self.assertEqual(response.status_code, 404)

    def test_get_on_idea_artifacts_is_405(self):
        response = self.client.get(reverse("api:idea_artifacts", args=[self.idea.pk]))
        self.assertEqual(response.status_code, 405)

    def test_get_returns_metadata_without_content_by_default(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        create = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        artifact_id = create.json()["artifacts"][0]["id"]

        response = self.client.get(reverse("api:artifact_detail", args=[artifact_id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("content", data)
        self.assertEqual(data["filename"], "notes.md")

    def test_get_with_content_flag_returns_text_content(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        create = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        artifact_id = create.json()["artifacts"][0]["id"]

        response = self.client.get(
            reverse("api:artifact_detail", args=[artifact_id]), {"content": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "# Notes")

    def test_get_with_content_flag_omits_content_for_binary_artifact(self):
        upload = SimpleUploadedFile("app.apk", b"fake-apk-bytes", content_type="application/vnd.android.package-archive")
        create = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        artifact_id = create.json()["artifacts"][0]["id"]

        response = self.client.get(
            reverse("api:artifact_detail", args=[artifact_id]), {"content": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content", response.json())

    def test_get_missing_artifact_is_404(self):
        response = self.client.get(reverse("api:artifact_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_the_artifact(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        create = self.client.post(
            reverse("api:idea_artifacts", args=[self.idea.pk]), {"file": upload}
        )
        artifact_id = create.json()["artifacts"][0]["id"]

        response = self.client.delete(reverse("api:artifact_detail", args=[artifact_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["artifacts"], [])

    def test_delete_missing_artifact_is_404(self):
        response = self.client.delete(reverse("api:artifact_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_post_on_artifact_detail_is_405(self):
        response = self.client.post(reverse("api:artifact_detail", args=[999999]))
        self.assertEqual(response.status_code, 405)


class IdeaMoveApiTests(ApiTestCase):
    def setUp(self):
        self.source = Project.objects.create(name="Source", folder="source")
        self.target = Project.objects.create(name="Target", folder="target")
        self.idea = Idea.objects.create(project=self.source, title="An idea")

    def test_patch_project_by_folder_moves_the_idea(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"project": "target"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], self.idea.pk)
        self.assertEqual(data["project_id"], self.target.pk)
        self.assertEqual(data["project_folder"], "target")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.target.pk)

    def test_patch_project_by_id_moves_the_idea(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"project": self.target.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.target.pk)

    def test_patch_unknown_project_is_404(self):
        response = self.patch_json(
            reverse("api:idea_detail", args=[self.idea.pk]), {"project": "nope"}
        )
        self.assertEqual(response.status_code, 404)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, self.source.pk)

    def test_post_ideas_rejects_project_in_body(self):
        response = self.post_json(
            reverse("api:project_ideas", args=[self.source.pk]),
            {"title": "T", "project": "target"},
        )
        self.assertEqual(response.status_code, 400)

    def test_openapi_update_body_lists_project_but_create_body_does_not(self):
        data = self.client.get(reverse("api:openapi")).json()
        idea_detail = data["paths"]["/api/ideas/{pk}/"]["patch"]
        patch_props = idea_detail["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertIn("project", patch_props)
        project_ideas = data["paths"]["/api/projects/{pk}/ideas/"]["post"]
        post_props = project_ideas["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertNotIn("project", post_props)


class CompactIdeaListApiTests(TestCase):
    """#244: the API stays full by default; ?full=0 opts into compact rows."""

    def setUp(self):
        project = Project.objects.create(name="MusicBox", folder="MusicBox")
        Idea.objects.create(project=project, title="Add shuffle", plan="1. Step")

    def test_default_is_full(self):
        data = self.client.get("/api/ideas/").json()
        self.assertIn("prompt", data[0])

    def test_full_0_returns_rows(self):
        data = self.client.get("/api/ideas/?full=0").json()
        self.assertNotIn("prompt", data[0])
        self.assertTrue(data[0]["has_plan"])
