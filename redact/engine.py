"""The scrubbing core. No HTTP, no Django imports beyond the app config.

Wraps `redact.log_redact.Redactor` (the canonical redactor; it is also a
stdlib-only CLI, `python3 redact/log_redact.py`) with the two primitives the
view needs:
line-at-a-time redaction, and a post-hoc leak check over the redacted output.
"""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from typing import Iterable, Iterator
from urllib.parse import urlsplit

from .log_redact import (
    DISPNAME_BODY, JSON_ID_FIELDS, KEEP_EXACT, KEEP_NETS, KEEP_REALMS, PASSWORD_KEEP,
    PRIVATE_NETS, RULES, SQL_MARKER, SQL_PART, TAILSCALE_NETS, URL_KEEP_HOST_ONLY, Redactor,
    _SSID_SKIP, json_row,
)

__all__ = ["redact_stream", "leak_check", "Redactor"]


def redact_stream(lines: Iterable[str], redactor: Redactor) -> Iterator[str]:
    """Scrub each line in turn. Safe to use line-at-a-time: no rule in RULES
    spans a newline, so this keeps memory constant on a large journal."""
    for line in lines:
        yield redactor.scrub(line)


# The leak check asks one question: does anything in the *output* still look
# like something the redactor was supposed to catch? So the shapes come from
# the redactor's own RULES rather than from a second hand-written set. A
# hand-rolled copy is how this check goes quietly vacuous — the first version
# of it could not match a `::`-compressed IPv6 address, which is precisely the
# gap that once let a real address through.
#
# Since idea #136, private/Tailscale addresses and *.ts.net names are kept by
# default (they carry nothing outside the tailnet they came from), so a
# surviving one is no longer a leak -- only genuinely public IPs, emails, and
# (since idea #130) a surviving tailnet login, stable node ID, Wi-Fi SSID, or
# a URL whose query string wasn't stripped get flagged here.
_RULE = dict(RULES)
_EMAIL_RE = re.compile(_RULE["email"])
_IPV4_RE = re.compile(_RULE["ipv4"])
_IPV6_RE = re.compile(_RULE["ipv6"])
_LOGIN_RE = re.compile(_RULE["login"])
_STABLEID_RE = re.compile(_RULE["stableid"])
_SSID_RE = re.compile(_RULE["ssid"])
_URL_RE = re.compile(_RULE["url"])
# idea #488 (Zulip). These rules can't match their own placeholders except
# where noted, so any hit in the output is a value that got through.
_OAUTH_RE = re.compile(_RULE["oauth"])
_ZREQUESTER_RE = re.compile(_RULE["zrequester"])
_ZUSERNAME_RE = re.compile(_RULE["zusername"])  # value may be placeholders
_PUSHTOKEN_RE = re.compile(_RULE["pushtoken"])  # 'token': value may be one
_UUID_RE = re.compile(_RULE["uuid"])
_UPLOAD_RE = re.compile(_RULE["uploadpath"])    # value may be a placeholder
_CAPPATH_RE = re.compile(_RULE["cappath"])
# Credentials and the other stacks' identifiers. Placeholders contain no
# character these values can start with, except where noted.
_SECRET_RE = re.compile(_RULE["secret"])
_AUTHHEADER_RE = re.compile(_RULE["authheader"])
_PASSWORD_RE = re.compile(_RULE["password"])      # value may be a flag ("false")
_MXID_RE = re.compile(_RULE["mxid"])
_CERTCN_RE = re.compile(_RULE["certcn"])          # value may be a placeholder
_OVPNPEER_RE = re.compile(_RULE["ovpnpeer"])
_ZTID_RE = re.compile(_RULE["ztid"])
_SSHUSER_RE = re.compile(_RULE["sshuser"])
# The redactor's dispname rule needs a raw email after the name; in the
# output that email is "<[USER-…]@[DOMAIN-…]>" (or still raw, if missed).
_DISPNAME_RE = re.compile(
    r"(?:(?<=[\['\"(])|(?<=, )|(?<=: ))" + DISPNAME_BODY
    + r"(?=<(?:\[USER-[0-9a-f]+\]|[A-Za-z0-9._%+-]+)@)"
)
# A scrubbed SSID is itself a quoted string ("[SSID-xxxxxx]") and would
# otherwise re-trip the ssidq/ssidk alternatives on the *output*.
_PLACEHOLDER_RE = re.compile(r"^\[[A-Z0-9]+-[0-9a-f]+\]$")
_ALL_PLACEHOLDERS_RE = re.compile(r"^(?:\[[A-Z0-9]+-[0-9a-f]+\]|@|%40)+$")


def _ip_is_kept(text: str) -> bool:
    candidate = text
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return True  # not really an address (e.g. a truncated MAC-like group) — ignore
    if candidate in KEEP_EXACT:
        return True
    return any(
        addr in net for net in (*KEEP_NETS, *PRIVATE_NETS, *TAILSCALE_NETS)
    )


def _string_values(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _string_values(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_values(v)


def leak_check(line: str, names: list[str] | None = None) -> Counter:
    """Count anything in `line` that still looks like a public IPv4/IPv6
    address or an email — i.e. something the redactor should have caught but
    didn't — minus the keep-list (loopback, multicast, TEST-NET, private,
    Tailscale, ::1, ...). A JSON-lines row is checked value by value,
    decoded, the way the redactor scrubbed it."""
    row = json_row(line.rstrip("\r\n"))
    if row is not None:
        hits: Counter = Counter()
        for value in _string_values(row):
            hits.update(_leak_check_entry(value))
        for key in JSON_ID_FIELDS:
            value = row.get(key)
            if isinstance(value, str) and value and not _PLACEHOLDER_RE.match(value):
                hits["jsonid"] += 1
    else:
        hits = _leak_check_entry(line)
    if names:
        for name in names:
            if name and name in line:
                hits["literal_name"] += 1
    return hits


def _doh_path_survived(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in URL_KEEP_HOST_ONLY and parsed.path not in ("", "/")


def _leak_check_entry(line: str) -> Counter:
    hits: Counter = Counter()

    for m in _EMAIL_RE.finditer(line):
        hits["email"] += 1

    for m in _IPV4_RE.finditer(line):
        if not _ip_is_kept(m.group()):
            hits["ipv4"] += 1

    for m in _IPV6_RE.finditer(line):
        if not _ip_is_kept(m.group()):
            hits["ipv6"] += 1

    for m in _LOGIN_RE.finditer(line):
        hits["login"] += 1

    for m in _STABLEID_RE.finditer(line):
        hits["stableid"] += 1

    for m in _SSID_RE.finditer(line):
        inner = (m.group("ssidq") or m.group("ssidu") or m.group("ssidk") or "").strip()
        if inner in _SSID_SKIP or _PLACEHOLDER_RE.match(inner):
            continue
        hits["ssid"] += 1

    for m in _URL_RE.finditer(line):
        # A kept diagnostic URL (tailscale.com, a DoH provider) has no query
        # string once scrubbed; one still carrying "?" means the query --
        # which always holds the identifying material -- survived. A DoH
        # provider is cut to scheme://host, so any path left on one is a
        # profile or device id that survived.
        if "?" in m.group() or _doh_path_survived(m.group()):
            hits["url"] += 1

    hits["oauth"] += len(_OAUTH_RE.findall(line))
    for m in _ZREQUESTER_RE.finditer(line):
        realm = m.group("zrealm") or m.group("zsub") or m.group("zin")
        if realm.lower() not in KEEP_REALMS:
            hits["zrequester"] += 1
    for m in _ZUSERNAME_RE.finditer(line):
        if not _ALL_PLACEHOLDERS_RE.match(m.group("zuval")):
            hits["zusername"] += 1
    for m in _PUSHTOKEN_RE.finditer(line):
        if not _PLACEHOLDER_RE.match(m.group()):
            hits["pushtoken"] += 1
    hits["uuid"] += len(_UUID_RE.findall(line))
    for m in _DISPNAME_RE.finditer(line):
        name = m.group("dnameq") or m.group("dnameu")
        if not _PLACEHOLDER_RE.match(name):
            hits["dispname"] += 1
    for m in _UPLOAD_RE.finditer(line):
        # The rule's value stops before "]", so the placeholder lacks its own.
        if not _PLACEHOLDER_RE.match(m.group("upval") + "]"):
            hits["uploadpath"] += 1
    hits["cappath"] += len(_CAPPATH_RE.findall(line))

    hits["secret"] += len(_SECRET_RE.findall(line))
    hits["authheader"] += len(_AUTHHEADER_RE.findall(line))
    for m in _PASSWORD_RE.finditer(line):
        if m.group("pwval").lower() not in PASSWORD_KEEP:
            hits["password"] += 1
    hits["mxid"] += len(_MXID_RE.findall(line))
    for m in _CERTCN_RE.finditer(line):
        if not _PLACEHOLDER_RE.match(m.group("cnval") + "]"):
            hits["certcn"] += 1
    hits["ovpnpeer"] += len(_OVPNPEER_RE.findall(line))
    hits["ztid"] += len(_ZTID_RE.findall(line))
    for m in _SSHUSER_RE.finditer(line):
        # A scrubbed Matrix id ("for user @[USER-...]") leaves just its "@".
        if m.group().strip("@.-_"):
            hits["sshuser"] += 1

    marker = SQL_MARKER.search(line)
    if marker:
        for m in SQL_PART.finditer(line, marker.end()):
            value = m.group("sqllit") or m.group("sqlkey") or m.group("sqlrow") or ""
            if value and not _PLACEHOLDER_RE.match(value):
                hits["sql"] += 1

    return +hits
