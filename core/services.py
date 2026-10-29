"""Operations on Project and Idea. No HTTP, no JSON-RPC, no ORM access outside this module.

`core/api.py` and `core/mcp.py` are adapters over these functions: they parse a request,
call one function here, and shape the result. A new operation is added once, here, and
surfaces in both places.
"""

import mimetypes
import os
import subprocess
from pathlib import Path

from django.core.files.base import ContentFile
from django.utils import timezone

from .forms import IdeaEditForm, IdeaForm, ProjectForm
from .models import (
    Artifact,
    ARTIFACT_EXTENSIONS,
    Idea,
    OPEN_IDEA_STATUSES,
    Project,
    Screenshot,
    SCREENSHOT_EXTENSIONS,
)

STATUS_TIMEOUT_SECONDS = 5

# 10 MB: generous enough for a full-page screenshot, small enough that a runaway
# upload cannot fill the disk out from under an unauthenticated endpoint.
SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024

# 2 MB: the MCP add_artifact tool takes the file as base64 in the tool call itself, so
# the model has to emit every byte -- fine for a log excerpt or a spec, not for an APK.
# Larger files go through the multipart API endpoint instead (ARTIFACT_MAX_BYTES).
MCP_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024

# 200 MB: the multipart API endpoint streams straight to disk (FILE_UPLOAD_MAX_MEMORY_SIZE),
# so it can take a real APK/AAB without the DATA_UPLOAD_MAX_MEMORY_SIZE cap on non-multipart
# bodies coming anywhere near it.
ARTIFACT_MAX_BYTES = 200 * 1024 * 1024

# A text artifact read back through read_artifact is capped independently of the
# upload size limits above -- this bounds what gets stuffed into a tool result, not
# what can be stored.
ARTIFACT_READ_TEXT_MAX_BYTES = 256 * 1024


class ServiceError(Exception):
    pass


class NotFound(ServiceError):
    pass


class Invalid(ServiceError):
    pass


class ValidationFailed(ServiceError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("validation failed")


def _resolve_project(ref):
    # An int, or a string of only digits, is a pk; anything else is a folder value.
    # This lets a session working in <workspace>/MusicBox call list_ideas(project="MusicBox")
    # without first looking up an id.
    is_pk = isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit())
    try:
        if is_pk:
            return Project.objects.get(pk=int(ref))
        return Project.objects.get(folder=ref)
    except Project.DoesNotExist:
        raise NotFound(f"no project with {'id' if is_pk else 'folder'} {ref!r}")


def _get_idea(idea_id):
    try:
        return (
            Idea.objects.select_related("project")
            .prefetch_related("screenshots", "artifacts")
            .get(pk=idea_id)
        )
    except Idea.DoesNotExist:
        raise NotFound(f"no idea with id {idea_id!r}")


def _get_screenshot(screenshot_id):
    try:
        return Screenshot.objects.select_related("idea", "idea__project").get(pk=screenshot_id)
    except Screenshot.DoesNotExist:
        raise NotFound(f"no screenshot with id {screenshot_id!r}")


def _get_artifact(artifact_id):
    try:
        return Artifact.objects.select_related("idea", "idea__project").get(pk=artifact_id)
    except Artifact.DoesNotExist:
        raise NotFound(f"no artifact with id {artifact_id!r}")


def _reject_unknown_keys(data, allowed_fields):
    unknown = set(data) - set(allowed_fields)
    if unknown:
        bad = ", ".join(sorted(unknown))
        raise Invalid(f"unknown field(s): {bad}")


# Must run before _reject_unknown_keys: "status" is not a form field, so the generic
# unknown-key error would otherwise shadow the message naming the transition function.
def _reject_status_write(data, transition_fn_name):
    if "status" in data:
        raise Invalid(f"status is not writable here; use {transition_fn_name} instead")


def _form_errors(form):
    # Plain {field: [str, ...]}, JSON-serializable -- get_json_data() nests message/code
    # dicts, which json.dumps handles fine, but a plain list of strings is simpler for
    # every caller (HTTP body, MCP isError text) to render without extra unpacking.
    return {field: [str(e) for e in errors] for field, errors in form.errors.items()}


def _save_via_form(form_class, data, instance=None):
    form = form_class(data, instance=instance)
    if not form.is_valid():
        raise ValidationFailed(_form_errors(form))
    return form.save()


def _apply_partial(instance, form_class, fields, data):
    # A ModelForm bound to a partial dict blanks every omitted field, so build a
    # complete dict from the instance's current values first and overlay the caller's
    # keys on top of it.
    merged = {field: getattr(instance, field) for field in fields}
    merged.update(data)
    return _save_via_form(form_class, merged, instance=instance)


def _transition(instance, target, transition_fn_name):
    if not instance.can_transition_to(target):
        allowed = ", ".join(t.target for t in instance.allowed_transitions()) or "none"
        raise Invalid(
            f"cannot move {instance.status!r} to {target!r} via {transition_fn_name}; "
            f"allowed targets: {allowed}"
        )
    instance.status = target
    instance.save()
    return instance


def project_to_dict(project):
    return {
        "id": project.pk,
        "name": project.name,
        "folder": project.folder,
        "description": project.description,
        "status": project.status,
        "status_display": project.get_status_display(),
        "git_status": project.git_status,
        "git_status_display": project.get_git_status_display(),
        "git_detail": project.git_detail,
        "git_checked_at": project.git_checked_at.isoformat() if project.git_checked_at else None,
        "root_path": project.root_path,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "allowed_transitions": [
            {"target": t.target, "label": t.label} for t in project.allowed_transitions()
        ],
    }


# A compact row's goal is a hint for picking an idea, not the goal itself -- get_idea
# returns it in full.
ROW_GOAL_MAX_CHARS = 160


def idea_to_row(idea):
    """The compact shape of an idea: enough to pick one, nothing to work from.

    list_ideas returns these to MCP callers by default (and to the API with
    ?full=0), because a full record per idea made one project's list hundreds of KB.
    """
    goal = " ".join(idea.goal.split())
    if len(goal) > ROW_GOAL_MAX_CHARS:
        goal = goal[: ROW_GOAL_MAX_CHARS - 1] + "…"
    return {
        "id": idea.pk,
        "project_folder": idea.project.folder,
        "project_name": idea.project.name,
        "title": idea.title,
        "kind": idea.kind,
        "status": idea.status,
        "has_plan": bool(idea.plan.strip()),
        "has_report": bool(idea.report.strip()),
        "goal": goal,
    }


def idea_to_dict(idea, include_prompt=True):
    data = {
        "id": idea.pk,
        "project_id": idea.project_id,
        "project_folder": idea.project.folder,
        "project_name": idea.project.name,
        "title": idea.title,
        "kind": idea.kind,
        "kind_display": idea.kind_prompt_value,
        "goal": idea.goal,
        "context": idea.context,
        "notes": idea.notes,
        "plan": idea.plan,
        "report": idea.report,
        "review": idea.review,
        "commits": idea.commits,
        "needs_verification": idea.needs_verification,
        "status": idea.status,
        "status_display": idea.get_status_display(),
        "created_at": idea.created_at.isoformat(),
        "updated_at": idea.updated_at.isoformat(),
        "allowed_transitions": [
            {"target": t.target, "label": t.label} for t in idea.allowed_transitions()
        ],
        "project_root_path": idea.project.root_path,
        "screenshots": [
            {
                "id": s.pk,
                "url": s.image.url,
                "caption": s.caption,
                "created_at": s.created_at.isoformat(),
            }
            for s in idea.screenshots.all()
        ],
        "artifacts": [
            {
                "id": a.pk,
                "filename": a.filename,
                "url": a.file.url,
                "caption": a.caption,
                "content_type": a.content_type,
                "size": a.size,
                "created_at": a.created_at.isoformat(),
            }
            for a in idea.artifacts.all()
        ],
    }
    if include_prompt:
        data["prompt"] = idea.build_prompt()
    return data


def list_projects(status=None, folder=None):
    qs = Project.objects.all()
    if status is not None:
        if status not in Project.Status.values:
            raise Invalid(f"unknown project status {status!r}")
        qs = qs.filter(status=status)
    if folder is not None:
        qs = qs.filter(folder=folder)
    return [project_to_dict(p) for p in qs]


def get_project(ref):
    return project_to_dict(_resolve_project(ref))


def create_project(data):
    _reject_status_write(data, "transition_project")
    _reject_unknown_keys(data, ProjectForm.Meta.fields)
    project = _save_via_form(ProjectForm, data)
    return project_to_dict(project)


def update_project(ref, data):
    _reject_status_write(data, "transition_project")
    _reject_unknown_keys(data, ProjectForm.Meta.fields)
    project = _resolve_project(ref)
    project = _apply_partial(project, ProjectForm, ProjectForm.Meta.fields, data)
    return project_to_dict(project)


def transition_project(ref, target):
    project = _resolve_project(ref)
    _transition(project, target, "transition_project")
    return project_to_dict(project)


# Git's own minimum unambiguous prefix length -- rejecting anything shorter keeps
# commit="" or commit="a" from matching every idea that has any commit at all.
COMMIT_QUERY_MIN_LENGTH = 4


def _commit_matches(stored, query):
    # Bidirectional prefix comparison: either side may be the abbreviated one -- a
    # short query hash against a full stored hash (git log/git blame output vs. a full
    # hash recorded by update_idea), or a short stored hash against a full query (an
    # idea recorded with an abbreviated hash, looked up later with git rev-parse).
    return stored == query or stored.startswith(query) or query.startswith(stored)


def list_ideas(project=None, status=None, kind=None, open_only=False, commit=None, full=True):
    qs = Idea.objects.select_related("project")
    if full:
        qs = qs.prefetch_related("screenshots", "artifacts")
    if project is not None:
        qs = qs.filter(project=_resolve_project(project))
    if status is not None:
        if status not in Idea.Status.values:
            raise Invalid(f"unknown idea status {status!r}")
        qs = qs.filter(status=status)
    if kind is not None:
        if kind not in Idea.Kind.values:
            raise Invalid(f"unknown idea kind {kind!r}")
        qs = qs.filter(kind=kind)
    if open_only:
        qs = qs.filter(status__in=OPEN_IDEA_STATUSES)
    if commit is not None:
        if len(commit) < COMMIT_QUERY_MIN_LENGTH:
            raise Invalid(
                f"commit query {commit!r} is shorter than {COMMIT_QUERY_MIN_LENGTH} characters"
            )
        qs = [i for i in qs if any(_commit_matches(c, commit) for c in i.commits)]
    to_dict = idea_to_dict if full else idea_to_row
    return [to_dict(i) for i in qs]


def get_idea(idea_id, include_prompt=True):
    return idea_to_dict(_get_idea(idea_id), include_prompt=include_prompt)


def create_idea(project_ref, data):
    _reject_status_write(data, "transition_idea")
    _reject_unknown_keys(data, IdeaForm.Meta.fields)
    project = _resolve_project(project_ref)
    idea = Idea(project=project)
    idea = _save_via_form(IdeaForm, data, instance=idea)
    return idea_to_dict(idea)


def update_idea(idea_id, data):
    _reject_status_write(data, "transition_idea")
    _reject_unknown_keys(data, IdeaEditForm.Meta.fields)
    idea = _get_idea(idea_id)
    if "project" in data:
        # A move. The reference is resolved like every other project argument (id or
        # folder), so the form only ever sees a pk and an unknown target is a NotFound
        # rather than a form error naming a choice list.
        data = {**data, "project": _resolve_project(data["project"]).pk}
    idea = _apply_partial(idea, IdeaEditForm, IdeaEditForm.Meta.fields, data)
    return idea_to_dict(idea)


def transition_idea(idea_id, target):
    idea = _get_idea(idea_id)
    _transition(idea, idea.resolve_target(target), "transition_idea")
    return idea_to_dict(idea)


def check_screenshot_upload(filename, size):
    """Validate a screenshot's name and size.

    Split out of add_screenshot so the multipart endpoint can reject an oversized
    upload from the file's declared size, rather than reading the whole thing into
    memory first only to throw it away.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SCREENSHOT_EXTENSIONS:
        allowed = ", ".join(sorted(SCREENSHOT_EXTENSIONS))
        raise Invalid(f"unsupported file extension {ext!r}; allowed: {allowed}")
    if size > SCREENSHOT_MAX_BYTES:
        raise Invalid(f"file is {size} bytes, over the {SCREENSHOT_MAX_BYTES}-byte limit")


def add_screenshot(idea_id, filename, content, caption=""):
    idea = _get_idea(idea_id)
    check_screenshot_upload(filename, len(content))
    screenshot = Screenshot(idea=idea, caption=caption)
    screenshot.image.save(filename, ContentFile(content), save=True)
    return idea_to_dict(_get_idea(idea_id))


def delete_screenshot(screenshot_id):
    screenshot = _get_screenshot(screenshot_id)
    idea_id = screenshot.idea_id
    screenshot.image.delete(save=False)
    screenshot.delete()
    return idea_to_dict(_get_idea(idea_id))


def check_artifact_upload(filename, size, limit):
    """Validate an artifact's name and size against `limit`.

    Split out of add_artifact, same reasoning as check_screenshot_upload: the
    multipart endpoint rejects an oversized upload from its declared size, before
    reading it into memory.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ARTIFACT_EXTENSIONS:
        allowed = ", ".join(sorted(ARTIFACT_EXTENSIONS))
        raise Invalid(f"unsupported file extension {ext!r}; allowed: {allowed}")
    if size > limit:
        raise Invalid(f"file is {size} bytes, over the {limit}-byte limit")


def add_artifact(idea_id, filename, content, caption=""):
    idea = _get_idea(idea_id)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    artifact = Artifact(idea=idea, caption=caption, content_type=content_type, size=len(content))
    artifact.file.save(filename, ContentFile(content), save=True)
    return idea_to_dict(_get_idea(idea_id))


def delete_artifact(artifact_id):
    artifact = _get_artifact(artifact_id)
    idea_id = artifact.idea_id
    artifact.file.delete(save=False)
    artifact.delete()
    return idea_to_dict(_get_idea(idea_id))


def read_artifact(artifact_id):
    """Metadata for one artifact, plus its text content when it is a text type
    (capped at ARTIFACT_READ_TEXT_MAX_BYTES) -- a binary artifact only ever gets its
    metadata and URL back, never its bytes.
    """
    artifact = _get_artifact(artifact_id)
    data = {
        "id": artifact.pk,
        "idea_id": artifact.idea_id,
        "filename": artifact.filename,
        "url": artifact.file.url,
        "caption": artifact.caption,
        "content_type": artifact.content_type,
        "size": artifact.size,
        "created_at": artifact.created_at.isoformat(),
        "is_text": artifact.is_text,
    }
    if artifact.is_text:
        with artifact.file.open("rb") as fh:
            raw = fh.read(ARTIFACT_READ_TEXT_MAX_BYTES + 1)
        truncated = len(raw) > ARTIFACT_READ_TEXT_MAX_BYTES
        data["content"] = raw[:ARTIFACT_READ_TEXT_MAX_BYTES].decode("utf-8", errors="replace")
        data["content_truncated"] = truncated
    return data


def _check_project_git_status(root_path):
    """Return (GitStatus value, detail string) for the repo at root_path.

    Read-only: runs `git status --porcelain=v2 --branch`, nothing that writes to the
    repo. Safe to run against every project's checkout, submodules included.
    """
    if not (Path(root_path) / ".git").exists():
        return Project.GitStatus.NOT_A_REPO, "no .git directory"

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Project.GitStatus.ERROR, str(exc)[:200]

    if result.returncode != 0:
        return Project.GitStatus.ERROR, result.stderr.strip()[:200]

    ahead, behind, has_upstream = 0, 0, False
    changed = 0
    for line in result.stdout.splitlines():
        if line.startswith("# branch.ab "):
            has_upstream = True
            # "# branch.ab +<ahead> -<behind>"
            parts = line.split()
            ahead = int(parts[2].lstrip("+"))
            behind = int(parts[3].lstrip("-"))
        elif line.startswith("#"):
            continue
        else:
            changed += 1

    if changed:
        detail_bits = [f"{changed} uncommitted"]
        if ahead:
            detail_bits.append(f"{ahead} unpushed")
        if behind:
            detail_bits.append(f"{behind} behind")
        return Project.GitStatus.DIRTY, " · ".join(detail_bits)

    if not has_upstream:
        return Project.GitStatus.NO_REMOTE, "no upstream branch configured"

    if ahead and behind:
        return Project.GitStatus.DIVERGED, f"{ahead} unpushed, {behind} behind"
    if ahead:
        return Project.GitStatus.AHEAD, f"{ahead} unpushed"
    if behind:
        return Project.GitStatus.BEHIND, f"{behind} behind"
    return Project.GitStatus.CLEAN, ""


def sync_git_status():
    """Check every project's Git status and store it on the Project record.

    Read-only against every project's checkout -- never writes to a repo, only to
    this app's database -- so it is safe to run against the real instance. Returns
    a list of per-project result dicts, one per Project, each noting whether the
    stored status/detail actually changed.
    """
    results = []
    for project in Project.objects.all():
        status, detail = _check_project_git_status(project.root_path)
        changed = status != project.git_status or detail != project.git_detail
        project.git_status = status
        project.git_detail = detail
        project.git_checked_at = timezone.now()
        project.save(update_fields=["git_status", "git_detail", "git_checked_at"])
        results.append(
            {
                "folder": project.folder,
                "git_status": status,
                "git_status_display": dict(Project.GitStatus.choices)[status],
                "git_detail": detail,
                "changed": changed,
            }
        )
    return results
