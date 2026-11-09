"""MCP endpoint: a stateless, hand-rolled JSON-RPC 2.0 server over Streamable HTTP.

`POST /mcp/` carries one JSON-RPC message and gets a plain JSON response back -- the MCP
spec permits that instead of an SSE stream, and nothing here streams or pushes, so there
is no SSE, no Mcp-Session-Id, no server-initiated message. Each tool is a thin adapter
over one function in `core/services.py`; see that module for the actual behaviour.

Deliberately not using the `mcp` SDK: the protocol surface needed here is small enough
that hand-rolling it keeps the dependency list at zero and every branch inspectable.
"""

import base64
import binascii
import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import schemas, services
from .models import ARTIFACT_EXTENSIONS, PLAN_INSTRUCTION, REVIEW_INSTRUCTION

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "claude-manager", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Tool input schemas, built from core/schemas.py so they cannot fall behind
# core/models.py or core/transitions.py.
# ---------------------------------------------------------------------------

def _object_schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _status_enum(values, description):
    return {"type": "string", "enum": values, "description": description}


PROJECT_STATUS_ARG = _status_enum(
    schemas.PROJECT_STATUS_VALUES, "Filter to projects in this status."
)
PROJECT_TARGET_ARG = _status_enum(
    schemas.PROJECT_TRANSITION_TARGETS,
    "The status to move the project to. Must be one of the record's own "
    "allowed_transitions targets, not just any project status.",
)
IDEA_STATUS_ARG = _status_enum(schemas.IDEA_STATUS_VALUES, "Filter to ideas in this status.")
IDEA_KIND_ARG = _status_enum(schemas.IDEA_KIND_VALUES, "Filter to ideas of this type.")
IDEA_TARGET_ARG = _status_enum(
    schemas.IDEA_TRANSITION_TARGETS,
    "The status to move the idea to. Must be one of the record's own "
    "allowed_transitions targets, not just any idea status. Once implementation work on "
    "an idea is done, write the report with update_idea, attach any screenshots with "
    "add_screenshot, and call this with target \"verify\" -- never \"implemented\" "
    "directly, since moving to implemented is the user's own move after checking the "
    "work. On an idea whose needs_verification is false, \"verify\" closes it as "
    "\"implemented\" straight away.",
)

PROJECT_REF_ARG = {
    # A union type, because the description promises both spellings: a client that
    # read "id or folder name" and sent the integer id would otherwise be violating
    # the very schema this tool advertises. services._resolve_project takes either.
    "type": ["string", "integer"],
    "description": "The project's id or its folder name -- either resolves it.",
}

_project_fields = schemas.PROJECT_FIELD_SCHEMAS
_idea_fields = schemas.IDEA_FIELD_SCHEMAS


def _decode_base64(data):
    # A malformed data_base64 is a caller mistake, not a server fault -- raising a
    # ServiceError routes it through _call_tool's existing mapping to an isError
    # result with a readable message, the same as any other bad input.
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise services.Invalid(f"data_base64 is not valid base64: {exc}")


def _add_artifact_from_base64(arguments):
    # Checked against MCP_ARTIFACT_MAX_BYTES (not the larger API cap) before the
    # decoded bytes are stored -- this is the path where the model itself emitted
    # every byte, so it is the one place that cap has to be enforced.
    content = _decode_base64(arguments["data_base64"])
    services.check_artifact_upload(arguments["filename"], len(content), services.MCP_ARTIFACT_MAX_BYTES)
    return services.add_artifact(
        arguments["idea_id"], arguments["filename"], content, caption=arguments.get("caption", "")
    )

# Writes answer with an ack, not the record: echoing a full idea (plan, report and
# prompt included) after every update or transition put the same text into the
# session two or three times over. Each ack only drops keys from the service's
# full dict -- the API still returns that dict whole.
IDEA_ACK_KEYS = ("id", "status", "allowed_transitions")
IDEA_CREATE_ACK_KEYS = ("id", "project_folder", "title", "status", "allowed_transitions")


def _ack(record, keys, **extra):
    return {**{k: record[k] for k in keys}, **extra}


def _update_idea(arguments):
    data = {k: v for k, v in arguments.items() if k != "idea_id"}
    record = services.update_idea(arguments["idea_id"], data)
    return _ack(record, IDEA_ACK_KEYS, updated=sorted(data))


TOOLS = [
    {
        "name": "list_projects",
        "description": (
            "List projects, optionally filtered by status or folder. Each returned "
            "record carries allowed_transitions, the legal moves out of its current "
            "status."
        ),
        "inputSchema": _object_schema(
            {"status": PROJECT_STATUS_ARG, "folder": _project_fields["folder"]},
            [],
        ),
        "handler": lambda a: services.list_projects(
            status=a.get("status"), folder=a.get("folder")
        ),
    },
    {
        "name": "get_project",
        "description": (
            "Fetch a single project by id or folder name. The record carries "
            "allowed_transitions, the legal moves out of its current status."
        ),
        "inputSchema": _object_schema({"project": PROJECT_REF_ARG}, ["project"]),
        "handler": lambda a: services.get_project(a["project"]),
    },
    {
        "name": "create_project",
        "description": "Create a new project.",
        "inputSchema": _object_schema(
            {
                "name": _project_fields["name"],
                "folder": _project_fields["folder"],
                "description": _project_fields["description"],
            },
            ["name", "folder"],
        ),
        "handler": lambda a: services.create_project(dict(a)),
    },
    {
        "name": "update_project",
        "description": (
            "Edit a project's name, folder, or description. Omitted fields are left "
            "unchanged. Status is not writable here -- use transition_project."
        ),
        "inputSchema": _object_schema(
            {
                "project": PROJECT_REF_ARG,
                "name": _project_fields["name"],
                "folder": _project_fields["folder"],
                "description": _project_fields["description"],
            },
            ["project"],
        ),
        "handler": lambda a: services.update_project(
            a["project"], {k: v for k, v in a.items() if k != "project"}
        ),
    },
    {
        "name": "transition_project",
        "description": (
            "Move a project to a new lifecycle status. The target must be one of the "
            "targets listed in the project's own allowed_transitions -- fetch the "
            "project first if unsure what is legal."
        ),
        "inputSchema": _object_schema(
            {"project": PROJECT_REF_ARG, "target": PROJECT_TARGET_ARG},
            ["project", "target"],
        ),
        "handler": lambda a: services.transition_project(a["project"], a["target"]),
    },
    {
        "name": "list_ideas",
        "description": (
            "List ideas, optionally filtered by project (id or folder), status, type "
            "(kind), open_only (excludes implemented/abandoned ideas), or by a commit "
            "hash, full or abbreviated, that implemented it. Use commit to go from a "
            "bare hash found in git blame/git bisect/git log back to the idea that "
            "produced it -- its goal, plan and report -- before starting a regression "
            "fix. Use q to search past ideas by topic before re-deriving a lesson: "
            "every word must appear, each in any of title, goal, context, notes, "
            "plan, report or review (case-insensitive). Returns compact rows (id, "
            "project_folder, project_name, title, "
            "kind, status, has_plan, has_report, and goal cut to 160 characters) -- "
            "call get_idea for an idea's full text, or pass full=true for full "
            "records, which can run to hundreds of KB for a whole project."
        ),
        "inputSchema": _object_schema(
            {
                "project": PROJECT_REF_ARG,
                "status": IDEA_STATUS_ARG,
                "kind": IDEA_KIND_ARG,
                "open_only": {
                    "type": "boolean",
                    "description": "Exclude implemented and abandoned ideas.",
                },
                "commit": {
                    "type": "string",
                    "description": (
                        "A commit hash, full or abbreviated (at least 4 characters), "
                        "to find the idea that recorded it in its commits list. "
                        "Matches whichever side is shorter -- an abbreviated query "
                        "against a full stored hash or vice versa."
                    ),
                },
                "q": {
                    "type": "string",
                    "description": (
                        "Search text. Whitespace-separated words that must all "
                        "appear, each in any text field (title, goal, context, "
                        "notes, plan, report, review); case-insensitive."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": "Return full records instead of compact rows.",
                },
            },
            [],
        ),
        "handler": lambda a: services.list_ideas(
            project=a.get("project"),
            status=a.get("status"),
            kind=a.get("kind"),
            open_only=a.get("open_only", False),
            commit=a.get("commit"),
            q=a.get("q"),
            full=a.get("full", False),
        ),
    },
    {
        "name": "get_idea",
        "description": (
            "Fetch a single idea by id. The record is the instruction for the work: "
            "`cd` into `project_root_path` first and carry out everything from there, "
            f"after reading {settings.WORKSPACE_ROOT}/CLAUDE.md and <project_root_path>/CLAUDE.md if "
            "not already read this session; then work from title, kind, goal, context "
            "and notes. The record also carries allowed_transitions, the legal moves "
            "out of its current status. An idea's `plan` field holds a step-by-step "
            "implementation plan written ahead of the work; write one there with "
            "update_idea when planning, and when one is stored: "
            + PLAN_INSTRUCTION
            + " A non-empty `review` holds code-review findings on earlier work: "
            + REVIEW_INSTRUCTION
            + " Pass include_prompt=true to also get `prompt`, the same content "
            "rendered as one Markdown prompt (it repeats every section, so it roughly "
            "doubles the response). Once the work is done, first move any lesson that "
            "will matter again (a root cause, a trap, a how-to) into the project's "
            "docs/ -- the report is never fed back into a later prompt -- and add a "
            "line to the project's CLAUDE.md only for a new rule or a pointer to that "
            "doc. Then write a short report to the `report` field with update_idea, "
            "linking any doc it changed -- only possible problems and relevant "
            "decisions made, as a list of short sentences, or, if there is nothing "
            "relevant, just \"Implemented as planned. Nothing to report.\" -- attach "
            "any screenshots with add_screenshot, and call transition_idea with "
            "target \"verify\"; the user moves it to \"implemented\" themselves after "
            "checking the report -- unless needs_verification is false, in which case "
            "that same call closes it as \"implemented\". The `screenshots` field lists what is already "
            "attached, and `artifacts` lists any other attached files (logs, specs, "
            "binaries -- see add_artifact)."
        ),
        "inputSchema": _object_schema(
            {
                "idea_id": {"type": "integer", "description": "The idea's id."},
                "include_prompt": {
                    "type": "boolean",
                    "description": "Also return the rendered prompt (repeats every section).",
                },
            },
            ["idea_id"],
        ),
        "handler": lambda a: services.get_idea(
            a["idea_id"], include_prompt=a.get("include_prompt", False)
        ),
    },
    {
        "name": "create_idea",
        "description": (
            "Add a new idea to a project. Besides the title, the idea is built from "
            "four free-form sections that together become its executable prompt: goal "
            "(the outcome sought), context (background needed to understand it), notes "
            "(anything else -- the idea itself, caveats, how to verify it, or whatever "
            "else applies), and plan (a step-by-step implementation plan, normally "
            "written later than the rest). All four are optional but filling them in "
            "produces a better prompt. kind classifies the idea as one of fix, feature, "
            "design, question or other; a non-other kind becomes a leading \"### Type\" "
            "section of the generated prompt. other is the neutral default and shows "
            "nothing in the prompt, so it never needs setting explicitly. "
            "priority is low, medium (the default) or high and orders the idea lists, "
            "high first. Always set it: use the one the user named, otherwise choose "
            "yourself -- high for blocking, broken or time-sensitive work, medium for "
            "normal work, low for nice-to-haves. "
            "needs_verification defaults to false: finishing the idea closes it "
            "without the user checking it; pass true when they should. Answers "
            "with the new idea's id, project_folder, title, status and "
            "allowed_transitions, not the record."
        ),
        "inputSchema": _object_schema(
            {
                "project": PROJECT_REF_ARG,
                "title": _idea_fields["title"],
                "kind": _idea_fields["kind"],
                "priority": _idea_fields["priority"],
                "goal": _idea_fields["goal"],
                "context": _idea_fields["context"],
                "notes": _idea_fields["notes"],
                "plan": _idea_fields["plan"],
                "needs_verification": _idea_fields["needs_verification"],
            },
            ["project", "title"],
        ),
        "handler": lambda a: _ack(
            services.create_idea(a["project"], {k: v for k, v in a.items() if k != "project"}),
            IDEA_CREATE_ACK_KEYS,
        ),
    },
    {
        "name": "update_idea",
        "description": (
            "Edit an idea's title, type (kind), priority (low, medium or high), any of its five prompt sections "
            "(goal, context, notes, plan, review), its report, or its commits. "
            "Omitted fields are left unchanged. Use plan to store a step-by-step "
            "implementation plan for the idea without implementing it yet. kind is "
            "one of fix, feature, design, question or other; a non-other kind "
            "becomes a leading \"### Type\" section of the generated prompt, while "
            "other -- the neutral default -- shows nothing. report is not part of "
            "the prompt -- it is written after the work is done, as a list of short "
            "sentences covering only possible problems and relevant decisions made, "
            "or, if there is nothing relevant, just \"Implemented as planned. "
            "Nothing to report.\"; write it here and then call transition_idea with "
            "target \"verify\". review IS part of the prompt -- it is input for the "
            "idea's next agent, not output: when a code review turns up a finding on "
            "code that already corresponds to an existing idea (most often the idea "
            "being verified, or one found via list_ideas' commit filter), write the "
            "finding here with update_idea and then call transition_idea with target "
            "\"planned\" (the \"Reopen\" transition, legal from verify/implemented/"
            "abandoned) so it reappears as open work; only create a new idea with "
            "create_idea when the finding has no corresponding idea at all. Status "
            "is not writable here -- use transition_idea. commits is the list of "
            "hashes that implemented this idea, normally set once, after the work "
            "is committed -- it replaces the full list rather than appending to it. "
            "project moves the idea to another project (given as its id or folder "
            "name), keeping its id, status, sections, plan, report, review, commits "
            "and screenshots -- use it instead of re-creating the idea under the "
            "other project and abandoning the original. Answers with id, status, "
            "allowed_transitions and `updated` (the names of the fields written), "
            "not the record -- call get_idea to read it back."
        ),
        "inputSchema": _object_schema(
            {
                "idea_id": {"type": "integer", "description": "The idea's id."},
                "project": _idea_fields["project"],
                "title": _idea_fields["title"],
                "kind": _idea_fields["kind"],
                "priority": _idea_fields["priority"],
                "goal": _idea_fields["goal"],
                "context": _idea_fields["context"],
                "notes": _idea_fields["notes"],
                "plan": _idea_fields["plan"],
                "report": _idea_fields["report"],
                "review": _idea_fields["review"],
                "commits": _idea_fields["commits"],
                "needs_verification": _idea_fields["needs_verification"],
            },
            ["idea_id"],
        ),
        "handler": _update_idea,
    },
    {
        "name": "transition_idea",
        "description": (
            "Move an idea to a new lifecycle status. Once implementation work for the "
            "idea is done, write its report with update_idea, attach any screenshots "
            "with add_screenshot, and call this with target \"verify\" -- never "
            "\"implemented\" directly; moving to implemented is the user's own move, "
            "after checking the report. On an idea whose needs_verification is false, "
            "\"verify\" closes it as \"implemented\" instead, and the answer says so. "
            "The target must be one of the targets listed "
            "in the idea's own allowed_transitions -- fetch the idea first if unsure "
            "what is legal. Answers with id, status and allowed_transitions."
        ),
        "inputSchema": _object_schema(
            {"idea_id": {"type": "integer", "description": "The idea's id."}, "target": IDEA_TARGET_ARG},
            ["idea_id", "target"],
        ),
        "handler": lambda a: _ack(
            services.transition_idea(a["idea_id"], a["target"]), IDEA_ACK_KEYS
        ),
    },
    {
        "name": "add_screenshot",
        "description": (
            "Attach a screenshot to an idea, normally just before moving it to "
            "\"verify\" with transition_idea. data_base64 is the image file's bytes, "
            "base64-encoded, because this server may run on another machine or in a "
            "container and so cannot read a path the calling session names -- "
            "encode the file yourself and pass its content, not a path. filename must "
            "end in .png, .jpg, .jpeg, .gif or .webp; the upload is capped at 10 MB. "
            "Answers with the idea's id and its screenshot list."
        ),
        "inputSchema": _object_schema(
            {
                "idea_id": {"type": "integer", "description": "The idea's id."},
                "filename": {
                    "type": "string",
                    "description": "Filename, used only for its extension and for display.",
                },
                "data_base64": {
                    "type": "string",
                    "description": "The image file's bytes, base64-encoded.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption shown under the thumbnail.",
                },
            },
            ["idea_id", "filename", "data_base64"],
        ),
        "handler": lambda a: _ack(
            services.add_screenshot(
                a["idea_id"],
                a["filename"],
                _decode_base64(a["data_base64"]),
                caption=a.get("caption", ""),
            ),
            ("id", "screenshots"),
        ),
    },
    {
        "name": "add_artifact",
        "description": (
            "Attach an artifact (any file -- a log, a .md spec, a binary like an .apk) "
            "to an idea, in any status, unlike add_screenshot which is image-only. "
            "data_base64 is the file's bytes, base64-encoded, because this server "
            "cannot read a path the calling session names -- encode the file yourself "
            "and pass its content, not a path. This is capped at 2 MB since the model "
            "has to emit every byte of data_base64 itself; for a larger file (e.g. an "
            "APK), use the API instead: curl --noproxy '*' -F file=@<path> -F "
            f"caption=<caption> {settings.BASE_URL}/api/ideas/<idea_id>/artifacts/ "
            "-- that endpoint takes up to 200 MB. filename must end in one of: "
            + ", ".join(sorted(ARTIFACT_EXTENSIONS))
            + ". An .html/.htm artifact is served sandboxed (opaque origin, no "
            "external loads): inline scripts, styles and data: images work, nothing "
            "else. Answers with the idea's id and its artifact list."
        ),
        "inputSchema": _object_schema(
            {
                "idea_id": {"type": "integer", "description": "The idea's id."},
                "filename": {
                    "type": "string",
                    "description": "Filename, used for its extension and for display.",
                },
                "data_base64": {
                    "type": "string",
                    "description": "The file's bytes, base64-encoded.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption shown next to the file.",
                },
            },
            ["idea_id", "filename", "data_base64"],
        ),
        "handler": lambda a: _ack(_add_artifact_from_base64(a), ("id", "artifacts")),
    },
    {
        "name": "read_artifact",
        "description": (
            "Fetch one artifact's metadata (filename, url, caption, content_type, "
            "size). For a text-type artifact (log, .md, .json, .yaml, .csv, .diff, "
            "...) this also returns its content, capped at 256 KB with "
            "content_truncated set if it was cut off; a binary artifact never returns "
            "its bytes here -- fetch its url instead."
        ),
        "inputSchema": _object_schema(
            {"artifact_id": {"type": "integer", "description": "The artifact's id."}},
            ["artifact_id"],
        ),
        "handler": lambda a: services.read_artifact(a["artifact_id"]),
    },
    {
        "name": "delete_artifact",
        "description": (
            "Remove an artifact and its stored file. Answers with the idea's id and "
            "its remaining artifact list."
        ),
        "inputSchema": _object_schema(
            {"artifact_id": {"type": "integer", "description": "The artifact's id."}},
            ["artifact_id"],
        ),
        "handler": lambda a: _ack(services.delete_artifact(a["artifact_id"]), ("id", "artifacts")),
    },
    {
        "name": "sync_git_status",
        "description": (
            "Check every project's Git status (clean, uncommitted, unpushed, ...) and "
            "store it on the Project record. Read-only against every project's "
            "checkout -- runs `git status`, never writes to a repo, only to this app's "
            "database -- so it is safe to call directly against the real instance, "
            "including from a session with no shell access to it. Takes no arguments. "
            "Only call this when asked to update/sync/refresh Git status; nothing "
            "triggers it automatically. Returns one result per project."
        ),
        "inputSchema": _object_schema({}, []),
        "handler": lambda a: services.sync_git_status(),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _validate_arguments(tool, arguments):
    allowed = set(tool["inputSchema"]["properties"])
    unknown = set(arguments) - allowed
    if unknown:
        return f"unknown argument(s): {', '.join(sorted(unknown))}"
    missing = [f for f in tool["inputSchema"]["required"] if f not in arguments]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    return None


def _text_result(payload, is_error=False):
    # No indent and no \uXXXX escapes: this text goes straight into the model's
    # context, where whitespace and escapes are tokens that carry nothing.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _error_result(message):
    return _text_result({"error": message}, is_error=True)


def _call_tool(params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return _error_result(f"unknown tool {name!r}")

    problem = _validate_arguments(tool, arguments)
    if problem is not None:
        return _error_result(problem)

    try:
        payload = tool["handler"](arguments)
    except services.ServiceError as exc:
        # A ServiceError carries a message meant for a human (or an agent that can
        # read and correct itself), so it becomes isError content, not a transport
        # fault -- a JSON-RPC error is invisible to the model driving the tool call.
        if isinstance(exc, services.ValidationFailed):
            return _text_result({"error": "validation failed", "errors": exc.errors}, is_error=True)
        return _error_result(str(exc))
    except Exception as exc:  # noqa: BLE001 -- never let a handler bug become a 500
        return _error_result(f"internal error: {exc}")

    return _text_result(payload)


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def _rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _handle_initialize(params):
    requested = (params or {}).get("protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": SERVER_INFO,
    }


METHODS = {
    "initialize": lambda params: _handle_initialize(params),
    "ping": lambda params: {},
    "tools/list": lambda params: {"tools": [_public_tool(t) for t in TOOLS]},
    "tools/call": lambda params: _call_tool(params or {}),
}


def _public_tool(tool):
    return {k: tool[k] for k in ("name", "description", "inputSchema")}


def _is_notification(message):
    # A JSON-RPC request has an "id"; a notification omits it entirely and gets no
    # response body at all -- sending one back would itself violate the protocol.
    return "id" not in message


def _dispatch(message):
    method = message.get("method")
    params = message.get("params")
    handler = METHODS.get(method)
    if handler is None:
        return _rpc_error(message.get("id"), -32601, f"unknown method {method!r}")
    return _rpc_result(message.get("id"), handler(params))


def _is_valid_request_object(message):
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and isinstance(message.get("method"), str)
    )


@csrf_exempt
def mcp_endpoint(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        message = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(_rpc_error(None, -32700, "malformed JSON"))

    if isinstance(message, list):
        # Batching was removed in protocol 2025-06-18; reject rather than silently
        # process only the first element.
        return JsonResponse(_rpc_error(None, -32600, "batched requests are not supported"))

    if not _is_valid_request_object(message):
        return JsonResponse(_rpc_error(None, -32600, "not a valid JSON-RPC 2.0 request object"))

    if _is_notification(message):
        # notifications/initialized and any other notification: acknowledge with an
        # empty body. A JSON-RPC response body here would itself be a protocol
        # violation, so there is nothing to dispatch to and nothing to return.
        return HttpResponse(status=202)

    return JsonResponse(_dispatch(message))
