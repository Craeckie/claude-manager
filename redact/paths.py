"""Where redacted output goes: inside a case's logs/ directory under LOG_ANALYSIS_ROOT.

LOG_ANALYSIS_ROOT is intentionally a different tree from REDACT_STATE_DIR
(see state.py) — the salt and hosts list must never land where a case's
logs/ (or anything else world-handed-to-Claude) could read them.

Since idea #319 a case is normally a *project folder* (`router`, `MusicBox`):
every log about one project lands in one folder under one salt, so its logs
correlate. Any other name works too, and dated cases
(`2025-01-15-router`, `2025-01-15-0853`) are still valid names. Because the
folder no longer says *when*, each uploaded file carries its own timestamp.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.utils.text import get_valid_filename

# A case name: a project folder (capitals allowed -- `HomeLab`, `MyApp`),
# a free name, or an old dated case. Anchored full-match, and the character class
# admits neither "/" nor ".", so this still forecloses traversal (and hidden dirs:
# the first character must be a letter or digit).
CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_NAME = 64

# Budget for a keyword appended to a timestamp: "YYYY-MM-DD-HHMM-" is 16.
MAX_KEYWORD = MAX_NAME - len("YYYY-MM-DD-HHMM-")

# Top-level folders of LOG_ANALYSIS_ROOT that are not cases. Any folder holding a
# CLAUDE.md is shared tool code, not a case, and is refused too.
RESERVED_CASE_NAMES = frozenset({"uploaded-logs"})

# The timestamp an uploaded file's name starts with: server clock, to the second.
FILE_STAMP_FORMAT = "%Y-%m-%d-%H%M%S"
FILE_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-")

CASE_GITIGNORE = ".ts-redact-salt\n*.redaction-stats\n"


class NotConfigured(Exception):
    """LOG_ANALYSIS_ROOT is unset or missing. The view must render the
    'not configured' panel, never a 500."""


class ReservedCaseName(ValueError):
    """The name belongs to a LOG_ANALYSIS_ROOT folder that is not a case."""


def log_analysis_root():
    configured = getattr(settings, "LOG_ANALYSIS_ROOT", None)
    if not configured:
        raise NotConfigured("LOG_ANALYSIS_ROOT is not configured")
    root = configured if isinstance(configured, Path) else Path(configured)
    if not root.is_dir():
        raise NotConfigured(f"LOG_ANALYSIS_ROOT does not exist: {root}")
    return root


def is_valid_case_name(name: str) -> bool:
    return bool(CASE_NAME_RE.match(name or ""))


def _existing_dirs() -> list[str]:
    try:
        root = log_analysis_root()
    except NotConfigured:
        return []
    return [p.name for p in root.iterdir() if p.is_dir() and is_valid_case_name(p.name)]


def is_reserved(name: str) -> bool:
    """A top-level LOG_ANALYSIS_ROOT folder that is not a case: `uploaded-logs`, or a
    shared tool (a folder with a CLAUDE.md). Compared case-insensitively, so
    `Uploaded-Logs` cannot sneak past on a case-sensitive filesystem."""
    lowered = (name or "").lower()
    if lowered in RESERVED_CASE_NAMES:
        return True
    try:
        root = log_analysis_root()
    except NotConfigured:
        return False
    return any(
        d.lower() == lowered and (root / d / "CLAUDE.md").exists() for d in _existing_dirs()
    )


def slugify_keyword(text: str, max_len: int = MAX_KEYWORD) -> str:
    """Reduce free text to something that can sit in a case-folder name.

    Anything outside [a-z0-9] becomes a hyphen, so "desk1 DNS!" -> "desk1-dns" and
    "../etc" -> "etc": a keyword can never climb out of LOG_ANALYSIS_ROOT, it can
    only turn into a duller keyword. An empty result means "no keyword", not an
    error -- the caller falls back to a bare timestamp.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-")


def generate_case_name(keyword: str = "", now=None) -> str:
    """<YYYY-MM-DD-HHMM>, plus -<keyword> when one survives slugification.

    `now` is injectable so tests do not race the clock.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d-%H%M")
    slug = slugify_keyword(keyword)
    return f"{stamp}-{slug}" if slug else stamp


def project_folders() -> list[str]:
    """Every claude-manager project's folder that can be a case name, sorted
    case-insensitively. A project *is* a case, whether or not it has logs yet."""
    from core.models import Project  # redact has no models of its own

    folders = Project.objects.values_list("folder", flat=True)
    return sorted((f for f in folders if is_valid_case_name(f)), key=str.lower)


def canonical_case_name(name: str) -> str:
    """The spelling a valid case name is stored under: a project's folder wins
    (`musicbox` -> `MusicBox`), then an existing case folder, else the name as typed.
    Case-insensitive on purpose, so one project never ends up split across two
    folders -- and two salts -- by a difference in capitals."""
    lowered = name.lower()
    for folder in project_folders():
        if folder.lower() == lowered:
            return folder
    for d in sorted(_existing_dirs()):
        if d.lower() == lowered:
            return d
    return name


def resolve_case_name(raw: str, now=None) -> str:
    """Turn whatever the field held into a case name. The single entry point for
    both upload paths, so the browser and the JS-off form cannot disagree.

    A valid name is used as typed, apart from matching a project or an existing
    case regardless of capitals: that is how a log lands in its project's case,
    how a free name starts a case of that name, and how the second file of a
    batch lands in the same folder as the first. Anything else is slugified;
    blank (or nothing left after slugifying) gets a bare `YYYY-MM-DD-HHMM`.

    Raises ReservedCaseName for a LOG_ANALYSIS_ROOT folder that is not a case.
    """
    candidate = (raw or "").strip()
    if not is_valid_case_name(candidate):
        candidate = slugify_keyword(candidate, max_len=MAX_NAME)
    if not candidate:
        return generate_case_name("", now=now)
    name = canonical_case_name(candidate)
    if is_reserved(name):
        raise ReservedCaseName(f"{name!r} is a reserved folder, not a case")
    return name


def list_cases() -> list[str]:
    """Every existing case folder: valid name, not reserved (the same rule as
    is_reserved, in one pass over the root)."""
    try:
        root = log_analysis_root()
    except NotConfigured:
        return []
    return sorted(
        d for d in _existing_dirs()
        if d.lower() not in RESERVED_CASE_NAMES and not (root / d / "CLAUDE.md").exists()
    )


def file_time(path: Path) -> datetime:
    """When a log arrived: the stamp its name starts with, or (an older,
    unstamped file) its modification time."""
    m = FILE_STAMP_RE.match(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), FILE_STAMP_FORMAT)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def case_logs(case_name: str) -> list[dict]:
    """The logs in a case, newest first, each with the time it arrived. Empty
    for a name that is not an existing case."""
    if not is_valid_case_name(case_name):
        return []
    try:
        logs = log_analysis_root() / case_name / "logs"
    except NotConfigured:
        return []
    if not logs.is_dir():
        return []
    entries = [
        {"name": p.name, "time": file_time(p)}
        for p in logs.iterdir()
        if p.is_file() and not p.name.endswith(".redaction-stats")
    ]
    return sorted(entries, key=lambda e: (e["time"], e["name"]), reverse=True)


def picker_cases() -> tuple[list[dict], list[dict]]:
    """The picker's two groups: every project (with its case folder's log count
    and last arrival, when it has one), then every other existing case, most
    recently used first."""
    existing = set(list_cases())

    def entry(name):
        logs = case_logs(name) if name in existing else []
        return {
            "name": name,
            "count": len(logs),
            "last": logs[0]["time"] if logs else None,
        }

    folders = project_folders()
    projects = [entry(f) for f in folders]
    taken = {f.lower() for f in folders}
    others = [entry(d) for d in existing if d.lower() not in taken]
    others.sort(key=lambda e: (e["last"] or datetime.min, e["name"]), reverse=True)
    return projects, others


def case_dir(case_name: str, create: bool = False):
    if not is_valid_case_name(case_name):
        raise SuspiciousFileOperation(f"invalid case name: {case_name!r}")
    if is_reserved(case_name):
        raise SuspiciousFileOperation(f"reserved, not a case: {case_name!r}")
    root = log_analysis_root()
    d = root / case_name
    if create:
        logs = d / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        gitignore = d / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(CASE_GITIGNORE, encoding="utf-8")
    elif not d.is_dir():
        raise SuspiciousFileOperation(f"case does not exist: {case_name!r}")
    return d


def logs_dir(case_name: str, create: bool = False):
    d = case_dir(case_name, create=create) / "logs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def destination_path(
    case_name: str, upload_filename: str, create_case: bool = False, now=None
):
    """A collision-free path under <case>/logs/ for the scrubbed output, named
    `<YYYY-MM-DD-HHMMSS>-<upload name>.log` by the server clock, so a case that
    lives as long as its project still shows which log is new. Never overwrites
    an existing file -- appends -2, -3, ... instead."""
    logs = logs_dir(case_name, create=create_case)
    base = get_valid_filename(upload_filename) or "log"
    # Strip a redundant .log the upload might already carry, then re-add it,
    # so "foo.log" doesn't become "foo.log.log".
    if base.lower().endswith(".log"):
        base = base[: -len(".log")]
    base = f"{(now or datetime.now()).strftime(FILE_STAMP_FORMAT)}-{base}"
    candidate = logs / f"{base}.log"
    n = 2
    while candidate.exists():
        candidate = logs / f"{base}-{n}.log"
        n += 1
    return candidate
