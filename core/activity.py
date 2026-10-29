"""The activity log (#361): who changed which idea or project, and when.

Recording is done by model signals, so every write path -- the htmx views, the API, MCP
and the admin -- is covered by the same code and a new write path cannot forget it. The
source comes from a context variable that `ActivitySourceMiddleware` sets per request;
a write with no source (a management command, a test's setUp) is not recorded.

`group_events()` folds the raw log into feed entries for the /activity/ page.
"""

import contextlib
import contextvars
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import Artifact, Event, Idea, Project, Screenshot, idea_detail_url

_source = contextvars.ContextVar("activity_source", default=None)

# What counts as a change worth showing. Project.git_* is excluded on purpose: a git
# status sync rewrites it on every project and is not anybody's action.
IDEA_FIELDS = ("project", "title", "kind", "goal", "context", "notes", "plan", "report", "review", "commits")
PROJECT_FIELDS = ("name", "folder", "description")

GROUP_WINDOW_MINUTES = 15
FEED_DAYS = 14
FEED_LIMIT = 500

# Path prefix -> source; anything else is the web UI.
_PREFIX_SOURCES = (
    ("/api/", Event.Source.API),
    ("/mcp", Event.Source.MCP),
    ("/admin/", Event.Source.ADMIN),
)


def source_for_path(path):
    for prefix, source in _PREFIX_SOURCES:
        if path.startswith(prefix):
            return source
    return Event.Source.WEB


@contextlib.contextmanager
def source(value):
    """Record writes inside the block as coming from `value`."""
    token = _source.set(value)
    try:
        yield
    finally:
        _source.reset(token)


class ActivitySourceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with source(source_for_path(request.path)):
            return self.get_response(request)


# --------------------------------------------------------------------------- recording


def _record(**kwargs):
    Event.objects.create(source=_source.get(), **kwargs)


def _tracked(instance, names):
    return {
        name: (instance.project_id if name == "project" else getattr(instance, name))
        for name in (*names, "status")
    }


@receiver(pre_save, sender=Idea)
@receiver(pre_save, sender=Project)
def _remember_before(sender, instance, **kwargs):
    if _source.get() is None or instance._state.adding:
        return
    names = IDEA_FIELDS if sender is Idea else PROJECT_FIELDS
    old = sender._base_manager.filter(pk=instance.pk).first()
    instance._activity_before = _tracked(old, names) if old is not None else None


@receiver(post_save, sender=Idea)
def _idea_saved(sender, instance, created, **kwargs):
    if _source.get() is None:
        return
    common = {
        "idea_id": instance.pk,
        "idea_title": instance.title,
        "project_id": instance.project_id,
        "project_name": instance.project.name,
    }
    if created:
        _record(action=Event.Action.IDEA_CREATED, to_status=instance.status, **common)
        return
    before = getattr(instance, "_activity_before", None)
    if before is None:
        return
    after = _tracked(instance, IDEA_FIELDS)
    changed = [name for name in IDEA_FIELDS if before[name] != after[name]]
    status_changed = before["status"] != after["status"]
    if not changed and not status_changed:
        return
    detail = ""
    if "project" in changed:
        old_name = Project._base_manager.filter(pk=before["project"]).values_list("name", flat=True).first()
        detail = f"{old_name or '?'} → {instance.project.name}"
    _record(
        action=Event.Action.IDEA_CHANGED,
        fields=changed,
        from_status=before["status"] if status_changed else "",
        to_status=after["status"] if status_changed else "",
        detail=detail,
        **common,
    )


@receiver(post_save, sender=Project)
def _project_saved(sender, instance, created, **kwargs):
    if _source.get() is None:
        return
    common = {"project_id": instance.pk, "project_name": instance.name}
    if created:
        _record(action=Event.Action.PROJECT_CREATED, to_status=instance.status, **common)
        return
    before = getattr(instance, "_activity_before", None)
    if before is None:
        return
    after = _tracked(instance, PROJECT_FIELDS)
    changed = [name for name in PROJECT_FIELDS if before[name] != after[name]]
    status_changed = before["status"] != after["status"]
    if not changed and not status_changed:
        return
    _record(
        action=Event.Action.PROJECT_CHANGED,
        fields=changed,
        from_status=before["status"] if status_changed else "",
        to_status=after["status"] if status_changed else "",
        **common,
    )


@receiver(post_delete, sender=Idea)
def _idea_deleted(sender, instance, **kwargs):
    if _source.get() is None:
        return
    project = Project._base_manager.filter(pk=instance.project_id).values_list("name", flat=True).first()
    _record(
        action=Event.Action.IDEA_DELETED,
        idea_id=instance.pk,
        idea_title=instance.title,
        project_id=instance.project_id,
        project_name=project or "",
    )


_FILE_ACTIONS = {
    (Screenshot, True): Event.Action.SCREENSHOT_ADDED,
    (Screenshot, False): Event.Action.SCREENSHOT_DELETED,
    (Artifact, True): Event.Action.ARTIFACT_ADDED,
    (Artifact, False): Event.Action.ARTIFACT_DELETED,
}


def _file_event(sender, instance, added):
    if _source.get() is None:
        return
    # By lookup, not instance.idea: inside an idea's cascade delete the cached
    # relation may be gone, and the row itself may follow right after.
    idea = (
        Idea._base_manager.filter(pk=instance.idea_id)
        .values("title", "project_id", "project__name")
        .first()
    ) or {}
    file = instance.image if sender is Screenshot else instance.file
    _record(
        action=_FILE_ACTIONS[(sender, added)],
        idea_id=instance.idea_id,
        idea_title=idea.get("title", ""),
        project_id=idea.get("project_id"),
        project_name=idea.get("project__name", ""),
        # The view deletes the file before the row, which clears its name.
        detail=(file.name or "").rsplit("/", 1)[-1],
    )


@receiver(post_save, sender=Screenshot)
@receiver(post_save, sender=Artifact)
def _file_saved(sender, instance, created, **kwargs):
    if created:
        _file_event(sender, instance, added=True)


@receiver(post_delete, sender=Screenshot)
@receiver(post_delete, sender=Artifact)
def _file_deleted(sender, instance, **kwargs):
    _file_event(sender, instance, added=False)


# --------------------------------------------------------------------------- grouping

_FIELD_LABELS = {"kind": "type"}

_FILE_NOUNS = {
    Event.Action.SCREENSHOT_ADDED: ("screenshot", "added"),
    Event.Action.SCREENSHOT_DELETED: ("screenshot", "deleted"),
    Event.Action.ARTIFACT_ADDED: ("artifact", "added"),
    Event.Action.ARTIFACT_DELETED: ("artifact", "deleted"),
}


@dataclass
class Group:
    """One feed entry: consecutive changes by one source to one idea (or project)."""

    source: str
    idea_id: int | None
    project_id: int | None
    title: str
    project_name: str
    events: list = field(default_factory=list)
    # Set by the view: whether the idea still exists, so the link is not a 404.
    exists: bool = True

    @property
    def start(self):
        return self.events[0].created_at

    @property
    def end(self):
        return self.events[-1].created_at

    @property
    def is_agent(self):
        return self.source in Event.AGENT_SOURCES

    @property
    def source_label(self):
        return Event.Source(self.source).label

    @property
    def url(self):
        return idea_detail_url(self.idea_id) if self.idea_id and self.exists else ""

    @property
    def summary(self):
        """What happened, in order of first occurrence, repeats folded together."""
        parts = {}  # key -> list of values, insertion-ordered
        for event in self.events:
            action = event.action
            if action in (Event.Action.IDEA_CREATED, Event.Action.PROJECT_CREATED):
                parts.setdefault("created", [])
            elif action == Event.Action.IDEA_DELETED:
                parts.setdefault("deleted", [])
            elif action in _FILE_NOUNS:
                parts.setdefault(action, []).append(event)
            else:
                edited = [f for f in event.fields if f != "project"]
                if edited:
                    names = parts.setdefault("edited", [])
                    names.extend(f for f in edited if f not in names)
                if "project" in event.fields:
                    parts.setdefault("moved", []).append(event.detail)
                if event.to_status:
                    chain = parts.setdefault("status", [])
                    if not chain or chain[-1] != event.from_status:
                        chain.append(event.from_status)
                    chain.append(event.to_status)
        summary = []
        for key, values in parts.items():
            if key == "created":
                summary.append("created")
            elif key == "deleted":
                summary.append("deleted")
            elif key == "edited":
                summary.append(", ".join(_FIELD_LABELS.get(v, v) for v in values) + " edited")
            elif key == "moved":
                summary.extend(f"moved {detail}" for detail in values)
            elif key == "status":
                summary.append(" → ".join(values))
            else:
                noun, verb = _FILE_NOUNS[key]
                count = len(values)
                summary.append(f"{count} {noun}s {verb}" if count > 1 else f"{noun} {verb}")
        return summary


def _subject(event):
    if event.action in (Event.Action.PROJECT_CREATED, Event.Action.PROJECT_CHANGED):
        return ("project", event.project_id)
    return ("idea", event.idea_id)


def group_events(events):
    """Fold events into feed entries, newest-ending first.

    An event joins its subject's latest entry when the source is the same and it came
    within GROUP_WINDOW_MINUTES of that entry's last event. Because each subject keeps
    only its latest entry, a change by anyone else in between starts a new one. Events
    on other subjects may interleave: an agent often works several ideas at once.
    """
    window = timedelta(minutes=GROUP_WINDOW_MINUTES)
    groups = []
    current = {}
    for event in sorted(events, key=lambda e: (e.created_at, e.pk or 0)):
        key = _subject(event)
        group = current.get(key)
        if group is None or group.source != event.source or event.created_at - group.end > window:
            is_project = key[0] == "project"
            group = Group(
                source=event.source,
                idea_id=None if is_project else event.idea_id,
                project_id=event.project_id,
                title=event.project_name if is_project else event.idea_title,
                project_name=event.project_name,
            )
            groups.append(group)
            current[key] = group
        group.events.append(event)
        # The newest snapshot wins; a move changes the project mid-entry.
        group.project_id = event.project_id
        group.project_name = event.project_name
    groups.sort(key=lambda g: (g.end, g.events[-1].pk or 0), reverse=True)
    return groups


def display_zone():
    return ZoneInfo(settings.DISPLAY_TIME_ZONE)
