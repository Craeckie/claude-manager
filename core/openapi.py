"""Builds the OpenAPI 3.1 document served at /api/openapi.json.

Every field schema comes from core/schemas.py -- nothing here retypes a field description
or an enum, so this document and the MCP tool schemas (core/mcp.py) cannot disagree.
"""

from . import schemas as s

_REF_PROJECT = {"$ref": "#/components/schemas/Project"}
_REF_IDEA = {"$ref": "#/components/schemas/Idea"}
_REF_ERROR = {"$ref": "#/components/schemas/Error"}
_REF_VALIDATION_ERROR = {"$ref": "#/components/schemas/ValidationError"}
_REF_ARTIFACT = {"$ref": "#/components/schemas/Artifact"}


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _response(description, schema):
    return {"description": description, **_json(schema)}


_RESP_400 = _response("Malformed request.", _REF_ERROR)
_RESP_404 = _response("No matching record.", _REF_ERROR)
_RESP_422 = _response("Form validation failed.", _REF_VALIDATION_ERROR)


def _array(ref):
    return {"type": "array", "items": ref}


def _pk_param(description):
    return {
        "name": "pk",
        "in": "path",
        "required": True,
        "schema": {"type": "integer"},
        "description": description,
    }


def _query_param(name, description, enum=None, schema_type="string"):
    schema = {"type": schema_type}
    if enum is not None:
        schema["enum"] = enum
    return {"name": name, "in": "query", "required": False, "schema": schema, "description": description}


def _write_body(field_schemas, fields, required, extra_note):
    # "status" is deliberately excluded: it moves only through the transition
    # endpoints (core/services.py._reject_status_write), so it never appears here.
    return {
        "required": True,
        **_json(
            {
                "type": "object",
                "properties": {field: field_schemas[field] for field in fields},
                "required": required,
                "additionalProperties": False,
                "description": extra_note,
            }
        ),
    }


def _transition_body(targets):
    return {
        "required": True,
        **_json(
            {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": targets,
                        "description": "The status to move to. Must be one of the record's allowed_transitions.",
                    }
                },
                "required": ["target"],
                "additionalProperties": False,
            }
        ),
    }


def _project_create_body():
    return _write_body(
        s.PROJECT_FIELD_SCHEMAS,
        s.PROJECT_WRITE_FIELDS,
        ["name", "folder"],
        "Fields for a new project. status is not accepted; new projects start as 'idea'.",
    )


def _project_update_body():
    return _write_body(
        s.PROJECT_FIELD_SCHEMAS,
        s.PROJECT_WRITE_FIELDS,
        [],
        "Partial update -- send only the fields to change. status is not accepted; "
        "use POST .../transition/ instead.",
    )


def _idea_create_body():
    return _write_body(
        s.IDEA_FIELD_SCHEMAS,
        s.IDEA_WRITE_FIELDS,
        ["title"],
        "Fields for a new idea. status is not accepted; new ideas start as 'new'.",
    )


def _idea_update_body():
    return _write_body(
        s.IDEA_FIELD_SCHEMAS,
        s.IDEA_UPDATE_FIELDS,
        [],
        "Partial update -- send only the fields to change. project (an id or a folder "
        "name) moves the idea to another project, keeping everything else. status is "
        "not accepted; use POST .../transition/ instead.",
    )


def _paths():
    return {
        "/api/projects/": {
            "get": {
                "operationId": "list_projects",
                "summary": "List projects",
                "description": "All projects, optionally filtered by status or folder.",
                "parameters": [
                    _query_param("status", "Filter by lifecycle status.", enum=s.PROJECT_STATUS_VALUES),
                    _query_param("folder", "Filter by exact folder name."),
                ],
                "responses": {
                    "200": _response("The matching projects.", _array(_REF_PROJECT)),
                    "400": _RESP_400,
                },
            },
            "post": {
                "operationId": "create_project",
                "summary": "Create a project",
                "description": "Creates a new project. New projects start in the 'idea' status.",
                "requestBody": _project_create_body(),
                "responses": {
                    "201": _response("The created project.", _REF_PROJECT),
                    "400": _RESP_400,
                    "422": _RESP_422,
                },
            },
        },
        "/api/projects/{pk}/": {
            "get": {
                "operationId": "get_project",
                "summary": "Get a project",
                "description": "Fetches a single project by id.",
                "parameters": [_pk_param("The project's id.")],
                "responses": {"200": _response("The project.", _REF_PROJECT), "404": _RESP_404},
            },
            "patch": {
                "operationId": "update_project",
                "summary": "Update a project",
                "description": "Partial update: omitted fields keep their current value.",
                "parameters": [_pk_param("The project's id.")],
                "requestBody": _project_update_body(),
                "responses": {
                    "200": _response("The updated project.", _REF_PROJECT),
                    "400": _RESP_400,
                    "404": _RESP_404,
                    "422": _RESP_422,
                },
            },
        },
        "/api/projects/{pk}/transition/": {
            "post": {
                "operationId": "transition_project",
                "summary": "Move a project to a new status",
                "description": (
                    "The only way to change a project's status. Legal targets are listed "
                    "in the project's allowed_transitions; anything else is a 400."
                ),
                "parameters": [_pk_param("The project's id.")],
                "requestBody": _transition_body(s.PROJECT_TRANSITION_TARGETS),
                "responses": {
                    "200": _response("The project in its new status.", _REF_PROJECT),
                    "400": _RESP_400,
                    "404": _RESP_404,
                },
            }
        },
        "/api/projects/sync_git_status/": {
            "post": {
                "operationId": "sync_git_status",
                "summary": "Sync every project's Git status",
                "description": (
                    "Checks every project's Git status (clean, uncommitted, unpushed, "
                    "...) and stores it on the Project record. Read-only against every "
                    "project's checkout -- runs `git status`, never writes to a repo, "
                    "only to this app's database -- so it is safe to call directly "
                    "against the real instance. Takes no body. Only call this when "
                    "asked to update/sync/refresh Git status; nothing triggers it "
                    "automatically."
                ),
                "responses": {
                    "200": _response(
                        "One result per project.",
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "folder": {"type": "string"},
                                    "git_status": {"type": "string"},
                                    "git_status_display": {"type": "string"},
                                    "git_detail": {"type": "string"},
                                    "changed": {"type": "boolean"},
                                },
                            },
                        },
                    ),
                },
            }
        },
        "/api/projects/{pk}/ideas/": {
            "get": {
                "operationId": "list_project_ideas",
                "summary": "List a project's ideas",
                "description": "All ideas belonging to this project.",
                "parameters": [_pk_param("The project's id.")],
                "responses": {"200": _response("The project's ideas.", _array(_REF_IDEA)), "404": _RESP_404},
            },
            "post": {
                "operationId": "create_idea",
                "summary": "Create an idea under a project",
                "description": "Creates a new idea. New ideas start in the 'new' status.",
                "parameters": [_pk_param("The owning project's id.")],
                "requestBody": _idea_create_body(),
                "responses": {
                    "201": _response("The created idea.", _REF_IDEA),
                    "400": _RESP_400,
                    "404": _RESP_404,
                    "422": _RESP_422,
                },
            },
        },
        "/api/ideas/": {
            "get": {
                "operationId": "list_ideas",
                "summary": "List ideas",
                "description": "All ideas, optionally filtered by project, status, or openness.",
                "parameters": [
                    _query_param("project", "A project id or folder name."),
                    _query_param("status", "Filter by lifecycle status.", enum=s.IDEA_STATUS_VALUES),
                    _query_param("kind", "Filter by idea type.", enum=s.IDEA_KIND_VALUES),
                    _query_param(
                        "open",
                        "Set to 1/true to return only open ideas (new or planned), "
                        "excluding implemented and abandoned.",
                        schema_type="boolean",
                    ),
                    _query_param(
                        "commit",
                        "A commit hash, full or abbreviated (at least 4 characters), "
                        "that implemented the idea -- matches whichever side of the "
                        "comparison is the shorter prefix.",
                    ),
                    _query_param(
                        "full",
                        "Set to 0/false for compact rows (id, project_folder, project_name, "
                        "title, kind, status, has_plan, has_report, goal cut to 160 chars) "
                        "instead of full records. Defaults to full.",
                        schema_type="boolean",
                    ),
                ],
                "responses": {
                    "200": _response("The matching ideas.", _array(_REF_IDEA)),
                    "400": _RESP_400,
                    "404": _RESP_404,
                },
            }
        },
        "/api/ideas/{pk}/": {
            "get": {
                "operationId": "get_idea",
                "summary": "Get an idea",
                "description": (
                    "Fetches a single idea by id. The 'prompt' field is the ready-to-execute "
                    "instruction text; 'project_root_path' is the directory to cd into first."
                ),
                "parameters": [_pk_param("The idea's id.")],
                "responses": {"200": _response("The idea.", _REF_IDEA), "404": _RESP_404},
            },
            "patch": {
                "operationId": "update_idea",
                "summary": "Update an idea",
                "description": "Partial update: omitted fields keep their current value.",
                "parameters": [_pk_param("The idea's id.")],
                "requestBody": _idea_update_body(),
                "responses": {
                    "200": _response("The updated idea.", _REF_IDEA),
                    "400": _RESP_400,
                    "404": _RESP_404,
                    "422": _RESP_422,
                },
            },
        },
        "/api/ideas/{pk}/transition/": {
            "post": {
                "operationId": "transition_idea",
                "summary": "Move an idea to a new status",
                "description": (
                    "The only way to change an idea's status. Call with target: 'verify' "
                    "once implementation work is done -- the report and any screenshots "
                    "should be attached first. Moving to 'implemented' is the user's own "
                    "move, after checking the report. On an idea whose needs_verification "
                    "is false, 'verify' closes it as 'implemented' instead. Legal "
                    "targets are listed in the "
                    "idea's allowed_transitions; anything else is a 400."
                ),
                "parameters": [_pk_param("The idea's id.")],
                "requestBody": _transition_body(s.IDEA_TRANSITION_TARGETS),
                "responses": {
                    "200": _response("The idea in its new status.", _REF_IDEA),
                    "400": _RESP_400,
                    "404": _RESP_404,
                },
            }
        },
        "/api/ideas/{pk}/screenshots/": {
            "post": {
                "operationId": "add_screenshot",
                "summary": "Attach a screenshot to an idea",
                "description": (
                    "Multipart upload (like curl -F): a 'file' part (png/jpg/jpeg/gif/webp, "
                    "10 MB max) and an optional 'caption' part. Returns the updated idea."
                ),
                "parameters": [_pk_param("The idea's id.")],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string", "format": "binary"},
                                    "caption": {"type": "string"},
                                },
                                "required": ["file"],
                            }
                        }
                    },
                },
                "responses": {
                    "201": _response("The idea, with the new screenshot attached.", _REF_IDEA),
                    "400": _RESP_400,
                    "404": _RESP_404,
                },
            }
        },
        "/api/screenshots/{pk}/": {
            "delete": {
                "operationId": "delete_screenshot",
                "summary": "Remove a screenshot",
                "description": "Deletes the screenshot and its stored file.",
                "parameters": [_pk_param("The screenshot's id.")],
                "responses": {
                    "200": _response("The idea, with the screenshot removed.", _REF_IDEA),
                    "404": _RESP_404,
                },
            }
        },
        "/api/ideas/{pk}/artifacts/": {
            "post": {
                "operationId": "add_artifact",
                "summary": "Attach an artifact to an idea",
                "description": (
                    "Multipart upload (like curl -F): a 'file' part (any of the allowed "
                    "extensions -- logs, specs, binaries; 200 MB max) and an optional "
                    "'caption' part. Attachable in every idea status, unlike a screenshot. "
                    "Returns the updated idea."
                ),
                "parameters": [_pk_param("The idea's id.")],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string", "format": "binary"},
                                    "caption": {"type": "string"},
                                },
                                "required": ["file"],
                            }
                        }
                    },
                },
                "responses": {
                    "201": _response("The idea, with the new artifact attached.", _REF_IDEA),
                    "400": _RESP_400,
                    "404": _RESP_404,
                },
            }
        },
        "/api/artifacts/{pk}/": {
            "get": {
                "operationId": "get_artifact",
                "summary": "Fetch an artifact's metadata",
                "description": (
                    "Metadata for one artifact. Pass ?content=1 to also get its text "
                    "content (only for text-type artifacts, capped at 256 KB)."
                ),
                "parameters": [
                    _pk_param("The artifact's id."),
                    _query_param("content", "Set to 1 to include text content.", schema_type="string"),
                ],
                "responses": {
                    "200": _response("The artifact.", _REF_ARTIFACT),
                    "404": _RESP_404,
                },
            },
            "delete": {
                "operationId": "delete_artifact",
                "summary": "Remove an artifact",
                "description": "Deletes the artifact and its stored file.",
                "parameters": [_pk_param("The artifact's id.")],
                "responses": {
                    "200": _response("The idea, with the artifact removed.", _REF_IDEA),
                    "404": _RESP_404,
                },
            },
        },
    }


def build_schema(request):
    # "Try it out" in Swagger UI has to hit whatever host the docs were opened on,
    # not a hardcoded one -- this project has no fixed public hostname.
    base_url = request.build_absolute_uri("/").rstrip("/")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Claude Manager API",
            "version": "1.0.0",
            "description": (
                "CRUD and lifecycle transitions for Projects and Ideas. Unauthenticated: "
                "anyone who can reach this host can read and write every record (see CLAUDE.md)."
            ),
        },
        "servers": [{"url": base_url}],
        "paths": _paths(),
        "components": {
            "schemas": {
                "Project": s.PROJECT_SCHEMA,
                "Idea": s.IDEA_SCHEMA,
                "Error": s.ERROR_SCHEMA,
                "ValidationError": s.VALIDATION_ERROR_SCHEMA,
                "Transition": s.TRANSITION_SCHEMA,
                "Screenshot": s.SCREENSHOT_SCHEMA,
                "Artifact": s.ARTIFACT_SCHEMA,
            }
        },
    }
