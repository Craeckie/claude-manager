"""Per-case salt and names, kept outside LOG_ANALYSIS_ROOT and never read aloud.

The salt is the only thing preventing a dictionary attack on the redaction
placeholders (the IPv4 space is trivially enumerable otherwise), so it is
never rendered into a response, never logged, and this module offers no
function that returns its bytes to a caller that would print or ship them
anywhere but straight into `Redactor(salt=...)`.

HARD RULE for anyone editing this file: do not add a function that returns
the salt's contents for display, logging, or a test assertion on its value.
Existence and byte-length are the most a caller may ever check.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation

from .paths import NotConfigured, is_valid_case_name

_SALT_BYTES = 32


def state_root() -> Path:
    """The directory holding every case's salt and names, created on first use.

    Raises NotConfigured -- the same signal LOG_ANALYSIS_ROOT uses -- when the
    setting is missing or the directory cannot be created. It is deliberately not
    an OSError: deployed with REDACT_STATE_DIR unset, this fell back to the default
    inside the image (`/app/.redact-state`) on a read-only filesystem and the raw
    errno escaped the view as a 500. A misconfigured secret store has to say which
    setting is wrong, not hand back a traceback.
    """
    configured = getattr(settings, "REDACT_STATE_DIR", None)
    if not configured:
        raise NotConfigured("REDACT_STATE_DIR is not configured")
    root = Path(configured)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NotConfigured(
            f"REDACT_STATE_DIR ({root}) cannot be created: {exc.strerror}. "
            "In a container, point it at a writable volume -- the default lives "
            "inside the image and is not one."
        ) from exc
    if not os.access(root, os.W_OK):
        raise NotConfigured(f"REDACT_STATE_DIR ({root}) is not writable")
    return root


def _case_dir(case_name: str) -> Path:
    # The view validates before it gets here, but this module builds a filesystem
    # path out of user input, so it does not take that on trust: one refactor that
    # reorders the checks in views._do_redact would otherwise be a traversal.
    if not is_valid_case_name(case_name):
        raise SuspiciousFileOperation(f"invalid case name: {case_name!r}")
    d = state_root() / case_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def salt_path(case_name: str) -> Path:
    return _case_dir(case_name) / "salt"


def hosts_path(case_name: str) -> Path:
    return _case_dir(case_name) / "hosts"


def load_or_create_salt(case_name: str) -> bytes:
    """Return the case's salt, creating it (0600) on first use. The bytes
    returned are for constructing a Redactor only — never print, log, or
    otherwise surface them."""
    path = salt_path(case_name)
    if path.exists():
        return path.read_bytes()
    salt = os.urandom(_SALT_BYTES)
    path.write_bytes(salt)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # non-POSIX filesystem
    return salt


def load_hosts(case_name: str) -> list[str]:
    """Names to redact literally for this case. Read back into the upload
    form so the user sees their own data in their own browser — this is not
    the secret the salt is."""
    path = hosts_path(case_name)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_hosts(case_name: str, hosts: list[str]) -> None:
    path = hosts_path(case_name)
    path.write_text("\n".join(hosts) + ("\n" if hosts else ""), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def merge_hosts(case_name: str, hosts: list[str]) -> list[str]:
    """Union this upload's names with the ones the case already remembers, save
    the result, and return it.

    Deliberately a union rather than a replace. An upload can arrive with an
    empty or partial names box — a case name typed by hand instead of picked
    from the list, a second browser tab, a reload — and replacing would then
    scrub that file *less* thoroughly than the previous one with nothing
    anywhere saying so. Silent under-redaction is the failure this whole
    feature exists to prevent, so the box can only ever add names. Removing one
    is a rare, deliberate act: edit the case's `hosts` file.
    """
    merged = load_hosts(case_name)
    for name in hosts:
        if name not in merged:
            merged.append(name)
    save_hosts(case_name, merged)
    return merged
