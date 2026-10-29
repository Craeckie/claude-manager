"""Reusable JSON Schema fragments and field metadata.

Consumed by both `core/openapi.py` and `core/mcp.py` so the two descriptions of a field
cannot disagree. Everything here is derived from `core.models` / `core.transitions` /
`core.forms` rather than retyped, so it can never fall behind them.
"""

from .forms import IdeaEditForm, IdeaForm, ProjectForm
from .models import Idea, Project
from .transitions import IDEA_TRANSITIONS, PROJECT_TRANSITIONS

PROJECT_STATUS_VALUES = list(Project.Status.values)
PROJECT_GIT_STATUS_VALUES = list(Project.GitStatus.values)
IDEA_STATUS_VALUES = list(Idea.Status.values)
IDEA_KIND_VALUES = list(Idea.Kind.values)


def _transition_targets(transitions_table):
    targets = {t.target for targets in transitions_table.values() for t in targets}
    return sorted(targets)


PROJECT_TRANSITION_TARGETS = _transition_targets(PROJECT_TRANSITIONS)
IDEA_TRANSITION_TARGETS = _transition_targets(IDEA_TRANSITIONS)

PROJECT_WRITE_FIELDS = tuple(ProjectForm.Meta.fields)
IDEA_WRITE_FIELDS = tuple(IdeaForm.Meta.fields)
# An update additionally accepts `project` -- a move; a create gets its project from
# the URL or the tool argument instead, which is why the two tuples differ.
IDEA_UPDATE_FIELDS = tuple(IdeaEditForm.Meta.fields)


def _help_text(model, field_name):
    return model._meta.get_field(field_name).help_text or None


PROJECT_FIELD_SCHEMAS = {
    "name": {"type": "string", "description": "The project's display name. Must be unique."},
    "folder": {
        "type": "string",
        "description": (
            "The project's directory name under the workspace root. Must be unique; used "
            "to resolve a project by folder instead of id."
        ),
    },
    "description": {
        "type": "string",
        "description": "Free-form summary of what the project is.",
    },
}

IDEA_FIELD_SCHEMAS = {
    "project": {
        # Same union as the MCP project argument: an id or a folder name.
        "type": ["string", "integer"],
        "description": (
            "Move the idea to this project, given as its id or its folder name. The "
            "idea keeps its id, status, sections, plan, report, review, commits, "
            "screenshots and artifacts. Only accepted on an update, not on a create."
        ),
    },
    "title": {"type": "string", "description": "Short summary of the idea."},
    "kind": {
        "type": "string",
        "enum": IDEA_KIND_VALUES,
        "description": _help_text(Idea, "kind"),
    },
    "goal": {"type": "string", "description": _help_text(Idea, "goal")},
    "context": {"type": "string", "description": _help_text(Idea, "context")},
    "notes": {"type": "string", "description": _help_text(Idea, "notes")},
    "plan": {"type": "string", "description": _help_text(Idea, "plan")},
    "report": {"type": "string", "description": _help_text(Idea, "report")},
    "review": {"type": "string", "description": _help_text(Idea, "review")},
    "commits": {
        "type": "array",
        "items": {"type": "string"},
        "description": _help_text(Idea, "commits"),
    },
    "needs_verification": {
        "type": "boolean",
        "description": _help_text(Idea, "needs_verification") + " Defaults to false.",
    },
}

SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "url": {"type": "string", "description": "URL to fetch the stored image from."},
        "caption": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "url", "caption", "created_at"],
}

ARTIFACT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "filename": {"type": "string"},
        "url": {"type": "string", "description": "URL to fetch the stored file from."},
        "caption": {"type": "string"},
        "content_type": {"type": "string"},
        "size": {"type": "integer", "description": "Size of the stored file, in bytes."},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "filename", "url", "caption", "content_type", "size", "created_at"],
}

TRANSITION_SCHEMA = {
    "type": "object",
    "description": "A legal move out of a record's current status.",
    "properties": {
        "target": {"type": "string", "description": "The status this transition moves to."},
        "label": {"type": "string", "description": "Human-readable label for the transition."},
    },
    "required": ["target", "label"],
}

ERROR_SCHEMA = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}

VALIDATION_ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "errors": {
            "type": "object",
            "description": "Per-field validation messages.",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        }
    },
    "required": ["errors"],
}

PROJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "name": PROJECT_FIELD_SCHEMAS["name"],
        "folder": PROJECT_FIELD_SCHEMAS["folder"],
        "description": PROJECT_FIELD_SCHEMAS["description"],
        "status": {
            "type": "string",
            "enum": PROJECT_STATUS_VALUES,
            "description": "Lifecycle status. Change it with transition_project, not here.",
        },
        "status_display": {"type": "string", "description": "Human-readable status label."},
        "git_status": {
            "type": "string",
            "enum": PROJECT_GIT_STATUS_VALUES,
            "description": (
                "Git status as of the last `sync_git_status` management command run "
                "(clean, uncommitted, unpushed, ...). Run by hand, so this can be stale."
            ),
        },
        "git_status_display": {"type": "string", "description": "Human-readable git status label."},
        "git_detail": {
            "type": "string",
            "description": "Short detail for git_status, e.g. counts of uncommitted/unpushed changes.",
        },
        "git_checked_at": {
            "type": ["string", "null"],
            "format": "date-time",
            "description": "When git_status was last synced, or null if never.",
        },
        "root_path": {
            "type": "string",
            "description": "Absolute path to the project's directory on disk.",
        },
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "allowed_transitions": {"type": "array", "items": TRANSITION_SCHEMA},
    },
    "required": [
        "id",
        "name",
        "folder",
        "description",
        "status",
        "status_display",
        "git_status",
        "git_status_display",
        "git_detail",
        "git_checked_at",
        "root_path",
        "created_at",
        "updated_at",
        "allowed_transitions",
    ],
}

IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "project_id": {"type": "integer"},
        "project_folder": {"type": "string"},
        "project_name": {"type": "string"},
        "title": IDEA_FIELD_SCHEMAS["title"],
        "kind": IDEA_FIELD_SCHEMAS["kind"],
        "kind_display": {
            "type": "string",
            "description": (
                "Human-readable type label. Blank when kind is \"other\" -- that value "
                "means no type was set, not a real category, so it is never displayed."
            ),
        },
        "goal": IDEA_FIELD_SCHEMAS["goal"],
        "context": IDEA_FIELD_SCHEMAS["context"],
        "notes": IDEA_FIELD_SCHEMAS["notes"],
        "plan": IDEA_FIELD_SCHEMAS["plan"],
        "report": IDEA_FIELD_SCHEMAS["report"],
        "review": IDEA_FIELD_SCHEMAS["review"],
        "commits": IDEA_FIELD_SCHEMAS["commits"],
        "needs_verification": IDEA_FIELD_SCHEMAS["needs_verification"],
        "status": {
            "type": "string",
            "enum": IDEA_STATUS_VALUES,
            "description": "Lifecycle status. Change it with transition_idea, not here.",
        },
        "status_display": {"type": "string", "description": "Human-readable status label."},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "allowed_transitions": {"type": "array", "items": TRANSITION_SCHEMA},
        "prompt": {
            "type": "string",
            "description": "The ready-to-execute instruction text (Idea.build_prompt()).",
        },
        "project_root_path": {
            "type": "string",
            "description": "Absolute path to `cd` into before acting on this idea.",
        },
        "screenshots": {
            "type": "array",
            "items": SCREENSHOT_SCHEMA,
            "description": "Screenshots attached to this idea, oldest first.",
        },
        "artifacts": {
            "type": "array",
            "items": ARTIFACT_SCHEMA,
            "description": (
                "Any files attached to this idea (logs, specs, binaries, ...), oldest "
                "first -- unlike screenshots, attachable in every idea status."
            ),
        },
    },
    "required": [
        "id",
        "project_id",
        "project_folder",
        "project_name",
        "title",
        "kind",
        "kind_display",
        "goal",
        "context",
        "notes",
        "plan",
        "report",
        "review",
        "commits",
        "needs_verification",
        "status",
        "status_display",
        "created_at",
        "updated_at",
        "allowed_transitions",
        "prompt",
        "project_root_path",
        "screenshots",
        "artifacts",
    ],
}
