"""The /redact/ screen.

Two upload paths, both explicitly avoiding Django's multipart handler (which
spills anything over FILE_UPLOAD_MAX_MEMORY_SIZE to a temp file on disk — the
one thing a log-redaction feature must never do):

- POST /redact/upload/  — JS path. Raw body, Content-Type: application/
  octet-stream, filename in X-Filename. Django never parses this as a form,
  so request.POST stays empty and the body stream is read directly here.
- POST /redact/preview/ — raw body like upload, but the redacted text comes
  back as JSON and nothing is written under LOG_ANALYSIS_ROOT (the log-uploader
  app's "redact & copy").
- All three read the user's own words and regexes from the X-Redact-Terms and
  X-Redact-Patterns headers (base64 of a JSON list), never the query string, and
  never store them.
- POST /redact/paste/   — JS-off fallback. An ordinary urlencoded POST (a
  <textarea>), bounded by DATA_UPLOAD_MAX_MEMORY_SIZE (16 MB, already raised
  for the screenshot-upload path) rather than the multipart temp-file path.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import os
from collections import Counter

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import logcat, paths, state
from .engine import CustomEntryError, Redactor, leak_check, redact_stream
from .log_redact import MAX_CUSTOM_ENTRIES, PROFILES, compile_custom, looks_like_build_log

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_CHUNK_SIZE = 65536
_HEADER_PEEK = 8
_PREVIEW_LINES = 20
_PROFILE_PEEK_LINES = 200

ARCHIVE_MAGICS = (
    b"PK\x03\x04",  # zip
    b"\x1f\x8b",  # gzip
    b"BZh",  # bzip2
    b"\xfd7zXZ",  # xz
)


class ArchiveRejected(Exception):
    pass


class UploadTooLarge(Exception):
    pass


def _parse_hosts(raw: str) -> list[str]:
    # Accepts either newline- or comma-separated; the form textarea uses
    # newlines (one name per line, matching log_redact.py's --hosts-file).
    hosts: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        name = chunk.strip()
        if name:
            hosts.append(name)
    return hosts


class CustomHeaderError(Exception):
    """X-Redact-Terms / X-Redact-Patterns that cannot be read or used."""

    def __init__(self, message, kind="", index=None):
        super().__init__(message)
        self.kind = kind
        self.index = index

    def response(self):
        body = {"error": str(self)}
        if self.kind:
            body["kind"] = self.kind
        if self.index is not None:
            body["index"] = self.index
        return JsonResponse(body, status=400)


def _header_entries(request, header):
    """A list of strings from a header holding base64 of a UTF-8 JSON list. The
    entries go in headers, never the query string, so they stay out of access
    logs and URLs; they are not stored anywhere and not logged."""
    raw = request.headers.get(header, "").strip()
    if not raw:
        return []
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("utf-8")
        entries = json.loads(decoded)
    except (binascii.Error, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise CustomHeaderError(f"{header} is not base64 of a UTF-8 JSON list") from None
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise CustomHeaderError(f"{header} must be a JSON list of strings")
    if len(entries) > MAX_CUSTOM_ENTRIES:
        raise CustomHeaderError(f"{header} has more than {MAX_CUSTOM_ENTRIES} entries")
    return entries


def _custom_entries(request):
    """(terms, patterns) the user sent for this request, validated. Raises
    CustomHeaderError, which `.response()` turns into a 400 naming the entry."""
    terms = _header_entries(request, "X-Redact-Terms")
    patterns = _header_entries(request, "X-Redact-Patterns")
    try:
        compile_custom(terms, patterns)
    except CustomEntryError as exc:
        raise CustomHeaderError(str(exc), exc.kind, exc.index) from None
    return terms, patterns


def _not_configured(request, exc=None):
    return render(
        request, "redact/_not_configured.html", {"reason": str(exc) if exc else ""},
        status=409,
    )


def _index_context(selected="", hosts_text=None, **extra):
    """Everything index.html needs besides the not-configured flag. `selected` is
    shown in the picker and its logs are listed -- the "is this log new" view."""
    selected = (selected or "").strip()
    if paths.is_valid_case_name(selected):
        selected = paths.canonical_case_name(selected)
    valid = paths.is_valid_case_name(selected) and not paths.is_reserved(selected)
    if hosts_text is None:
        hosts_text = "\n".join(state.load_hosts(selected)) if valid else ""
    project_cases, other_cases = paths.picker_cases()
    context = {
        "not_configured": False,
        "root": str(paths.log_analysis_root()),
        "project_cases": project_cases,
        "other_cases": other_cases,
        "selected_case": selected,
        "selected_logs": paths.case_logs(selected) if valid else [],
        "hosts_text": hosts_text,
    }
    context.update(extra)
    return context


@require_GET
def index(request):
    try:
        paths.log_analysis_root()
    except paths.NotConfigured:
        return render(request, "redact/index.html", {"not_configured": True})
    return render(
        request, "redact/index.html", _index_context(request.GET.get("case", ""))
    )


def _stream_lines_from(read):
    """read: a callable(size) -> bytes, e.g. HttpRequest.read. Yields decoded
    text lines. Raises ArchiveRejected / UploadTooLarge as guards trip.
    Never touches request.body — only ever calls read(n) for bounded chunks."""
    header = b""
    while len(header) < _HEADER_PEEK:
        chunk = read(_HEADER_PEEK - len(header))
        if not chunk:
            break
        header += chunk
    if any(header.startswith(magic) for magic in ARCHIVE_MAGICS):
        raise ArchiveRejected()

    total = len(header)
    if total > MAX_UPLOAD_BYTES:
        raise UploadTooLarge()

    buf = header
    while True:
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield (line + b"\n").decode("utf-8", errors="replace")
        chunk = read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise UploadTooLarge()
        buf += chunk
    if buf:
        yield buf.decode("utf-8", errors="replace")


def _resolve_profile(requested, case_name, head_lines):
    """`auto` picks `build` for a case listed in REDACT_BUILD_CASES or when the
    first lines look like package-manager / image-build output."""
    if requested in PROFILES:
        return requested
    if case_name.lower() in settings.REDACT_BUILD_CASES:
        return "build"
    return "build" if looks_like_build_log("".join(head_lines)) else "default"


def _write_stats(stats_path, stats, leak_hits, line_count, logfilter=None, profile="default",
                 custom=(0, 0)):
    total = sum(stats.values())
    lines = [f"{total} replacements:"]
    for kind, n in stats.most_common():
        lines.append(f"  {kind:<9} {n}")
    lines.append("")
    if logfilter is not None and logfilter.detected:
        # Before/after counts, so the next person can tell whether the filter cut
        # what it claimed to -- and so a misdetect is visible instead of silent.
        if logfilter.scoped_to:
            # logcat already ran --uid=<pkg> to produce this export (the
            # package: header only exists on that branch of the producer), so
            # the process filter would only drop this app's own tags. Skipped
            # entirely rather than run and report 0 dropped, so the stats say
            # *why* nothing was subtracted instead of leaving it to be assumed.
            lines.append(
                f"format: android-logcat — scoped by logcat to {logfilter.scoped_to}, "
                f"process filter skipped, {logfilter.kept} lines kept"
            )
        else:
            lines.append(
                f"format: android-logcat — {logfilter.dropped} lines dropped "
                f"(not from the anchored process), {logfilter.kept} kept"
            )
        if logfilter.continuations_kept:
            # A continuation line is kept by inheriting the fate of the entry
            # above it in its buffer, never on its own merits. That inheritance
            # being silent is what let a foreign continuation read as clean, so
            # it gets a number of its own.
            lines.append(
                f"  {logfilter.continuations_kept} continuation lines kept "
                "(inherited from the entry above)"
            )
        if logfilter.kept_body == 0:
            # Nothing survived the process filter. Silently, that would read
            # as "leak check: clean" on a file of header and dividers alone,
            # so it is loud instead of green.
            lines.append(
                "WARNING: no log lines survived the process filter — the export "
                "contains no recognised process; nothing to analyse"
            )
    if profile != "default":
        lines.append(f"profile: {profile}")
    if any(custom):
        # Counts only: the entries themselves are never written down.
        lines.append(f"custom entries: {custom[0]} terms, {custom[1]} patterns")
    lines.append(f"{line_count} lines scrubbed")
    if leak_hits:
        lines.append("LEAK CHECK — unredacted matches found:")
        for kind, n in leak_hits.most_common():
            lines.append(f"  {kind:<16} {n}")
    else:
        lines.append("leak check: clean")
    stats_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _Run:
    """One redaction pass over a stream: the redactor, the logcat filter and the
    leak check, shared by the upload paths (which write the lines to a case) and
    the preview (which returns them). `lines()` is a generator; `line_count` and
    `leak_hits` are complete once it is exhausted."""

    def __init__(self, redactor, logfilter, hosts, profile, source, terms=(), patterns=()):
        self.redactor = redactor
        self.logfilter = logfilter
        self.hosts = hosts
        self.terms = list(terms)
        self.patterns = list(patterns)
        self.profile = profile
        self._source = source
        self.line_count = 0
        self.leak_hits = Counter()

    def lines(self):
        # Subtraction before scrubbing: an Android logcat export is the whole
        # device's log, and the redactor's rules were written for daemon and
        # server logs. The filter drops every other process's lines before they
        # can be written; it is the identity function for anything that is not
        # logcat.
        for scrubbed in redact_stream(self.logfilter.run(self._source), self.redactor):
            self.line_count += 1
            self.leak_hits.update(leak_check(scrubbed, self.hosts, self.terms, self.patterns))
            yield scrubbed


def _profile_error(profile):
    if profile not in ("auto", *PROFILES):
        return {"error": f"unknown profile {profile!r}"}
    return None


def _start_run(read, case_name, hosts, salt, profile, terms=(), patterns=()):
    """Peek at the start of the stream for `auto`, then build the redactor. The
    peeked lines are chained back in front, so nothing is read twice and nothing
    touches disk. Raises ArchiveRejected / UploadTooLarge."""
    stream = _stream_lines_from(read)
    head = list(itertools.islice(stream, _PROFILE_PEEK_LINES))
    profile = _resolve_profile(profile, case_name, head)
    redactor = Redactor(
        salt=salt, hosts=hosts, profile=profile, terms=list(terms), patterns=list(patterns),
    )
    return _Run(
        redactor, logcat.LogcatFilter(redactor), hosts, profile,
        itertools.chain(head, stream), terms, patterns,
    )


_ARCHIVE_ERROR = {"error": "archive files are not accepted — upload the extracted log text"}


def _too_large_error():
    return {"error": f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"}


def _filter_warning(logfilter):
    if logfilter.detected and logfilter.kept_body == 0:
        return (
            "no log lines survived the process filter — the export contains no "
            "recognised process; nothing to analyse"
        )
    return ""


def _do_redact(request, case_name, filename, hosts, read, profile="auto", terms=(), patterns=()):
    # case_name arrives already resolved (see paths.resolve_case_name). This check
    # is the last line of defence rather than the user-facing validation: nothing
    # resolve_case_name returns can fail it, so reaching this means a caller built
    # a name some other way.
    if not paths.is_valid_case_name(case_name):
        return None, {"error": f"invalid case name: {case_name!r}"}, 400
    if paths.is_reserved(case_name):
        return None, {"error": f"{case_name!r} is reserved, not a case"}, 400

    if (error := _profile_error(profile)):
        return None, error, 400
    # Union, never replace: an upload whose names box was empty must not quietly
    # drop what the case already knows and scrub less than the last one did.
    hosts = state.merge_hosts(case_name, hosts)
    salt = state.load_or_create_salt(case_name)

    try:
        run = _start_run(read, case_name, hosts, salt, profile, terms, patterns)
    except ArchiveRejected:
        return None, _ARCHIVE_ERROR, 400
    except UploadTooLarge:
        return None, _too_large_error(), 400
    redactor, logfilter, profile = run.redactor, run.logfilter, run.profile

    try:
        dest = paths.destination_path(case_name, filename, create_case=True)
    except SuspiciousFileOperation as exc:
        return None, {"error": str(exc)}, 400

    preview: list[str] = []
    try:
        with open(dest, "w", encoding="utf-8", newline="") as out:
            os.chmod(dest, 0o644)
            for scrubbed in run.lines():
                out.write(scrubbed)
                if len(preview) < _PREVIEW_LINES:
                    preview.append(scrubbed.rstrip("\n"))
    except ArchiveRejected:
        dest.unlink(missing_ok=True)
        return None, _ARCHIVE_ERROR, 400
    except UploadTooLarge:
        dest.unlink(missing_ok=True)
        return None, _too_large_error(), 400
    line_count, leak_hits = run.line_count, run.leak_hits

    stats_path = dest.with_name(dest.name + ".redaction-stats")
    _write_stats(stats_path, redactor.stats, leak_hits, line_count, logfilter, profile,
                 (len(terms), len(patterns)))

    result = {
        "case": case_name,
        "profile": profile,
        "path": str(dest),
        "filename": dest.name,
        "line_count": line_count,
        "stats": dict(redactor.stats),
        "leak_hits": dict(leak_hits),
        "preview": preview,
        "format": "android-logcat" if logfilter.detected else "",
        "dropped": logfilter.dropped if logfilter.detected else 0,
        "warning": _filter_warning(logfilter),
    }
    return dest, result, 200


@require_POST
def upload(request):
    try:
        paths.log_analysis_root()
    except paths.NotConfigured as exc:
        return _not_configured(request, exc)

    # A project matches its folder, any other name is its own case, blank gets a
    # timestamp; a reserved folder is refused.
    try:
        case_name = paths.resolve_case_name(request.GET.get("case", ""))
    except paths.ReservedCaseName as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    filename = request.headers.get("X-Filename", "").strip()
    hosts = _parse_hosts(request.GET.get("hosts", ""))

    if not filename:
        return JsonResponse({"error": "missing X-Filename header"}, status=400)
    try:
        terms, patterns = _custom_entries(request)
    except CustomHeaderError as exc:
        return exc.response()

    try:
        dest, result, status = _do_redact(
            request, case_name, filename, hosts, request.read,
            profile=request.GET.get("profile", "auto") or "auto",
            terms=terms, patterns=patterns,
        )
    except paths.NotConfigured as exc:
        # The salt store is reached lazily, per case, so a broken REDACT_STATE_DIR
        # only surfaces here -- after the LOG_ANALYSIS_ROOT check has passed.
        return _not_configured(request, exc)
    if status != 200:
        return JsonResponse(result, status=status)

    return render(request, "redact/_result.html", {"result": result}, status=200)


@require_POST
def preview(request):
    """Redact the raw request body and return it as JSON, saving nothing.

    `?case=` borrows that case's salt and names (read-only: no case folder is
    created, and the names box is not merged), so placeholders match its logs.
    Without it a fixed state key `_preview` supplies a persistent salt.
    `?profile=` as for upload. Same size cap and archive refusal as upload.
    `X-Redact-Terms` / `X-Redact-Patterns` as for upload; a bad regex is a 400."""
    try:
        terms, patterns = _custom_entries(request)
    except CustomHeaderError as exc:
        return exc.response()
    try:
        raw_case = request.GET.get("case", "").strip()
        if raw_case:
            case_name = paths.resolve_case_name(raw_case)
            state_key = case_name
        else:
            case_name, state_key = "", state.PREVIEW_KEY
        profile = request.GET.get("profile", "auto") or "auto"
        if (error := _profile_error(profile)):
            return JsonResponse(error, status=400)
        hosts = state.load_hosts(state_key)
        salt = state.load_or_create_salt(state_key)
    except paths.ReservedCaseName as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except paths.NotConfigured as exc:
        return _not_configured(request, exc)

    try:
        run = _start_run(request.read, case_name, hosts, salt, profile, terms, patterns)
        text = "".join(run.lines())
    except ArchiveRejected:
        return JsonResponse(_ARCHIVE_ERROR, status=400)
    except UploadTooLarge:
        return JsonResponse(_too_large_error(), status=400)

    return JsonResponse({
        "text": text,
        "profile": run.profile,
        "stats": dict(run.redactor.stats),
        "leak_hits": dict(run.leak_hits),
        "line_count": run.line_count,
        "format": "android-logcat" if run.logfilter.detected else "",
        "warning": _filter_warning(run.logfilter),
    })


@require_POST
def paste(request):
    """JS-off fallback: an ordinary urlencoded POST, bounded by
    DATA_UPLOAD_MAX_MEMORY_SIZE rather than the multipart temp-file path."""
    try:
        paths.log_analysis_root()
    except paths.NotConfigured as exc:
        return _not_configured(request, exc)

    raw_case = request.POST.get("case") or request.POST.get("new_case") or ""
    filename = (request.POST.get("filename") or "pasted").strip()
    hosts = _parse_hosts(request.POST.get("hosts", ""))
    content = request.POST.get("content", "")

    def page(case, status=200, **extra):
        return render(
            request,
            "redact/index.html",
            _index_context(case, hosts_text="\n".join(hosts), **extra),
            status=status,
        )

    try:
        case_name = paths.resolve_case_name(raw_case)
    except paths.ReservedCaseName as exc:
        return page(raw_case, status=400, error=str(exc))
    try:
        terms, patterns = _custom_entries(request)
    except CustomHeaderError as exc:
        return page(case_name, status=400, error=str(exc))

    if not content:
        return page(case_name, status=400, error="paste some log text first")

    lines = content.splitlines(keepends=True)

    def read(_size, _it=iter(lines)):
        # Not chunked (the content is already fully in memory as POST data,
        # bounded by DATA_UPLOAD_MAX_MEMORY_SIZE), but satisfies the same
        # read(n)-shaped interface _stream_lines_from expects.
        try:
            return next(_it).encode("utf-8", errors="replace")
        except StopIteration:
            return b""

    try:
        dest, result, status = _do_redact(
            request, case_name, filename, hosts, read,
            profile=request.POST.get("profile", "auto") or "auto",
            terms=terms, patterns=patterns,
        )
    except paths.NotConfigured as exc:
        return _not_configured(request, exc)
    if status != 200:
        return page(case_name, status=status, error=result.get("error"))

    return page(case_name, result=result)
