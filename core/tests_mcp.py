import base64
import json
import tempfile

from django.test import TestCase, override_settings

from .models import Idea, Project
from .transitions import IDEA_TRANSITIONS, PROJECT_TRANSITIONS

MCP_URL = "/mcp/"

TEN_TOOL_NAMES = {
    "list_projects",
    "get_project",
    "create_project",
    "update_project",
    "transition_project",
    "list_ideas",
    "get_idea",
    "create_idea",
    "update_idea",
    "transition_idea",
    "add_screenshot",
    "add_artifact",
    "read_artifact",
    "delete_artifact",
    "sync_git_status",
}


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return body


class MCPTestCase(TestCase):
    def post(self, payload):
        return self.client.post(MCP_URL, data=json.dumps(payload), content_type="application/json")

    def call_tool(self, name, arguments=None, id_=1):
        return self.post(rpc("tools/call", {"name": name, "arguments": arguments or {}}, id_=id_))

    def tool_result(self, name, arguments=None, id_=1):
        response = self.call_tool(name, arguments, id_=id_)
        body = response.json()
        result = body["result"]
        payload = json.loads(result["content"][0]["text"])
        return result, payload

    def read_back(self, idea_id):
        # Writes answer with an ack, not the record (#244), so a test that checks
        # what a write stored reads the full idea back, rendered prompt included.
        _, payload = self.tool_result("get_idea", {"idea_id": idea_id, "include_prompt": True})
        return payload


class InitializeTests(MCPTestCase):
    def test_handshake_returns_protocol_version_capabilities_and_server_info(self):
        response = self.post(rpc("initialize", {"protocolVersion": "2025-06-18"}))
        result = response.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], "claude-manager")
        self.assertIn("version", result["serverInfo"])

    def test_supported_version_is_echoed(self):
        for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
            response = self.post(rpc("initialize", {"protocolVersion": version}))
            self.assertEqual(response.json()["result"]["protocolVersion"], version)

    def test_unknown_version_falls_back_to_latest(self):
        response = self.post(rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-06-18")

    def test_missing_protocol_version_falls_back_to_latest(self):
        response = self.post(rpc("initialize", {}))
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-06-18")


class NotificationTests(MCPTestCase):
    def test_notification_gets_202_and_empty_body(self):
        response = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_unknown_notification_also_gets_202_and_empty_body(self):
        response = self.post({"jsonrpc": "2.0", "method": "notifications/whatever"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")


class PingTests(MCPTestCase):
    def test_ping_returns_empty_object(self):
        response = self.post(rpc("ping"))
        self.assertEqual(response.json()["result"], {})


class ToolsListTests(MCPTestCase):
    def test_returns_exactly_the_ten_tools(self):
        response = self.post(rpc("tools/list"))
        tools = response.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, TEN_TOOL_NAMES)
        self.assertEqual(len(tools), 15)

    def test_every_tool_has_description_and_valid_input_schema(self):
        response = self.post(rpc("tools/list"))
        tools = response.json()["result"]["tools"]
        for tool in tools:
            self.assertTrue(tool["description"].strip(), tool["name"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("required", schema)

    def test_get_idea_description_mentions_prompt_and_root_path(self):
        response = self.post(rpc("tools/list"))
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        description = tools["get_idea"]["description"]
        self.assertIn("prompt", description)
        self.assertIn("project_root_path", description)
        self.assertIn("cd", description)

    def test_transition_idea_description_mentions_implemented_and_allowed_transitions(self):
        response = self.post(rpc("tools/list"))
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        description = tools["transition_idea"]["description"]
        self.assertIn("implemented", description)
        self.assertIn("allowed_transitions", description)

    def test_status_and_target_enums_match_transitions_and_model_status(self):
        response = self.post(rpc("tools/list"))
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}

        project_status_enum = tools["list_projects"]["inputSchema"]["properties"]["status"]["enum"]
        self.assertEqual(set(project_status_enum), set(Project.Status.values))

        idea_status_enum = tools["list_ideas"]["inputSchema"]["properties"]["status"]["enum"]
        self.assertEqual(set(idea_status_enum), set(Idea.Status.values))

        project_targets = {t.target for targets in PROJECT_TRANSITIONS.values() for t in targets}
        project_target_enum = tools["transition_project"]["inputSchema"]["properties"]["target"]["enum"]
        self.assertEqual(set(project_target_enum), project_targets)

        idea_targets = {t.target for targets in IDEA_TRANSITIONS.values() for t in targets}
        idea_target_enum = tools["transition_idea"]["inputSchema"]["properties"]["target"]["enum"]
        self.assertEqual(set(idea_target_enum), idea_targets)

        idea_kind_enum = tools["list_ideas"]["inputSchema"]["properties"]["kind"]["enum"]
        self.assertEqual(set(idea_kind_enum), set(Idea.Kind.values))


class ToolsCallProjectTests(MCPTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="MusicBox", description="Music player")

    def test_list_projects(self):
        result, payload = self.tool_result("list_projects")
        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["folder"], "MusicBox")

    def test_get_project_by_folder(self):
        result, payload = self.tool_result("get_project", {"project": "MusicBox"})
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["id"], self.project.pk)

    def test_get_project_by_id(self):
        result, payload = self.tool_result("get_project", {"project": str(self.project.pk)})
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["folder"], "MusicBox")

    def test_create_project(self):
        result, payload = self.tool_result(
            "create_project", {"name": "New Proj", "folder": "new-proj"}
        )
        self.assertFalse(result.get("isError"))
        self.assertTrue(Project.objects.filter(folder="new-proj").exists())
        self.assertEqual(payload["name"], "New Proj")

    def test_update_project(self):
        result, payload = self.tool_result(
            "update_project", {"project": "MusicBox", "description": "Updated description"}
        )
        self.assertFalse(result.get("isError"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.description, "Updated description")
        self.assertEqual(payload["description"], "Updated description")

    def test_transition_project(self):
        result, payload = self.tool_result(
            "transition_project", {"project": "MusicBox", "target": "defined"}
        )
        self.assertFalse(result.get("isError"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DEFINED)
        self.assertEqual(payload["status"], "defined")


class ToolsCallIdeaTests(MCPTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="MusicBox")
        self.idea = Idea.objects.create(project=self.project, title="Add shuffle", goal="Shuffle songs")

    def test_list_ideas(self):
        result, payload = self.tool_result("list_ideas", {"project": "MusicBox"})
        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["title"], "Add shuffle")

    def test_list_ideas_open_only(self):
        Idea.objects.create(project=self.project, title="Old idea", status=Idea.Status.ABANDONED)
        result, payload = self.tool_result("list_ideas", {"open_only": True})
        self.assertFalse(result.get("isError"))
        titles = {i["title"] for i in payload}
        self.assertIn("Add shuffle", titles)
        self.assertNotIn("Old idea", titles)

    def test_get_idea_returns_prompt_and_root_path(self):
        result, payload = self.tool_result("get_idea", {"idea_id": self.idea.pk, "include_prompt": True})
        self.assertFalse(result.get("isError"))
        self.assertIn("Add shuffle", payload["prompt"])
        self.assertIn("Shuffle songs", payload["prompt"])
        self.assertEqual(payload["project_root_path"], "/workspace/MusicBox")

    def test_create_idea(self):
        result, payload = self.tool_result(
            "create_idea",
            {"project": "MusicBox", "title": "New idea", "goal": "Do the thing"},
        )
        self.assertFalse(result.get("isError"))
        self.assertTrue(Idea.objects.filter(title="New idea").exists())
        self.assertEqual(self.read_back(payload["id"])["goal"], "Do the thing")

    def test_create_idea_needs_no_verification_unless_asked(self):
        _, plain = self.tool_result("create_idea", {"project": "MusicBox", "title": "Plain"})
        _, asked = self.tool_result(
            "create_idea", {"project": "MusicBox", "title": "Asked", "needs_verification": True}
        )
        self.assertFalse(self.read_back(plain["id"])["needs_verification"])
        self.assertTrue(self.read_back(asked["id"])["needs_verification"])

    def test_update_idea_sets_needs_verification(self):
        result, payload = self.tool_result(
            "update_idea", {"idea_id": self.idea.pk, "needs_verification": True}
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["updated"], ["needs_verification"])
        self.idea.refresh_from_db()
        self.assertTrue(self.idea.needs_verification)

    def test_transition_to_verify_closes_an_idea_without_verification(self):
        self.idea.status = Idea.Status.PLANNED
        self.idea.save()
        _, payload = self.tool_result("transition_idea", {"idea_id": self.idea.pk, "target": "verify"})
        self.assertEqual(payload["status"], "implemented")

    def test_update_idea(self):
        result, payload = self.tool_result(
            "update_idea", {"idea_id": self.idea.pk, "title": "Add real shuffle"}
        )
        self.assertFalse(result.get("isError"))
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.title, "Add real shuffle")
        self.assertEqual(payload["updated"], ["title"])

    def test_list_ideas_exposes_a_commit_filter(self):
        tools = {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}
        self.assertIn("commit", tools["list_ideas"]["inputSchema"]["properties"])

    def test_list_ideas_filters_by_full_commit_hash(self):
        self.idea.commits = ["abc123def456"]
        self.idea.save()
        _, payload = self.tool_result("list_ideas", {"commit": "abc123def456"})
        self.assertEqual([i["title"] for i in payload], ["Add shuffle"])

    def test_list_ideas_filters_by_abbreviated_commit_hash(self):
        self.idea.commits = ["abc123def456"]
        self.idea.save()
        _, payload = self.tool_result("list_ideas", {"commit": "abc123"})
        self.assertEqual([i["title"] for i in payload], ["Add shuffle"])

    def test_list_ideas_commit_too_short_is_an_error_result(self):
        result, _ = self.tool_result("list_ideas", {"commit": "abc"})
        self.assertTrue(result.get("isError"))

    def test_transition_idea_to_implemented(self):
        # new -> planned -> verify -> implemented, the real lifecycle path: Claude
        # Code stops at "verify", and only the user's own move takes it on to
        # "implemented" -- for an idea that needs verification.
        self.idea.needs_verification = True
        self.idea.save()
        self.tool_result("transition_idea", {"idea_id": self.idea.pk, "target": "planned"})
        self.tool_result("transition_idea", {"idea_id": self.idea.pk, "target": "verify"})
        result, payload = self.tool_result(
            "transition_idea", {"idea_id": self.idea.pk, "target": "implemented"}
        )
        self.assertFalse(result.get("isError"))
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.status, Idea.Status.IMPLEMENTED)
        self.assertEqual(payload["status"], "implemented")


class IdeaPlanToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_create_and_update_idea_expose_a_plan_argument(self):
        tools = self._tools()
        self.assertIn("plan", tools["create_idea"]["inputSchema"]["properties"])
        self.assertIn("plan", tools["update_idea"]["inputSchema"]["properties"])

    def test_get_idea_description_points_at_the_plan_field(self):
        self.assertIn("plan", self._tools()["get_idea"]["description"])

    def test_create_idea_stores_the_plan(self):
        project = Project.objects.create(name="A", folder="a")
        _, payload = self.tool_result(
            "create_idea", {"project": "a", "title": "T", "plan": "1. Step one"}
        )
        self.assertEqual(self.read_back(payload["id"])["plan"], "1. Step one")

    def test_update_idea_stores_the_plan_and_rebuilds_the_prompt(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.tool_result("update_idea", {"idea_id": idea.pk, "plan": "1. Step one"})
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["plan"], "1. Step one")
        self.assertIn("### Implementation plan", payload["prompt"])


class IdeaReviewToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_update_idea_exposes_a_review_argument(self):
        self.assertIn("review", self._tools()["update_idea"]["inputSchema"]["properties"])

    def test_create_idea_does_not_expose_a_review_argument(self):
        self.assertNotIn("review", self._tools()["create_idea"]["inputSchema"]["properties"])

    def test_update_idea_stores_the_review_and_rebuilds_the_prompt(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.tool_result("update_idea", {"idea_id": idea.pk, "review": "Fix the edge case"})
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["review"], "Fix the edge case")
        self.assertIn("### Review findings", payload["prompt"])

    def test_review_then_reopen_via_transition_idea(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T", status=Idea.Status.VERIFY)
        self.tool_result("update_idea", {"idea_id": idea.pk, "review": "Fix the edge case"})
        result, payload = self.tool_result(
            "transition_idea", {"idea_id": idea.pk, "target": "planned"}
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(self.read_back(idea.pk)["review"], "Fix the edge case")


class IdeaKindToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_create_and_update_idea_expose_a_kind_argument(self):
        tools = self._tools()
        self.assertIn("kind", tools["create_idea"]["inputSchema"]["properties"])
        self.assertIn("kind", tools["update_idea"]["inputSchema"]["properties"])

    def test_list_ideas_exposes_a_kind_filter(self):
        self.assertIn("kind", self._tools()["list_ideas"]["inputSchema"]["properties"])

    def test_create_idea_stores_the_kind(self):
        Project.objects.create(name="A", folder="a")
        _, payload = self.tool_result(
            "create_idea", {"project": "a", "title": "T", "kind": "fix"}
        )
        payload = self.read_back(payload["id"])
        self.assertEqual(payload["kind"], "fix")
        self.assertEqual(payload["kind_display"], "Fix")

    def test_update_idea_stores_the_kind_and_rebuilds_the_prompt(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.tool_result("update_idea", {"idea_id": idea.pk, "kind": "design"})
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["kind"], "design")
        self.assertIn("### Type\nDesign", payload["prompt"])

    def test_list_ideas_filters_by_kind(self):
        project = Project.objects.create(name="A", folder="a")
        Idea.objects.create(project=project, title="Fix idea", kind=Idea.Kind.FIX)
        Idea.objects.create(project=project, title="Feature idea", kind=Idea.Kind.FEATURE)
        _, payload = self.tool_result("list_ideas", {"kind": "fix"})
        self.assertEqual([i["title"] for i in payload], ["Fix idea"])


class ToolsCallErrorTests(MCPTestCase):
    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="MusicBox")

    def test_unknown_tool_name_is_isError_not_rpc_error(self):
        response = self.call_tool("no_such_tool")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("error", body)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("no_such_tool", body["result"]["content"][0]["text"])

    def test_missing_record_is_isError_with_readable_message(self):
        response = self.call_tool("get_project", {"project": "Kraete"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("Kraete", body["result"]["content"][0]["text"])

    def test_illegal_transition_is_isError_with_readable_message(self):
        response = self.call_tool(
            "transition_project", {"project": "MusicBox", "target": "implemented"}
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("implemented", body["result"]["content"][0]["text"])

    def test_missing_required_argument_is_isError(self):
        response = self.call_tool("get_project", {})
        body = response.json()
        self.assertTrue(body["result"]["isError"])
        self.assertIn("project", body["result"]["content"][0]["text"])

    def test_unknown_argument_is_isError(self):
        response = self.call_tool("get_project", {"project": "MusicBox", "bogus": 1})
        body = response.json()
        self.assertTrue(body["result"]["isError"])
        self.assertIn("bogus", body["result"]["content"][0]["text"])


class ProtocolLevelErrorTests(MCPTestCase):
    def test_malformed_json_is_parse_error(self):
        response = self.client.post(MCP_URL, data="{not json", content_type="application/json")
        body = response.json()
        self.assertEqual(body["error"]["code"], -32700)

    def test_batch_array_is_invalid_request(self):
        response = self.client.post(MCP_URL, data=json.dumps([rpc("ping")]), content_type="application/json")
        body = response.json()
        self.assertEqual(body["error"]["code"], -32600)
        self.assertIn("batch", body["error"]["message"].lower())

    def test_non_rpc_object_is_invalid_request(self):
        response = self.post({"foo": "bar"})
        body = response.json()
        self.assertEqual(body["error"]["code"], -32600)

    def test_unknown_method_is_method_not_found(self):
        response = self.post(rpc("no/such/method"))
        body = response.json()
        self.assertEqual(body["error"]["code"], -32601)


class TransportTests(MCPTestCase):
    def test_get_is_405_with_allow_post(self):
        response = self.client.get(MCP_URL)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_delete_is_405_with_allow_post(self):
        response = self.client.delete(MCP_URL)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_post_without_trailing_slash_works_and_is_not_a_redirect(self):
        response = self.client.post("/mcp", data=json.dumps(rpc("ping")), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], {})


class IdeaReportToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_update_idea_exposes_a_report_argument(self):
        self.assertIn("report", self._tools()["update_idea"]["inputSchema"]["properties"])

    def test_create_idea_does_not_expose_a_report_argument(self):
        # Matches IdeaCreateForm: a report is written after the work is done, not at
        # creation time.
        self.assertNotIn("report", self._tools()["create_idea"]["inputSchema"]["properties"])

    def test_update_idea_stores_the_report(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.tool_result(
            "update_idea", {"idea_id": idea.pk, "report": "Implemented as planned. Nothing to report."}
        )
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["report"], "Implemented as planned. Nothing to report.")

    def test_report_does_not_appear_in_the_prompt(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T", goal="G")
        self.tool_result("update_idea", {"idea_id": idea.pk, "report": "Ran into a snag."})
        payload = self.read_back(idea.pk)
        self.assertNotIn("snag", payload["prompt"])

    def test_transition_idea_description_says_verify_not_implemented_for_the_agent(self):
        description = self._tools()["transition_idea"]["description"]
        self.assertIn("verify", description)

    def test_get_idea_description_mentions_the_report(self):
        self.assertIn("report", self._tools()["get_idea"]["description"])


class IdeaCommitsToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_update_idea_exposes_a_commits_argument(self):
        self.assertIn("commits", self._tools()["update_idea"]["inputSchema"]["properties"])

    def test_create_idea_does_not_expose_a_commits_argument(self):
        # Matches IdeaCreateForm: an idea has no implementing commits until it is done.
        self.assertNotIn("commits", self._tools()["create_idea"]["inputSchema"]["properties"])

    def test_update_idea_stores_the_commits(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(project=project, title="T")
        self.tool_result("update_idea", {"idea_id": idea.pk, "commits": ["abc123", "def456"]})
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["commits"], ["abc123", "def456"])


class IdeaVerifyTransitionToolTests(MCPTestCase):
    def test_planned_to_implemented_directly_is_an_error(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(
            project=project, title="T", status=Idea.Status.PLANNED, needs_verification=True
        )
        result, _ = self.tool_result(
            "transition_idea", {"idea_id": idea.pk, "target": "implemented"}
        )
        self.assertTrue(result.get("isError"))

    def test_planned_to_verify_then_verify_to_implemented(self):
        project = Project.objects.create(name="A", folder="a")
        idea = Idea.objects.create(
            project=project, title="T", status=Idea.Status.PLANNED, needs_verification=True
        )
        result, payload = self.tool_result(
            "transition_idea", {"idea_id": idea.pk, "target": "verify"}
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["status"], "verify")

        result, payload = self.tool_result(
            "transition_idea", {"idea_id": idea.pk, "target": "implemented"}
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["status"], "implemented")


class AddScreenshotToolTests(MCPTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.project = Project.objects.create(name="A", folder="a")
        self.idea = Idea.objects.create(project=self.project, title="T")

    def test_add_screenshot_from_base64(self):
        encoded = base64.b64encode(b"pngbytes").decode()
        result, payload = self.tool_result(
            "add_screenshot",
            {
                "idea_id": self.idea.pk,
                "filename": "shot.png",
                "data_base64": encoded,
                "caption": "After",
            },
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload["screenshots"]), 1)
        self.assertEqual(payload["screenshots"][0]["caption"], "After")

    def test_a_multi_megabyte_screenshot_gets_through(self):
        # Django rejects a non-multipart body over DATA_UPLOAD_MAX_MEMORY_SIZE before
        # any view runs, and base64 inflates an image by a third, so the default 2.5 MB
        # turned an ordinary full-page screenshot into an HTML 400 the model could not
        # read. settings.py raises the cap; this is the regression test for it.
        encoded = base64.b64encode(b"p" * (3 * 1024 * 1024)).decode()
        result, payload = self.tool_result(
            "add_screenshot",
            {"idea_id": self.idea.pk, "filename": "big.png", "data_base64": encoded},
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload["screenshots"]), 1)

    def test_bad_base64_is_an_error_result_not_an_exception(self):
        result, _ = self.tool_result(
            "add_screenshot",
            {
                "idea_id": self.idea.pk,
                "filename": "shot.png",
                "data_base64": "not valid base64!!",
            },
        )
        self.assertTrue(result.get("isError"))

    def test_bad_extension_is_an_error_result(self):
        encoded = base64.b64encode(b"whatever").decode()
        result, _ = self.tool_result(
            "add_screenshot",
            {"idea_id": self.idea.pk, "filename": "shot.exe", "data_base64": encoded},
        )
        self.assertTrue(result.get("isError"))

    def test_add_screenshot_exposed_in_tools_list(self):
        response = self.post(rpc("tools/list"))
        tools = {t["name"] for t in response.json()["result"]["tools"]}
        self.assertIn("add_screenshot", tools)


class SyncGitStatusToolTests(MCPTestCase):
    def test_no_projects_returns_empty_list(self):
        result, payload = self.tool_result("sync_git_status")
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload, [])

    def test_updates_project_git_status(self):
        import subprocess
        from unittest import mock

        project = Project.objects.create(name="P", folder="p")
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True)
            with mock.patch.object(Project, "root_path", new=property(lambda self: tmp)):
                result, payload = self.tool_result("sync_git_status")

        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["folder"], "p")
        project.refresh_from_db()
        self.assertEqual(project.git_status, Project.GitStatus.NO_REMOTE)

    def test_rejects_unknown_argument(self):
        result, payload = self.tool_result("sync_git_status", {"bogus": 1})
        self.assertTrue(result.get("isError"))


class AddArtifactToolTests(MCPTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.project = Project.objects.create(name="A", folder="a")
        self.idea = Idea.objects.create(project=self.project, title="T")

    def test_add_artifact_from_base64(self):
        encoded = base64.b64encode(b"# Spec\n").decode()
        result, payload = self.tool_result(
            "add_artifact",
            {
                "idea_id": self.idea.pk,
                "filename": "spec.md",
                "data_base64": encoded,
                "caption": "Design",
            },
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(len(payload["artifacts"]), 1)
        self.assertEqual(payload["artifacts"][0]["caption"], "Design")

    def test_add_artifact_accepts_a_new_idea(self):
        # Unlike add_screenshot, artifacts are attachable in every idea status,
        # including a brand-new idea that has no plan or report yet.
        encoded = base64.b64encode(b"log line").decode()
        result, payload = self.tool_result(
            "add_artifact",
            {"idea_id": self.idea.pk, "filename": "run.log", "data_base64": encoded},
        )
        self.assertFalse(result.get("isError"))
        self.assertEqual(self.idea.status, Idea.Status.NEW)
        self.assertEqual(len(payload["artifacts"]), 1)

    def test_oversized_artifact_is_an_error_result(self):
        encoded = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
        result, _ = self.tool_result(
            "add_artifact",
            {"idea_id": self.idea.pk, "filename": "big.log", "data_base64": encoded},
        )
        self.assertTrue(result.get("isError"))

    def test_bad_base64_is_an_error_result_not_an_exception(self):
        result, _ = self.tool_result(
            "add_artifact",
            {
                "idea_id": self.idea.pk,
                "filename": "spec.md",
                "data_base64": "not valid base64!!",
            },
        )
        self.assertTrue(result.get("isError"))

    def test_bad_extension_is_an_error_result(self):
        encoded = base64.b64encode(b"whatever").decode()
        result, _ = self.tool_result(
            "add_artifact",
            {"idea_id": self.idea.pk, "filename": "virus.exe", "data_base64": encoded},
        )
        self.assertTrue(result.get("isError"))

    def test_add_artifact_exposed_in_tools_list(self):
        response = self.post(rpc("tools/list"))
        tools = {t["name"] for t in response.json()["result"]["tools"]}
        self.assertIn("add_artifact", tools)


class ReadAndDeleteArtifactToolTests(MCPTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.project = Project.objects.create(name="A", folder="a")
        self.idea = Idea.objects.create(project=self.project, title="T")
        encoded = base64.b64encode(b"# Spec\n").decode()
        _, payload = self.tool_result(
            "add_artifact",
            {"idea_id": self.idea.pk, "filename": "spec.md", "data_base64": encoded},
        )
        self.artifact_id = payload["artifacts"][0]["id"]

    def test_read_artifact_returns_text_content(self):
        result, payload = self.tool_result("read_artifact", {"artifact_id": self.artifact_id})
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["content"], "# Spec\n")
        self.assertFalse(payload["content_truncated"])

    def test_read_missing_artifact_is_an_error_result(self):
        result, _ = self.tool_result("read_artifact", {"artifact_id": 999999})
        self.assertTrue(result.get("isError"))

    def test_delete_artifact_removes_it(self):
        result, payload = self.tool_result("delete_artifact", {"artifact_id": self.artifact_id})
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["artifacts"], [])

    def test_delete_missing_artifact_is_an_error_result(self):
        result, _ = self.tool_result("delete_artifact", {"artifact_id": 999999})
        self.assertTrue(result.get("isError"))


class IdeaMoveToolTests(MCPTestCase):
    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_update_idea_exposes_a_project_argument_and_says_so(self):
        tool = self._tools()["update_idea"]
        project = tool["inputSchema"]["properties"]["project"]
        self.assertEqual(set(project["type"]), {"string", "integer"})
        self.assertIn("project", tool["description"])
        self.assertIn("move", tool["description"].lower())

    def test_create_idea_does_not_take_project_in_the_body_twice(self):
        # create_idea's project is the target project argument, not a body field --
        # it exists there already, so this only pins that it is still required.
        tool = self._tools()["create_idea"]
        self.assertIn("project", tool["inputSchema"]["required"])

    def test_update_idea_moves_by_folder(self):
        source = Project.objects.create(name="Source", folder="source")
        target = Project.objects.create(name="Target", folder="target")
        idea = Idea.objects.create(project=source, title="T", status=Idea.Status.PLANNED)
        result, payload = self.tool_result("update_idea", {"idea_id": idea.pk, "project": "target"})
        self.assertFalse(result.get("isError"))
        self.assertEqual(payload["updated"], ["project"])
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["id"], idea.pk)
        self.assertEqual(payload["project_id"], target.pk)
        self.assertEqual(payload["status"], "planned")
        self.assertIn("cd /workspace/target", payload["prompt"])

    def test_update_idea_moves_by_id(self):
        source = Project.objects.create(name="Source", folder="source")
        target = Project.objects.create(name="Target", folder="target")
        idea = Idea.objects.create(project=source, title="T")
        self.tool_result("update_idea", {"idea_id": idea.pk, "project": target.pk})
        payload = self.read_back(idea.pk)
        self.assertEqual(payload["project_folder"], "target")

    def test_update_idea_unknown_project_is_an_error(self):
        source = Project.objects.create(name="Source", folder="source")
        idea = Idea.objects.create(project=source, title="T")
        result = self.call_tool("update_idea", {"idea_id": idea.pk, "project": "nope"}).json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("nope", result["content"][0]["text"])


class CompactResponseTests(MCPTestCase):
    """#244: MCP output is compact by default; full records only on request."""

    def setUp(self):
        self.project = Project.objects.create(name="MusicBox", folder="MusicBox")
        self.idea = Idea.objects.create(
            project=self.project,
            title="Add shuffle",
            goal="Shuffle songs " + "x" * 300,
            plan="1. Step one",
            report="Done.",
        )

    def _tools(self):
        return {t["name"]: t for t in self.post(rpc("tools/list")).json()["result"]["tools"]}

    def test_list_ideas_returns_compact_rows_by_default(self):
        _, payload = self.tool_result("list_ideas", {"project": "MusicBox"})
        self.assertEqual(
            set(payload[0]),
            {"id", "project_folder", "project_name", "title", "kind", "status",
             "has_plan", "has_report", "goal"},
        )
        self.assertTrue(payload[0]["has_plan"])
        self.assertTrue(payload[0]["has_report"])
        self.assertLessEqual(len(payload[0]["goal"]), 160)
        self.assertTrue(payload[0]["goal"].endswith("…"))

    def test_list_ideas_full_returns_full_records(self):
        _, payload = self.tool_result("list_ideas", {"project": "MusicBox", "full": True})
        self.assertEqual(payload[0]["plan"], "1. Step one")
        self.assertIn("allowed_transitions", payload[0])

    def test_get_idea_omits_prompt_by_default(self):
        _, payload = self.tool_result("get_idea", {"idea_id": self.idea.pk})
        self.assertNotIn("prompt", payload)
        self.assertEqual(payload["plan"], "1. Step one")
        self.assertEqual(payload["project_root_path"], "/workspace/MusicBox")

    def test_get_idea_include_prompt(self):
        _, payload = self.tool_result("get_idea", {"idea_id": self.idea.pk, "include_prompt": True})
        self.assertIn("### Implementation plan", payload["prompt"])

    def test_get_idea_description_carries_the_prompt_only_instructions(self):
        from .models import PLAN_INSTRUCTION, REVIEW_INSTRUCTION

        description = self._tools()["get_idea"]["description"]
        self.assertIn(PLAN_INSTRUCTION, description)
        self.assertIn(REVIEW_INSTRUCTION, description)
        self.assertIn("CLAUDE.md", description)

    def test_update_idea_answers_with_an_ack(self):
        _, payload = self.tool_result(
            "update_idea", {"idea_id": self.idea.pk, "plan": "2. Step two", "title": "T2"}
        )
        self.assertEqual(
            payload,
            {
                "id": self.idea.pk,
                "status": "new",
                "allowed_transitions": [
                    {"target": "planned", "label": "Plan"},
                    {"target": "abandoned", "label": "Abandon"},
                ],
                "updated": ["plan", "title"],
            },
        )

    def test_transition_idea_answers_with_an_ack(self):
        _, payload = self.tool_result("transition_idea", {"idea_id": self.idea.pk, "target": "planned"})
        self.assertEqual(set(payload), {"id", "status", "allowed_transitions"})
        self.assertEqual(payload["status"], "planned")

    def test_create_idea_answers_with_an_ack(self):
        _, payload = self.tool_result("create_idea", {"project": "MusicBox", "title": "New", "goal": "G"})
        self.assertEqual(set(payload), {"id", "project_folder", "title", "status", "allowed_transitions"})
        self.assertEqual(payload["project_folder"], "MusicBox")

    def test_attachment_writes_answer_with_the_attachment_list_only(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            encoded = base64.b64encode(b"# Spec\n").decode()
            _, payload = self.tool_result(
                "add_artifact", {"idea_id": self.idea.pk, "filename": "s.md", "data_base64": encoded}
            )
            self.assertEqual(set(payload), {"id", "artifacts"})
            _, payload = self.tool_result("delete_artifact", {"artifact_id": payload["artifacts"][0]["id"]})
            self.assertEqual(payload, {"id": self.idea.pk, "artifacts": []})

    def test_tool_text_is_unindented_and_not_ascii_escaped(self):
        self.idea.title = "Pfeil → hier"
        self.idea.save()
        text = self.call_tool("get_idea", {"idea_id": self.idea.pk}).json()["result"]["content"][0]["text"]
        self.assertIn("→", text)
        self.assertNotIn("\n", text)
