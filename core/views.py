import hashlib
import json
import os
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Prefetch
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseNotModified
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.views.static import serve as serve_static

from . import activity, services
from .forms import IdeaCreateForm, IdeaEditForm, ProjectForm
from .models import (
    ARTIFACT_SANDBOXED_EXTENSIONS,
    CLOSED_PROJECT_STATUSES,
    OPEN_IDEA_STATUSES,
    Artifact,
    Event,
    Idea,
    Project,
    Screenshot,
)
from .security import harden_media_response


def offline(request):
    return render(request, "offline.html")


def service_worker(request):
    # `debug` decides whether the worker caches /static/ at all: unhashed dev
    # filenames must not be cache-first. See templates/sw.js.
    return render(
        request,
        "sw.js",
        {"debug": settings.DEBUG},
        content_type="application/javascript",
    )


# The pages a card can sit on (#384): the Ideas view at /, the Projects view, one
# project's page and one idea's page. Every card URL carries the page it was rendered
# for (`?view=`, next to `?closed=1`), because the card endpoints answer differently
# per page: whether a closed or moved record leaves, and where a JS-off form returns.
PAGE_VIEWS = ("ideas", "projects", "project", "idea")


class Page:
    def __init__(self, view="ideas", show_closed=False):
        self.view = view
        self.show_closed = show_closed

    @classmethod
    def from_request(cls, request):
        view = request.GET.get("view", "")
        return cls(view if view in PAGE_VIEWS else "ideas", request.GET.get("closed") == "1")

    @property
    def qs(self):
        """The query string every card URL on this page carries; "" on a plain Ideas view."""
        params = {}
        if self.view != "ideas":
            params["view"] = self.view
        if self.show_closed:
            params["closed"] = "1"
        return "?" + urlencode(params) if params else ""

    def url(self, project=None, idea=None):
        """The page itself, for a JS-off redirect or a Cancel link."""
        if self.view == "idea" and idea is not None:
            url = reverse("core:idea_detail", args=[idea.pk])
        elif self.view == "project" and project is not None:
            url = reverse("core:project_detail", args=[project.folder])
        elif self.view == "projects":
            url = reverse("core:projects")
        else:
            url = reverse("core:home")
        return url + ("?closed=1" if self.show_closed else "")

    def redirect(self, project=None, idea=None):
        return redirect(self.url(project, idea))

    def context(self):
        return {"view": self.view, "show_closed": self.show_closed, "qs": self.qs}


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _render_idea_card(request, idea, page, **extra):
    # An idea that just moved to a closed status is no longer listed, so an empty
    # body makes the outerHTML swap remove the row. Its own page keeps it.
    if idea.is_closed and not page.show_closed and page.view != "idea":
        return HttpResponse("")
    return render(request, "core/_idea.html", {"idea": idea, **page.context(), **extra})


def _new_idea_form(data=None):
    # htmx focuses an [autofocus] element in swapped-in content, so the cursor
    # lands in the form — the click has a visible effect even off-screen.
    form = IdeaCreateForm(data)
    form.fields["title"].widget.attrs["autofocus"] = True
    return form


def _project_ideas(project, page):
    """The ideas a project's list shows, closed ones only under ?closed=1."""
    idea_qs = project.ideas.all() if page.show_closed else project.ideas.filter(status__in=OPEN_IDEA_STATUSES)
    return list(idea_qs.prefetch_related("screenshots", "artifacts"))


def _render_project_card(request, project, page):
    # A project that just moved to a closed status is no longer listed, so an empty
    # body makes the outerHTML swap remove the row -- same as _render_idea_card. Its
    # own page keeps it, and re-renders its header there instead of a row.
    if project.status in CLOSED_PROJECT_STATUSES and not page.show_closed and page.view != "project":
        return HttpResponse("")
    project.visible_ideas = _project_ideas(project, page)
    context = {"project": project, **page.context()}
    if page.view == "project":
        return render(request, "core/_project_head.html", context)
    return render(request, "core/_project_row.html", context)


def _idea_matches(idea, query):
    """Whether a project page's search box shows `idea`; static/js/idea-filter.js applies
    the same rule. Digits (optionally `#`-prefixed) match that idea's id exactly."""
    if not query:
        return True
    if re.fullmatch(r"#?\d+", query):
        return query.lstrip("#") == str(idea.pk)
    return query.lower() in f"#{idea.pk} {idea.title} {idea.goal}".lower()


def _render_project_ideas(request, project, page):
    """A project page's idea list with a fresh add slot, after an idea was added."""
    context = {"project": project, "ideas": _project_ideas(project, page), "lazy": True, **page.context()}
    return render(request, "core/_project_ideas.html", context)


_PLACEHOLDER_PK = 999999999999


def _url_template(name, page):
    # The blank <template id="new-idea-form"> (_new_idea_template.html) is cloned client-side
    # for any project, and the background poll fetches any card, so neither can
    # carry one record's URL. Reversing with a placeholder pk and swapping the
    # digits for a token keeps the real route pattern (core/urls.py) as the single
    # source of truth instead of hand-typing it a second time in the template.
    url = reverse(name, args=[_PLACEHOLDER_PK]).replace(str(_PLACEHOLDER_PK), "__PK__")
    return url + page.qs


def _project_board(show_closed):
    """The projects the Projects view lists, each with its shown ideas as
    `visible_ideas` (for the open-idea counts). Shared with `changes`, so what the
    page polls is exactly what it renders."""
    idea_qs = Idea.objects.all() if show_closed else Idea.objects.filter(status__in=OPEN_IDEA_STATUSES)
    project_qs = Project.objects.all() if show_closed else Project.objects.exclude(status__in=CLOSED_PROJECT_STATUSES)
    return list(project_qs.prefetch_related(Prefetch("ideas", queryset=idea_qs, to_attr="visible_ideas")))


# The Ideas view's sections, in page order: the ones waiting on the user first.
IDEA_SECTIONS = [Idea.Status.VERIFY, Idea.Status.PLANNED, Idea.Status.NEW]
CLOSED_IDEA_SECTIONS = [Idea.Status.IMPLEMENTED, Idea.Status.ABANDONED]


def _idea_board_statuses(page):
    return IDEA_SECTIONS + (CLOSED_IDEA_SECTIONS if page.show_closed else [])


def _idea_board(page):
    """Every idea the Ideas view shows, newest first. Shared with `changes`."""
    # A verify idea's card shows its screenshots and every card its artifacts, so
    # both are prefetched here, batched, rather than queried per card.
    return list(
        Idea.objects.filter(status__in=_idea_board_statuses(page))
        .select_related("project")
        .prefetch_related("screenshots", "artifacts")
    )


@require_http_methods(["GET"])
def changes(request):
    """What static/js/board.js diffs against the cards on a page (#322, #384).

    Ideas as [id, project id, status, card version], projects as {id: header
    signature}, for one page's scope: `?idea=<id>` (its page), `?project=<id>` (its
    page: its ideas and its header), `?view=projects` (rows only) or, by default,
    the Ideas view. The page refreshes only what differs, in place. An unchanged
    scope answers 304 to the poll's If-None-Match, so idling is cheap.
    """
    page = Page.from_request(request)
    ideas, projects = [], []
    idea_ref = request.GET.get("idea", "")
    project_ref = request.GET.get("project", "")
    if idea_ref:
        # An idea's page keeps it whatever its status.
        ideas = list(Idea.objects.filter(pk=idea_ref if idea_ref.isdigit() else -1)
                     .prefetch_related("screenshots", "artifacts"))
    elif project_ref:
        project = get_object_or_404(Project, pk=project_ref if project_ref.isdigit() else -1)
        ideas = _project_ideas(project, page)
        project.visible_ideas = ideas
        projects = [project]
    elif page.view == "projects":
        projects = _project_board(page.show_closed)
    else:
        ideas = _idea_board(page)
    data = {
        "ideas": [[idea.pk, idea.project_id, idea.status, idea.card_version] for idea in ideas],
        "projects": {str(project.pk): project.head_signature for project in projects},
    }
    body = json.dumps(data, separators=(",", ":"))
    etag = '"%s"' % hashlib.sha1(body.encode()).hexdigest()
    if etag in request.headers.get("If-None-Match", ""):
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(body, content_type="application/json")
    response["ETag"] = etag
    response["Cache-Control"] = "no-cache"
    return response


def _board_context(page, changes_query, insert):
    """The data-* values board.js reads off a page's board element: what to poll,
    where to fetch a card, and where a card new to the page goes (`insert`: "status"
    for the Ideas view's sections, "slot" below a project page's add slot, "" none)."""
    query = dict(changes_query)
    if page.view == "projects":
        query["view"] = "projects"
    if page.show_closed:
        query["closed"] = "1"
    return {
        "changes_url": reverse("core:changes") + ("?" + urlencode(query) if query else ""),
        "idea_card_url": _url_template("core:idea_card", page),
        "idea_edit_url": _url_template("core:idea_edit", page),
        "project_card_url": _url_template("core:project_card", page),
        "project_edit_url": _url_template("core:project_edit", page),
        "board_insert": insert,
    }


def home(request):
    """The Ideas view (#384): every open idea across projects, in status sections
    (Verify, Planned, New), newest first in each; ?closed=1 adds the closed ones.

    The filter box hides cards client-side; `matches` makes a JS-off ?q= render the
    same thing. Cards are lazy, as everywhere a list of them is shown (#269).
    """
    page = Page("ideas", Page.from_request(request).show_closed)
    query = request.GET.get("q", "").strip()
    statuses = _idea_board_statuses(page)
    ideas = _idea_board(page)
    query_lower = query.lower()
    for idea in ideas:
        idea.matches = not query or any(
            query_lower in text.lower() for text in (idea.title, idea.project.name, idea.project.folder)
        )
    sections = []
    for status in statuses:
        members = [idea for idea in ideas if idea.status == status]
        sections.append({
            "status": status.value,
            "label": status.label,
            "ideas": members,
            "matches": any(idea.matches for idea in members),
        })
    context = {
        "page_title": "Ideas",
        "sections": sections,
        "q": query,
        "any_ideas": bool(ideas),
        "any_match": any(idea.matches for idea in ideas),
        "lazy": True,
        **_board_context(page, {}, "status"),
        **page.context(),
    }
    return render(request, "core/home.html", context)


@require_http_methods(["GET"])
def idea_detail(request, pk):
    """One idea's own page (#384): the full card, expanded, and its activity."""
    idea = get_object_or_404(
        Idea.objects.select_related("project").prefetch_related("screenshots", "artifacts"), pk=pk
    )
    page = Page("idea", Page.from_request(request).show_closed)
    context = {
        "idea": idea,
        "project": idea.project,
        "page_title": f"#{idea.pk} {idea.title}",
        **_activity_context(request, idea=idea),
        **_board_context(page, {"idea": idea.pk}, ""),
        **page.context(),
    }
    return render(request, "core/idea_detail.html", context)


@require_http_methods(["GET"])
def projects(request):
    """The Projects view (#384): one compact row per project, grouped by whether it
    has open ideas, closed projects under ?closed=1. The filter box hides rows
    client-side; `matches` makes a JS-off ?q= render the same thing."""
    page = Page("projects", Page.from_request(request).show_closed)
    query = request.GET.get("q", "").strip()
    rows = _project_board(page.show_closed)
    query_lower = query.lower()
    for project in rows:
        project.matches = not query or query_lower in project.name.lower() or query_lower in project.folder.lower()
    groups = [
        ("With open ideas", [p for p in rows if p.has_open_idea and p.status not in CLOSED_PROJECT_STATUSES]),
        ("Without open ideas", [p for p in rows if not p.has_open_idea and p.status not in CLOSED_PROJECT_STATUSES]),
        ("Closed", [p for p in rows if p.status in CLOSED_PROJECT_STATUSES]),
    ]
    context = {
        "page_title": "Projects",
        "groups": [
            {"label": label, "projects": members, "matches": any(p.matches for p in members)}
            for label, members in groups
            if members
        ],
        "q": query,
        "any_projects": bool(rows),
        "any_match": any(p.matches for p in rows),
        **_board_context(page, {}, ""),
        **page.context(),
    }
    return render(request, "core/projects.html", context)


@require_http_methods(["GET"])
def project_detail(request, ref):
    """One project's own page (#384): its header, its ideas and its activity.

    Addressed by folder. A numeric ref that is no project's folder is taken as a pk and
    redirected to the folder URL, so /projects/<id>/ links keep working.
    """
    project = Project.objects.filter(folder=ref).first()
    if project is None:
        if ref.isdigit():
            project = get_object_or_404(Project, pk=int(ref))
            query = request.META.get("QUERY_STRING", "")
            url = reverse("core:project_detail", args=[project.folder])
            return redirect(url + ("?" + query if query else ""))
        raise Http404("no such project")
    page = Page("project", Page.from_request(request).show_closed)
    query = request.GET.get("q", "").strip()
    ideas = _project_ideas(project, page)
    for idea in ideas:
        idea.matches = _idea_matches(idea, query)
    project.visible_ideas = ideas
    context = {
        "project": project,
        "ideas": ideas,
        "q": query,
        "no_match": bool(ideas) and not any(idea.matches for idea in ideas),
        "page_title": project.name,
        "lazy": True,
        **_new_idea_template_context(page),
        **_activity_context(request, project=project),
        **_board_context(page, {"project": project.pk}, "slot"),
        **page.context(),
    }
    return render(request, "core/project_detail.html", context)


def _new_idea_template_context(page):
    """What _new_idea_template.html needs: the blank form, cloned client-side per project."""
    return {
        "new_idea_form": _new_idea_form(),
        "new_idea_form_action": _url_template("core:idea_new", page),
    }


@require_http_methods(["GET", "POST"])
def idea_new(request, pk):
    project = get_object_or_404(Project, pk=pk)
    page = Page.from_request(request)
    if request.method == "POST":
        form = _new_idea_form(request.POST)
        if form.is_valid():
            idea = form.save(commit=False)
            idea.project = project
            idea.save()
            if _is_htmx(request):
                # The whole list is re-rendered: the new idea has to land at its
                # sorted position, and the add slot has to come back.
                return _render_project_ideas(request, project, page)
            return page.redirect(project=project)
        context = {"project": project, "form": form, "back_url": page.url(project=project), **page.context()}
        if _is_htmx(request):
            response = render(request, "core/_idea_new_form.html", context, status=422)
            # The form posts at the project page's whole idea list, because a
            # successful save re-renders it. The error response is only the form, so
            # it has to be retargeted at the form's own slot or it would eat the list.
            response["HX-Retarget"] = f"#new-idea-{project.pk}"
            response["HX-Reswap"] = "outerHTML"
            return response
        return render(request, "core/idea_new.html", context, status=422)
    # A GET here is only ever the JS-off fallback page now -- the "Add idea" link
    # opens the form by cloning _new_idea_template.html client-side instead of
    # fetching it, so there is no htmx branch to serve any more.
    form = _new_idea_form()
    context = {"project": project, "form": form, "back_url": page.url(project=project), **page.context()}
    return render(request, "core/idea_new.html", context)


@require_POST
def idea_transition(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    page = Page.from_request(request)
    # A card rendered before needs_verification was switched off still offers verify.
    target = idea.resolve_target(request.POST.get("target"))
    if not idea.can_transition_to(target):
        return HttpResponseBadRequest("invalid transition")
    idea.status = target
    idea.save()
    if _is_htmx(request):
        return _render_idea_card(request, idea, page)
    return page.redirect(project=idea.project, idea=idea)


@require_http_methods(["GET", "POST"])
def idea_edit(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    page = Page.from_request(request)
    old_project = idea.project
    if request.method == "POST":
        form = IdeaEditForm(request.POST, instance=idea)
        if form.is_valid():
            form.save()
            if not _is_htmx(request):
                # A project page returns to the project the idea was listed under.
                return page.redirect(project=old_project, idea=idea)
            if idea.project_id != old_project.pk and page.view == "project":
                # Moved off this project's page: the empty body removes the row.
                return HttpResponse("")
            # Everywhere else the card stays where it is, its project chip updated.
            return _render_idea_card(request, idea, page)
        context = {"idea": idea, "form": form, "back_url": page.url(old_project, idea), **page.context()}
        if _is_htmx(request):
            return render(request, "core/_idea_form.html", context, status=422)
        return render(request, "core/idea_edit.html", context, status=422)
    form = IdeaEditForm(instance=idea)
    context = {"idea": idea, "form": form, "back_url": page.url(old_project, idea), **page.context()}
    if _is_htmx(request):
        return render(request, "core/_idea_form.html", context)
    return render(request, "core/idea_edit.html", context)


@require_http_methods(["GET"])
def idea_card(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    return _render_idea_card(request, idea, Page.from_request(request))


@require_http_methods(["GET"])
def idea_body(request, pk):
    """Everything below a card's header row, for the lazy stand-in in _idea.html."""
    idea = get_object_or_404(Idea.objects.prefetch_related("screenshots", "artifacts"), pk=pk)
    context = {"idea": idea, **Page.from_request(request).context()}
    # Planned and verify ideas lead with their plan or report; the rest get the default layout.
    leads = idea.status in (Idea.Status.PLANNED, Idea.Status.VERIFY)
    template = "core/_idea_main.html" if leads else "core/_idea_default_body.html"
    return render(request, template, context)


@require_http_methods(["GET"])
def idea_prompt(request, pk):
    """The prompt block, for the lazy stand-in inside a card's Prompt <details>."""
    idea = get_object_or_404(Idea, pk=pk)
    return render(request, "core/_idea_prompt.html", {"idea": idea})


@require_POST
def screenshot_delete(request, pk):
    screenshot = get_object_or_404(Screenshot, pk=pk)
    idea = screenshot.idea
    page = Page.from_request(request)
    screenshot.image.delete(save=False)
    screenshot.delete()
    if _is_htmx(request):
        return _render_idea_card(request, idea, page)
    return page.redirect(project=idea.project, idea=idea)


@require_POST
def artifact_upload(request, pk):
    idea = get_object_or_404(Idea, pk=pk)
    page = Page.from_request(request)
    upload = request.FILES.get("file")
    error = None
    if upload is None:
        error = "missing file"
    else:
        try:
            services.check_artifact_upload(upload.name, upload.size, services.ARTIFACT_MAX_BYTES)
            services.add_artifact(pk, upload.name, upload.read(), caption=request.POST.get("caption", ""))
        except services.Invalid as exc:
            error = str(exc)
    if error:
        if _is_htmx(request):
            idea.refresh_from_db()
            response = _render_idea_card(request, idea, page, artifact_error=error)
            response.status_code = 422
            return response
        return page.redirect(project=idea.project, idea=idea)
    if _is_htmx(request):
        idea.refresh_from_db()
        return _render_idea_card(request, idea, page)
    return page.redirect(project=idea.project, idea=idea)


@require_POST
def artifact_delete(request, pk):
    artifact = get_object_or_404(Artifact, pk=pk)
    idea = artifact.idea
    page = Page.from_request(request)
    artifact.file.delete(save=False)
    artifact.delete()
    if _is_htmx(request):
        return _render_idea_card(request, idea, page)
    return page.redirect(project=idea.project, idea=idea)


def artifact_download(request, pk):
    artifact = get_object_or_404(Artifact, pk=pk)
    response = FileResponse(
        artifact.file.open("rb"), content_type=artifact.content_type or "application/octet-stream"
    )
    sandboxed = os.path.splitext(artifact.filename)[1].lower() in ARTIFACT_SANDBOXED_EXTENSIONS
    if sandboxed:
        # Inline, so a report opens straight from the card; safe only because
        # harden_media_response puts it in a sandbox.
        response["Content-Type"] = "text/html; charset=utf-8"
    elif not artifact.is_text:
        response["Content-Disposition"] = f'attachment; filename="{artifact.filename}"'
    return harden_media_response(response, artifact.filename)


def media(request, path):
    """/media/ -- django.views.static.serve plus the upload hardening headers."""
    return harden_media_response(
        serve_static(request, path, document_root=settings.MEDIA_ROOT), path
    )


@require_POST
def project_transition(request, pk):
    project = get_object_or_404(Project, pk=pk)
    page = Page.from_request(request)
    target = request.POST.get("target")
    if not project.can_transition_to(target):
        return HttpResponseBadRequest("invalid transition")
    project.status = target
    project.save()
    if _is_htmx(request):
        return _render_project_card(request, project, page)
    return page.redirect(project=project)


@require_http_methods(["GET", "POST"])
def project_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    page = Page.from_request(request)
    if request.method == "POST":
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            if _is_htmx(request):
                return _render_project_card(request, project, page)
            return page.redirect(project=project)
        context = {"project": project, "form": form, "back_url": page.url(project=project), **page.context()}
        if _is_htmx(request):
            return render(request, "core/_project_form.html", context, status=422)
        return render(request, "core/project_edit.html", context, status=422)
    form = ProjectForm(instance=project)
    context = {"project": project, "form": form, "back_url": page.url(project=project), **page.context()}
    if _is_htmx(request):
        return render(request, "core/_project_form.html", context)
    return render(request, "core/project_edit.html", context)


@require_http_methods(["GET"])
def project_card(request, pk):
    project = get_object_or_404(Project, pk=pk)
    return _render_project_card(request, project, Page.from_request(request))


_ACTIVITY_WHO = {"agents": Event.AGENT_SOURCES, "me": Event.USER_SOURCES}


def _activity_context(request, project=None, idea=None):
    """The feed partial's context (_activity_list.html), for /activity/ and the pages
    that embed it filtered to one project or one idea (#384).

    `?who=agents|me` is read from the request; the project or idea filter is passed in,
    already resolved, so a detail page and `?project=` / `?idea=` go through one path.
    """
    who = request.GET.get("who", "")
    if who not in _ACTIVITY_WHO:
        who = ""
    events = Event.objects.filter(
        created_at__gte=timezone.now() - timedelta(days=activity.FEED_DAYS)
    )
    if who:
        events = events.filter(source__in=_ACTIVITY_WHO[who])
    if idea is not None:
        events = events.filter(idea_id=idea.pk)
    elif project is not None:
        events = events.filter(project_id=project.pk)
    groups = activity.group_events(list(events[: activity.FEED_LIMIT]))

    # Current titles, so a rename shows; a deleted idea keeps its snapshot, unlinked.
    idea_ids = {g.idea_id for g in groups if g.idea_id}
    titles = dict(Idea._base_manager.filter(pk__in=idea_ids).values_list("pk", "title"))
    zone = activity.display_zone()
    today = timezone.now().astimezone(zone).date()
    days = []
    for group in groups:
        if group.idea_id:
            group.exists = group.idea_id in titles
            group.title = titles.get(group.idea_id, group.title)
        day = group.end.astimezone(zone).date()
        if not days or days[-1]["date"] != day:
            if day == today:
                label = "Today"
            elif day == today - timedelta(days=1):
                label = "Yesterday"
            else:
                label = day.strftime("%a %d %b %Y")
            days.append({"date": day, "label": label, "groups": []})
        days[-1]["groups"].append(group)

    base = reverse("core:activity")

    def url(**params):
        params = {k: v for k, v in params.items() if v}
        return base + ("?" + urlencode(params) if params else "")

    project_folder = project.folder if project else ""
    idea_id = idea.pk if idea else ""
    return {
        "days": days,
        "display_time_zone": zone,
        "refresh_url": url(who=who, project=project_folder, idea=idea_id),
        "who_links": [
            {"label": label, "url": url(who=value, project=project_folder, idea=idea_id), "active": who == value}
            for value, label in (("", "All"), ("agents", "Agents"), ("me", "Me"))
        ],
        "clear_project_url": url(who=who),
        # Completed with a project id in the template, per entry.
        "project_url_prefix": url(who=who) + ("&" if who else "?") + "project=",
    }


@require_http_methods(["GET"])
def activity_feed(request):
    """Recent changes, grouped per idea and source (#361).

    `?who=agents|me`, `?project=<id or folder>` and `?idea=<id>` filter it. The list is
    one partial, `_activity_list.html`, which the project and idea pages embed filtered.
    """
    project = idea = None
    unknown = False
    project_ref = request.GET.get("project", "")
    idea_ref = request.GET.get("idea", "")
    if idea_ref:
        idea = Idea.objects.filter(pk=idea_ref).first() if idea_ref.isdigit() else None
        unknown = idea is None
    elif project_ref:
        try:
            project = services._resolve_project(project_ref)
        except services.NotFound:
            unknown = True
    context = _activity_context(request, project=project, idea=idea)
    if unknown:
        # Nothing matches an unknown filter; the page says so instead of showing all,
        # and keeps saying so on its 30 s refresh.
        context["days"] = []
        context["refresh_url"] = request.get_full_path()
    context.update({"project": project, "idea": idea, "filter_unknown": unknown})
    if _is_htmx(request):
        return render(request, "core/_activity_list.html", context)
    return render(request, "core/activity.html", context)
