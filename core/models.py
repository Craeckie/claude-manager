import hashlib
import re

import markdown
import nh3
from django.conf import settings
from django.db import models
from django.db.models import Exists, Max, OuterRef
from django.db.models.functions import Coalesce, Greatest
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.text import get_valid_filename

from .transitions import IDEA_TRANSITIONS, IDEA_TRANSITIONS_NO_VERIFY, PROJECT_TRANSITIONS

# Appended to a prompt whose idea carries a plan, so the section reads as an instruction
# rather than as background the model is free to re-derive.
PLAN_INSTRUCTION = "Follow this plan; deviate only if it turns out to be wrong, and say so."

# Appended to a prompt whose idea carries review findings, so the next agent working
# the idea treats them as something to act on rather than as passive history.
REVIEW_INSTRUCTION = "Address these review findings; say what changed in the report."

# CSS cannot ask how tall the rendered plan (or report) turns out to be, so "long" is
# decided here, on the raw Markdown, and used to gate the clamp-and-expand control on a
# planned idea's plan block or a verify idea's report block. Either threshold tripping
# counts: a block can be long by line count (many short steps) or by character count (a
# few dense paragraphs) without tripping the other.
PLAN_LONG_LINE_THRESHOLD = 18
PLAN_LONG_CHAR_THRESHOLD = 1200


# HTML5's "raw text" elements: once a real HTML parser (a browser, or nh3's own) sees
# an opening tag with one of these names, everything up to a matching closing tag --
# or, lacking one, the rest of the document -- becomes that element's literal text,
# not markup. That is what let one unclosed "<script>" mentioned in an idea's report
# swallow every sibling project on the overview page into invisible script content
# (idea #66). Sanitizing markdown.markdown()'s *output* can't undo this: by the time
# nh3 parses that string, the swallowing has already happened. So these names are
# neutralized in the raw Markdown *before* it is converted, while everything else
# (comparisons, generics, a bare "<" in prose) is left alone.
_RAW_TEXT_TAG_RE = re.compile(
    r"<(/?)(script|style|textarea|title|iframe|noembed|noframes|xmp|plaintext)\b",
    re.IGNORECASE,
)

# Tags/attributes markdown.markdown() with the fenced_code/tables extensions actually
# produces, plus the small set for links and images -- nothing raw HTML could add.
_ALLOWED_MARKDOWN_TAGS = {
    "p", "br", "hr", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "img", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_MARKDOWN_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title"}}


_LIST_ITEM_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+")

# A line (or list item) opening with an idea reference like "#177". Python-Markdown
# needs no space after an ATX "#", so it would render that as an <h1>; a real heading
# always has a space, so "#" directly followed by a digit is never one.
_LEADING_IDEA_REF_RE = re.compile(r"^(\s*(?:(?:[-*+]|\d+\.)\s+)?)#(?=\d)")


def _loosen_markdown(text):
    """Insert a blank line before a list block that immediately follows a non-blank,
    non-list line, so Python-Markdown (which requires that blank line) actually
    renders it as a list instead of leaving it as literal text inside a <p>.

    Only touches the boundary before a list's first item -- consecutive items, an
    already-blank line above, and anything inside a fenced code block are untouched.
    It also escapes a leading "#<digit>" (see `_LEADING_IDEA_REF_RE`).
    """
    lines = text.split("\n")
    result = []
    in_code_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            result.append(line)
            continue
        if not in_code_fence and _LIST_ITEM_RE.match(line):
            prev = result[-1] if result else None
            if prev is not None and prev.strip() != "" and not _LIST_ITEM_RE.match(prev):
                result.append("")
        if not in_code_fence:
            line = _LEADING_IDEA_REF_RE.sub(r"\1\\#", line)
        result.append(line)
    return "\n".join(result)


_IDEA_EDIT_HREF_RE = re.compile(r'href="/ideas/(\d+)/edit/"')


def _render_markdown(text, loose=False):
    """Render Markdown to HTML for mark_safe, with any raw HTML in the source
    neutralized rather than passed through.

    A sanitizing pass alone is not enough (see the comment on `_RAW_TEXT_TAG_RE`), so
    this neutralizes the tags that can swallow content before conversion, then runs
    the result through nh3 for everything else raw HTML could still do -- an
    `onerror` handler, a `javascript:` link, an arbitrary tag. Markdown's own output
    (code spans, fenced code, tables, ...) is unaffected either way.

    `loose=True` is for Claude-written prose rather than carefully hand-written
    Markdown: it runs `_loosen_markdown()` first and adds the `nl2br` extension, so a
    single newline becomes a <br> and a list right after a paragraph line still renders
    as a list, instead of both collapsing into one literal-text paragraph. Every field
    on the card uses it -- plans are written by Claude via the MCP just like reports
    (idea #199), and the prompt embeds all the fields.
    """
    if loose:
        text = _loosen_markdown(text)
    unescaped = _RAW_TEXT_TAG_RE.sub(r"&lt;\1\2", text)
    extensions = ["fenced_code", "tables"]
    if loose:
        extensions.append("nl2br")
    rendered = markdown.markdown(unescaped, extensions=extensions)
    cleaned = nh3.clean(rendered, tags=_ALLOWED_MARKDOWN_TAGS, attributes=_ALLOWED_MARKDOWN_ATTRS)
    # Stored records link other ideas as /ideas/<id>/edit/ (#586): send them to the view.
    return mark_safe(_IDEA_EDIT_HREF_RE.sub(r'href="/ideas/\1/"', cleaned))


def _is_long(text):
    if not text.strip():
        return False
    return (
        text.count("\n") + 1 > PLAN_LONG_LINE_THRESHOLD or len(text) > PLAN_LONG_CHAR_THRESHOLD
    )


# Screenshots are validated in the service layer (extension + size), not by an
# ImageField -- that would pull in Pillow just to decode-check an upload nobody asked
# to manipulate. The set here and the cap in services.py are the actual gate.
SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def screenshot_upload_path(instance, filename):
    # Sanitized so a caller-supplied filename cannot steer the path (no "../", no
    # absolute path); the directory groups an idea's screenshots for easy cleanup.
    return f"screenshots/idea-{instance.idea_id}/{get_valid_filename(filename)}"


# Any file an idea's work produces or needs: a log excerpt, a spec, an APK. Unlike
# Screenshot this is not image-only and is attachable in every idea status, including
# "new" -- validated in the service layer (extension allowlist + size caps), same
# reasoning as Screenshot.
ARTIFACT_EXTENSIONS = {
    ".log", ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".diff", ".patch",
    ".apk", ".aab", ".zip", ".tar", ".gz", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".html", ".htm",
}

# Media is served from claude-manager's own origin, so an uploaded page with a script
# would otherwise run as claude-manager (stored XSS against its API and forms). These
# extensions are only served with SANDBOXED_HTML_CSP (see core/security.py): an opaque
# origin -- no cookies, no same-origin requests -- and no external loads at all, while
# inline scripts/styles and data: images (shopping's report.py) keep working.
ARTIFACT_SANDBOXED_EXTENSIONS = {".html", ".htm"}
SANDBOXED_HTML_CSP = (
    "sandbox allow-scripts; default-src 'none'; img-src data:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
)

# Extensions whose content is safe to read back as text (for the MCP read_artifact
# tool); everything else is treated as binary and only its metadata/URL is returned.
ARTIFACT_TEXT_EXTENSIONS = {
    ".log", ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".diff", ".patch",
}


def artifact_upload_path(instance, filename):
    # Same reasoning as screenshot_upload_path: sanitized so a caller-supplied
    # filename cannot steer the path.
    return f"artifacts/idea-{instance.idea_id}/{get_valid_filename(filename)}"


def _signature(*parts):
    """Short, stable hash of a card's displayed state, compared by the board's poll."""
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:12]


class ProjectManager(models.Manager):
    def get_queryset(self):
        # "Recent activity" is the project's own edit or any of its ideas being
        # created, edited or transitioned -- whichever happened last. Coalesce is
        # load-bearing: a project with no ideas has a NULL Max(), and SQLite's
        # GREATEST returns NULL if any argument is NULL, which would sort every
        # empty project into a NULL bucket instead of by its own updated_at.
        #
        # Projects with an open idea sort ahead of every project without one;
        # within each of those two groups, ordering stays by recency as before.
        return (
            super()
            .get_queryset()
            .annotate(
                last_activity=Greatest(
                    "updated_at", Coalesce(Max("ideas__updated_at"), "updated_at")
                ),
                has_open_idea=Exists(
                    Idea.objects.filter(project=OuterRef("pk"), status__in=OPEN_IDEA_STATUSES)
                ),
            )
            .order_by("-has_open_idea", "-last_activity", "-pk")
        )


class IdeaManager(models.Manager):
    def get_queryset(self):
        # Recency alone, with no status grouping: an idea that was just written
        # or just moved is the one being worked on, whatever its status.
        return super().get_queryset().order_by("-updated_at", "-pk")


class Project(models.Model):
    class Status(models.TextChoices):
        IDEA = "idea", "Idea"
        DEFINED = "defined", "Defined"
        IMPLEMENTED = "implemented", "Implemented"
        DISCONTINUED = "discontinued", "Discontinued"
        CANCELED = "canceled", "Canceled"

    class GitStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        CLEAN = "clean", "Clean"
        DIRTY = "dirty", "Uncommitted"
        AHEAD = "ahead", "Unpushed"
        BEHIND = "behind", "Behind"
        DIVERGED = "diverged", "Diverged"
        NO_REMOTE = "no_remote", "No remote"
        NOT_A_REPO = "not_a_repo", "Not a repo"
        ERROR = "error", "Error"

    # Statuses that need no attention from the user, so the card stays quiet instead
    # of a chip on every project (most are either clean or not a real checkout).
    QUIET_GIT_STATUSES = {GitStatus.NOT_CHECKED, GitStatus.CLEAN, GitStatus.NOT_A_REPO}

    name = models.CharField(max_length=200, unique=True)
    folder = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IDEA)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Populated only by the `sync_git_status` management command, run by hand -- see
    # its docstring. Not touched by any request path, so a stale value just means the
    # command hasn't been run since, not that anything is broken.
    git_status = models.CharField(
        max_length=20, choices=GitStatus.choices, default=GitStatus.NOT_CHECKED, blank=True
    )
    git_detail = models.CharField(max_length=200, blank=True)
    git_checked_at = models.DateTimeField(null=True, blank=True)

    objects = ProjectManager()

    def __str__(self):
        return self.name

    @property
    def root_path(self):
        # Every project is a folder under the workspace root; an idea prompt is
        # executed from the project root, so the prompt has to name it.
        return f"{settings.WORKSPACE_ROOT}/{self.folder}"

    @property
    def is_implemented(self):
        return self.status == self.Status.IMPLEMENTED

    @property
    def git_status_needs_attention(self):
        """Whether the git status chip is worth showing on the card.

        Clean, never-checked and not-a-repo carry nothing to act on, so the card
        stays quiet for them; only a status that calls for some git action shows.
        """
        return self.git_status not in self.QUIET_GIT_STATUSES

    @property
    def open_idea_counts(self):
        """Open ideas per status, for a project row or header: [(label, n), ...].

        Ordered verify → planned → new -- the ones waiting on the user first -- and
        only statuses that have any. Reads the view's prefetched `visible_ideas`
        when it is there, so the Projects view stays free of a query per row.
        """
        ideas = getattr(self, "visible_ideas", None)
        if ideas is None:
            ideas = self.ideas.filter(status__in=OPEN_IDEA_STATUSES)
        counts = {}
        for idea in ideas:
            counts[idea.status] = counts.get(idea.status, 0) + 1
        order = [Idea.Status.VERIFY, Idea.Status.PLANNED, Idea.Status.NEW]
        return [(status.label.lower(), counts[status]) for status in order if counts.get(status)]

    def get_commit_url(self, commit_hash):
        """A web link to a commit: on GitHub if origin is there, else under
        settings.GIT_WEB_BASE, or None when that is unset."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=self.root_path,
                capture_output=True,
                text=True,
                timeout=2,
            )
            url = result.stdout.strip()
            if url:
                if "github.com" in url:
                    repo = url.replace("https://github.com/", "").replace("git@github.com:", "").replace(".git", "")
                    return f"https://github.com/{repo}/commit/{commit_hash}"
        except Exception:
            pass
        if settings.GIT_WEB_BASE:
            return f"{settings.GIT_WEB_BASE}/{self.folder}/commit/{commit_hash}"
        return None

    @property
    def head_signature(self):
        """Fingerprint of what a project's row or page header shows.

        static/js/board.js polls it (`core:changes`) to refresh a row or header in
        place when an agent edits the project or moves one of its ideas (#322,
        #384); the idea cards have their own per-idea versions.
        """
        return _signature(
            self.name, self.folder, self.description, self.status, self.git_status,
            self.git_detail, self.git_checked_at, self.open_idea_counts,
        )

    def allowed_transitions(self):
        return PROJECT_TRANSITIONS.get(self.status, [])

    def can_transition_to(self, target):
        return any(t.target == target for t in self.allowed_transitions())


class Idea(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        PLANNED = "planned", "Planned"
        VERIFY = "verify", "Verify"
        IMPLEMENTED = "implemented", "Implemented"
        ABANDONED = "abandoned", "Abandoned"

    class Kind(models.TextChoices):
        FIX = "fix", "Fix"
        FEATURE = "feature", "Feature"
        DESIGN = "design", "Design"
        QUESTION = "question", "Question"
        OTHER = "other", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ideas")
    title = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
        default=Kind.OTHER,
        blank=True,
        help_text="What kind of change this idea is.",
    )
    goal = models.TextField(blank=True, help_text="What outcome this idea is trying to achieve.")
    context = models.TextField(blank=True, help_text="Background needed to understand the idea.")
    notes = models.TextField(blank=True, help_text="Any other notes.")
    plan = models.TextField(
        blank=True,
        help_text="Step-by-step implementation plan, written before the work starts.",
    )
    report = models.TextField(
        blank=True,
        help_text=(
            "Only possible problems and relevant decisions made, as a list of short "
            "sentences with the details below. If there is nothing relevant, just the "
            "sentence \"Implemented as planned. Nothing to report.\""
        ),
    )
    review = models.TextField(
        blank=True,
        help_text="Findings from a code review of this idea's implementation, for the idea's next agent to address.",
    )
    commits = models.JSONField(
        default=list,
        blank=True,
        help_text="List of commit hashes that implemented this idea.",
    )
    needs_verification = models.BooleanField(
        default=False,
        help_text=(
            "Whether the finished idea waits in verify for the user to check it. If "
            "false, finishing it (transition_idea to verify) closes it as implemented."
        ),
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = IdeaManager()

    PROMPT_SECTIONS = (
        ("Type", "kind_prompt_value"),
        ("Goal", "goal"),
        ("Context", "context"),
        ("Notes", "notes"),
        ("Implementation plan", "plan"),
        ("Review findings", "review"),
    )

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # `kind` is blank=True so that an omitted type is legal, not so that a blank
        # one can be stored: "other" is what "no type" means, and the API advertises
        # a closed five-value enum. Normalizing here covers the admin and any direct
        # ORM write as well as the forms.
        if not self.kind:
            self.kind = self.Kind.OTHER
        return super().save(*args, **kwargs)

    @property
    def card_version(self):
        """Fingerprint of everything the idea's card shows, for the board's poll (#322).

        Attaching or deleting a screenshot or artifact leaves updated_at alone, so
        their ids are part of it. Reads the prefetched relations where there are some.
        """
        return _signature(
            self.updated_at.isoformat(),
            [s.pk for s in self.screenshots.all()],
            [a.pk for a in self.artifacts.all()],
        )

    @property
    def detail_url(self):
        return idea_detail_url(self.pk)

    def allowed_transitions(self):
        table = IDEA_TRANSITIONS if self.needs_verification else IDEA_TRANSITIONS_NO_VERIFY
        return table.get(self.status, [])

    def resolve_target(self, target):
        """The status a requested transition lands on.

        Agents are told to finish an idea with target "verify"; on a planned idea that
        needs no verification, that means "implemented".
        """
        finishing = target == self.Status.VERIFY and self.status == self.Status.PLANNED
        if finishing and not self.needs_verification:
            return self.Status.IMPLEMENTED
        return target

    def can_transition_to(self, target):
        return any(t.target == target for t in self.allowed_transitions())

    @property
    def is_closed(self):
        return self.status in CLOSED_IDEA_STATUSES

    @property
    def kind_prompt_value(self):
        # "other" is the neutral default -- it produces no "### Type" section, so an
        # idea nobody has categorized still generates exactly the prompt it always did.
        if self.kind == self.Kind.OTHER:
            return ""
        return self.get_kind_display()

    def build_prompt(self):
        sections = [
            f"### Project\n"
            # One line: the card renders the prompt with nl2br, so a hard wrap here
            # would show up as ragged line breaks (idea #199).
            f"`cd {self.project.root_path}` first and carry out everything below in that "
            f"project, using it as the working directory for all paths and commands. "
            f"Before planning or implementing anything below, read `{settings.WORKSPACE_ROOT}/CLAUDE.md` "
            f"and `{self.project.root_path}/CLAUDE.md` (its authority for this project) if "
            f"you have not already read them this session.",
            f"### Title\n{self.title}",
        ]
        for heading, field in self.PROMPT_SECTIONS:
            value = getattr(self, field).strip()
            if value:
                sections.append(f"### {heading}\n{value}")
        if self.plan.strip():
            sections.append(PLAN_INSTRUCTION)
        if self.review.strip():
            sections.append(REVIEW_INSTRUCTION)
        return "\n\n".join(sections)

    @cached_property
    def plan_html(self):
        """The plan rendered for reading on the card; the raw Markdown stays editable."""
        if not self.plan.strip():
            return ""
        return _render_markdown(self.plan, loose=True)

    @cached_property
    def prompt_html(self):
        """The generated prompt rendered for reading on the card; the raw text stays
        copyable via the <pre>.
        """
        return _render_markdown(self.build_prompt(), loose=True)

    @cached_property
    def report_html(self):
        """The report rendered for reading on the card; the raw Markdown stays editable."""
        if not self.report.strip():
            return ""
        return _render_markdown(self.report, loose=True)

    @cached_property
    def review_html(self):
        """The review findings rendered for reading on the card; the raw Markdown stays editable."""
        if not self.review.strip():
            return ""
        return _render_markdown(self.review, loose=True)

    @property
    def plan_is_long(self):
        """Whether the raw plan is long enough to warrant a clamp-and-expand control.

        Judged on the raw Markdown, not the rendered HTML, since the threshold is meant
        to track how much the author wrote rather than how markdown.markdown() happens
        to wrap it.
        """
        return _is_long(self.plan)

    @property
    def report_is_long(self):
        """Whether the raw report is long enough to warrant a clamp-and-expand control.

        Same reasoning as plan_is_long, applied to the report instead.
        """
        return _is_long(self.report)

    @property
    def review_is_long(self):
        """Whether the raw review is long enough to warrant a clamp-and-expand control.

        Same reasoning as plan_is_long, applied to the review instead.
        """
        return _is_long(self.review)

    @property
    def commit_urls(self):
        """Return a list of commit hashes with their URLs."""
        return [
            {
                "hash": commit,
                "url": self.project.get_commit_url(commit)
            }
            for commit in self.commits
        ]


class Screenshot(models.Model):
    idea = models.ForeignKey(Idea, on_delete=models.CASCADE, related_name="screenshots")
    image = models.FileField(upload_to=screenshot_upload_path)
    caption = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")

    def __str__(self):
        return self.image.name


class Artifact(models.Model):
    idea = models.ForeignKey(Idea, on_delete=models.CASCADE, related_name="artifacts")
    file = models.FileField(upload_to=artifact_upload_path)
    caption = models.CharField(max_length=200, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")

    def __str__(self):
        return self.file.name

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1]

    @property
    def is_text(self):
        name = self.filename.lower()
        return any(name.endswith(ext) for ext in ARTIFACT_TEXT_EXTENSIONS)


def idea_detail_url(pk):
    """Where a link to one idea points: its own page (#384). `/ideas/<id>/edit/` keeps
    working too, because stored reports link to it."""
    return reverse("core:idea_detail", args=[pk])


class Event(models.Model):
    """One change to a project or idea, for the activity feed (#361).

    Written by the signal handlers in `core/activity.py`, never by hand. Ids and names
    are plain values, not foreign keys: the log outlives deletes and renames, and a
    file's post_delete fires inside its idea's own cascade delete, where an FK to the
    idea would point at a row that is already gone.
    """

    class Source(models.TextChoices):
        WEB = "web", "You"
        ADMIN = "admin", "You · Admin"
        API = "api", "Agent · API"
        MCP = "mcp", "Agent · MCP"

    class Action(models.TextChoices):
        IDEA_CREATED = "idea_created", "Idea created"
        IDEA_CHANGED = "idea_changed", "Idea changed"
        IDEA_DELETED = "idea_deleted", "Idea deleted"
        PROJECT_CREATED = "project_created", "Project created"
        PROJECT_CHANGED = "project_changed", "Project changed"
        SCREENSHOT_ADDED = "screenshot_added", "Screenshot added"
        SCREENSHOT_DELETED = "screenshot_deleted", "Screenshot deleted"
        ARTIFACT_ADDED = "artifact_added", "Artifact added"
        ARTIFACT_DELETED = "artifact_deleted", "Artifact deleted"

    AGENT_SOURCES = (Source.API, Source.MCP)
    USER_SOURCES = (Source.WEB, Source.ADMIN)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    action = models.CharField(max_length=30, choices=Action.choices)
    project_id = models.IntegerField(null=True, blank=True, db_index=True)
    project_name = models.CharField(max_length=200, blank=True)
    idea_id = models.IntegerField(null=True, blank=True, db_index=True)
    idea_title = models.CharField(max_length=200, blank=True)
    # Names of the changed fields, status excluded (it has its own pair below).
    fields = models.JSONField(default=list, blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    # A file's name, or "Old → New" on a move.
    detail = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def __str__(self):
        return f"{self.get_action_display()} ({self.get_source_display()})"

    @property
    def is_agent(self):
        return self.source in self.AGENT_SOURCES


OPEN_IDEA_STATUSES = [Idea.Status.NEW, Idea.Status.PLANNED, Idea.Status.VERIFY]
CLOSED_IDEA_STATUSES = [Idea.Status.IMPLEMENTED, Idea.Status.ABANDONED]

# Unlike an idea, IMPLEMENTED is a normal end state for a project (most projects are
# implemented) and stays visible; only these two are the closed-like statuses.
CLOSED_PROJECT_STATUSES = [Project.Status.DISCONTINUED, Project.Status.CANCELED]
