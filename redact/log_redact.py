#!/usr/bin/env python3
"""
log_redact.py — scrub identifying data from VPN, chat-server and service logs.

Replaces each secret with a stable HMAC-derived placeholder, so a given IP or
key maps to the same token everywhere. Log correlation survives; the values
don't. No dependencies beyond the stdlib.

What is always redacted, whatever produced the log:
  * credentials: vendor API keys and tokens (AWS, GitHub, GitLab, Slack,
    Stripe, Google, Matrix), JWTs, `Authorization:` header values,
    `password=`/`secret=`/`*_token=` values, OAuth/`api_key=` values;
  * identity: emails, sshd/PAM logins, chat user and room ids (Matrix
    `@user:server`, `!room:server`), display names next to an email,
    `/home/<user>` paths, certificate common names (`CN=`), OpenVPN
    `<user>/<ip>:<port>` peers;
  * network locators: public IPv4/IPv6 addresses, MAC addresses, Wi-Fi SSIDs,
    and every URL (DNS-over-HTTPS providers keep only their host);
  * VPN identity: WireGuard and Tailscale/Headscale keys, Tailscale auth
    keys, stable node ids and logins, ZeroTier node and network ids, log ids;
  * Zulip specifics: the realm in requesters, push tokens, upload and
    invitation paths, and the literals of a Postgres STATEMENT:/DETAIL: entry.

What is kept by default, since it carries nothing outside the network it came
from: RFC1918/link-local/ULA addresses, the CGNAT range VPNs such as Tailscale
use (100.64.0.0/10, fd7a:115c:a1e0::/48), `*.ts.net` names and device names.
Pass --redact-private/--redact-tailscale-ips/--redact-tailnet/--redact-names
to scrub those too. A server's own hostnames are in no rule: pass them with
--hosts.

JSON-lines exports are decoded, scrubbed value by value and re-encoded, so
they stay valid JSON: VictoriaLogs (`_msg`), Loki `logcli --output=jsonl`
(`line`), Docker's json-file driver and Synapse's JSON logs (`log`), and
`journalctl -o json` (`MESSAGE`; its machine and boot ids are tokenised).
A multi-line SQL statement is only covered on its first line.

    journalctl -u sshd | log_redact.py > clean.log
    Get-Content wireguard.log | py log_redact.py > clean.log
    log_redact.py --salt-file .redact-salt --hosts nas,thinkpad *.log

The salt is random per run unless --salt-file is given. Reuse the salt file to
keep placeholders consistent across several files or several days of logs; keep
it secret, since it is the only thing preventing a dictionary attack on the
placeholders (the IPv4 space is trivially enumerable otherwise).
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from hashlib import blake2b
from pathlib import Path
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Values that carry no information about the operator and stay readable.
# --------------------------------------------------------------------------

KEEP_EXACT = {
    "0.0.0.0", "255.255.255.255", "127.0.0.1",
    "::", "::1", "8.8.8.8", "8.8.4.4", "1.1.1.1", "9.9.9.9",
}
KEEP_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("ff00::/8"),
]
PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]
TAILSCALE_NETS = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
]

# --------------------------------------------------------------------------
# Patterns. Order matters: the first alternative that matches at a position
# wins, so specific rules precede general ones. Everything is applied in a
# single pass, so placeholders are never re-scanned.
# --------------------------------------------------------------------------

EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
# A display name, quoted ("Doe, John") or bare, followed by its " <".
DISPNAME_BODY = (r'(?:"(?P<dnameq>[^"\n]{1,128})"'
                 r"|(?P<dnameu>[^\s'\"<>\[\](),:][^'\"<>\[\](),:\n]{0,127}?)) ")

RULES: list[tuple[str, str]] = [
    # Auth material — redact unconditionally, these are live credentials.
    ("oauth", r"(?i)\b(?:client_secret|access_token|id_token|bearer|api_key)[=:\s\"']+[A-Za-z0-9._~+/-]{16,}={0,2}"),
    # Vendor credentials, recognisable by their own prefixes: AWS access key
    # ids, GitHub, GitLab, Slack, Stripe and Google API tokens, Synapse access
    # tokens, and JWTs (three base64url parts, the first two JSON objects).
    ("secret", r"\b(?:(?:AKIA|ASIA)[0-9A-Z]{16}"
               r"|gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255}"
               r"|glpat-[A-Za-z0-9_-]{20,}"
               r"|xox[abposr]-[A-Za-z0-9-]{10,}"
               r"|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
               r"|AIza[0-9A-Za-z_-]{35}"
               r"|syt_[A-Za-z0-9_-]{20,}"
               r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})(?![\w-])"),
    # Tailscale/Headscale auth keys, and the login URL that registers a node.
    ("authkey", r"tskey-(?:auth|api|client|scim|reusable)-[A-Za-z0-9_-]+"),
    ("authurl", r"https://login\.tailscale\.com/(?:a|admin/machines/new-linux)/[0-9a-zA-Z]+"),
    # An Authorization header's credentials, whatever the scheme: Basic is
    # user:password in plain base64. The header name and scheme stay.
    ("authheader", r"(?i)\b(?P<ahkeep>(?:proxy-)?authorization\\?[\"']?\s*[:=]\s*\\?[\"']?"
                   r"(?:basic|bearer|token|digest|negotiate)\s+)(?P<ahval>[A-Za-z0-9._~+/=-]{4,})"),
    # key=value / key: value credentials in config dumps, env listings and
    # query strings. A bare "token" is left out: Zulip's push rules own it.
    ("password", r"(?i)\b(?P<pwkeep>(?:password|passwd|pwd|secret|auth_token|refresh_token"
                 r"|private_token|secret_key|access_key)\\?[\"']?\s*[=:]\s*\\?[\"']?)"
                 r"(?P<pwval>[^\s\"'\\&,;\[\]{}()<>]{4,})"),

    # Zulip (idea #488). The realm (organisation subdomain) in a requester
    # "<user id>@<realm>" / "unauth@<realm>", in "subdomain=<realm>;" and in
    # send_email's "... email to [...] in <realm>". "root" is kept.
    ("zrequester", r"(?<![^\s(=])(?:\d+|unauth)@(?P<zrealm>[A-Za-z0-9][A-Za-z0-9-]*)(?![\w.@-])"
                   r"|\bsubdomain=(?P<zsub>[A-Za-z0-9][A-Za-z0-9-]*)(?=;)"
                   r"|(?<=\] in )(?P<zin>[A-Za-z0-9][A-Za-z0-9-]*)(?![\w.-])"),
    # The login in an "Authentication attempt" line; not always an email (LDAP).
    ("zusername", r"\busername=(?P<zuval>[^;\s\"']+)(?=;)"),
    # Mobile push: an FCM registration token anywhere, a 64-hex APNs token
    # after "device "/"token " or quoted, a repr'd dict's 'token' value.
    ("pushtoken", r"\b[A-Za-z0-9_-]{8,}:APA91b[A-Za-z0-9_-]{20,}"
                  r"|(?:(?<=device )|(?<=token )|(?<='))[0-9A-Fa-f]{64}\b"
                  r"|(?<='token': ')[^'\s]{8,}(?=')"),
    # The bouncer registration's server/user/realm UUIDs, and "remote user <uuid>:".
    ("uuid", r"(?:(?<=_uuid': ')|(?<=remote user ))"
             r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # The display name in "Name <email>"; the email is left to its own rule.
    ("dispname", r"(?:(?<=[\['\"(])|(?<=, )|(?<=: ))" + DISPNAME_BODY
                 + r"(?=<" + EMAIL + r">)"),
    # An upload's random segment and filename; /user_uploads/temporary/ is a
    # signed link. The realm id and the route stay readable.
    ("uploadpath", r"(?P<upkeep>/user_uploads/(?:(?:thumbnail|download)/)?(?:\d+|temporary)/)"
                   r"(?P<upval>[^\s\"'?#<>\]\)]+)"),
    # Capability URLs: invite, confirmation, password-reset and login tokens.
    ("cappath", r"(?P<capkeep>/(?:join|accounts/do_confirm|accounts/confirm_new_email"
                r"|accounts/login/subdomain|accounts/password/reset/[A-Za-z0-9_-]+"
                r"|accounts/unsubscribe/[a-z_]+)/)(?P<capval>[A-Za-z0-9_.:=-]+)"),

    # Tailnet login: the `self:`/`Persist{...}` lines' "u=<login>@<provider>"
    # and the unrelated "active login: <login>@<provider>" line (found only
    # by running this over real data -- the plan that added this rule only
    # knew about the "u=" form). Any URL -- most often a DNS-over-HTTPS
    # resolver embedding a provider profile id and device id in its
    # path/query -- follows.
    ("login", r'(?<!\w)(?P<loginprefix>u=\\?"?|active login: )'
              r"(?P<loginval>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)"),
    # The optional bracketed alternative matches a literal IPv6 host
    # (magicsock/derp endpoint URLs use these) with its own closing "]" so it
    # can never leave the match holding an unbalanced bracket -- the tail
    # still stops at the next one, which is always the surrounding text's
    # (a log line wrapping the URL in "[...]", or a JSON/array bracket).
    ("url", r"https?://(?:\[[0-9A-Fa-f:]+\])?[^\s\"'\]\)>]*"),

    # Bare stable node ID, as printed in "metrics: exit_node=..." and
    # "stableid=\"...\"" (the "nodeid:" rule below only catches the
    # "nodeid:<id>" label form, not these).
    ("stableid", r"\b[A-Za-z0-9]{6,}CNTRL\b"),

    # Wi-Fi SSID, from an `adb shell dumpsys wifi` dump: WifiInfo/ScanResult's
    # quoted or unquoted `SSID: ...` field, and a WifiConfiguration configKey
    # ("<ssid>"WPA_PSK). "<unknown ssid>" and an empty SSID are left alone.
    ("ssid", r'\bSSID: "(?P<ssidq>[^"]*)"'
             r'|\bSSID: (?P<ssidu>[^,\n]+?), BSSID:'
             r'|"(?P<ssidk>[^"]+)"(?=WPA_PSK|WPA_EAP|SAE|OWE|NONE|WEP\b)'),

    # Keys and stable node identity.
    ("key", r"(?i)\b(?:node|disco|machine|wg|m|nl|k|tka)key:[0-9a-f]{32,64}\b"),
    ("key2", r"(?i)\bd:[0-9a-f]{16,64}\b"),  # truncated disco key in magicsock lines
    ("keyb64", r"\b[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])"),
    ("nodeid", r"(?i)\bnodeid:[0-9a-zA-Z]+\b"),
    ("logid", r"(?i)\b(?:log ?id|collection|privateid|publicid)[=:\s\"']+[0-9a-f]{32,64}\b"),
    # ZeroTier: a node's 10-hex address and a network's 16-hex id where
    # zerotier-cli and its JSON print them, and the zt<hash> interface name.
    ("ztid", r"(?:(?<=200 info )|(?<=200 listpeers )|(?<=200 listnetworks )|(?<=\bjoin )"
             r"|(?<=\bleave )|(?<=\"nwid\": \")|(?<=\"nwid\":\")|(?<=\"address\": \")"
             r"|(?<=\"address\":\"))(?:[0-9a-f]{16}|[0-9a-f]{10})(?![\w])"
             r"|(?<![\w])zt[0-9a-z]{8}(?![\w])"),
    # "user bugreport: BUG-<publicLogID>-<timestamp>-<random>" embeds the
    # node's public log ID with no "log id:"-style label in front of it.
    ("bugmarker", r"\bBUG-(?P<bugid>[0-9a-f]{64})-\d{14}-[0-9a-f]{16}\b"),

    # Identity.
    # Matrix ids: @user:server, !room:server and #alias:server, also
    # percent-encoded the way a Synapse access log prints them in a path.
    ("mxid", r"(?<![\w@!#:%])(?P<mxsig>[@!#]|%(?:21|23|40))(?P<mxlocal>[A-Za-z0-9._=+-]+)"
             r"(?P<mxsep>:|%3[Aa])(?P<mxserver>(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}|localhost)"
             r"(?P<mxport>(?::|%3[Aa])\d+)?(?![\w.-])"),
    # An X.509 subject's common name (OpenVPN's VERIFY lines, TLS errors), and
    # an OpenVPN server's "<common name>/<ip>:<port>" peer prefix: one token
    # kind, so a client correlates across both.
    ("certcn", r"(?<![\w])CN=(?P<cnval>[^,/\s\"'\]]+)"),
    ("ovpnpeer", r"(?:(?<=\s)|^)(?P<ovval>[A-Za-z0-9_.@-]+)"
                 r"(?=/(?:\d{1,3}\.){3}\d{1,3}:\d+(?!\d))"),
    # The login in sshd's and PAM's auth lines.
    ("sshuser", r"(?:(?<=Accepted publickey for )|(?<=Accepted password for )"
                r"|(?<=Accepted keyboard-interactive/pam for )|(?<=Failed password for )"
                r"|(?<=Failed publickey for )|(?<=invalid user )|(?<=Invalid user )"
                r"|(?<=for user ))(?!invalid user )[A-Za-z0-9._@-]+"),
    # "%40" is the "@" of an email in a URL query string.
    ("email", r"\b[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("tailnet", r"\b(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+ts\.net\b"),
    ("userpath", r"(?i)[A-Z]:\\Users\\[^\\\s\"'<>|]+"),
    ("unixpath", r"/(?:home|Users)/[^/\s\"'<>|:]+"),

    # Network locators.
    ("mac", r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
    # Every RFC 4291 form, spelled out. A single "(?:h:){1,7}(?::|:?h)" misses
    # any address with groups after the "::" — fd7a:115c:a1e0::a401:333a, the
    # exact shape tailscaled prints for peers — and misses it silently.
    # False positives (a bare "1:2:3") cost nothing: _ip() hands anything
    # ipaddress rejects straight back unredacted.
    ("ipv6", r"(?<![\w:.])(?:"
             r"(?:(?:[0-9A-Fa-f]{1,4}:){1,6}|::(?:[Ff]{4}:)?)"
             r"(?::[0-9A-Fa-f]{1,4})*:?(?:\d{1,3}\.){3}\d{1,3}"  # v4-mapped
             r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
             r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
             r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
             r"|:(?::[0-9A-Fa-f]{1,4}){1,7}"
             r"|::"
             r")(?:%[0-9A-Za-z._-]+)?(?![\w:])"),
    # A trailing "-" is only allowed through when it introduces another
    # dotted quad (range notation, "10.0.0.0-10.255.255.255"); a "-" before
    # a non-address suffix ("1.86.0.1-beta") still blocks the match, or a
    # 4-part version string next to a hyphenated id would get swallowed.
    ("ipv4", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])(?!-(?!\d{1,3}\.))"),

    # Truncated key prefixes tailscaled prints as [aBcDe] when naming peers.
    ("shortkey", r"(?<![\w])\[[A-Za-z0-9+/]{5}\](?![\w])"),
]

# Group names must be unique in the combined pattern; several map to one kind.
GROUP_KIND = {
    "key2": "key",
    "ssidq": "ssid", "ssidu": "ssid", "ssidk": "ssid",
    "loginprefix": "login", "loginval": "login",
    "zrealm": "zrequester", "zsub": "zrequester", "zin": "zrequester",
    "zuval": "zusername", "dnameq": "dispname", "dnameu": "dispname",
    "upkeep": "uploadpath", "upval": "uploadpath",
    "capkeep": "cappath", "capval": "cappath",
    "ahkeep": "authheader", "ahval": "authheader",
    "pwkeep": "password", "pwval": "password",
    "mxsig": "mxid", "mxlocal": "mxid", "mxsep": "mxid", "mxserver": "mxid", "mxport": "mxid",
    "cnval": "certcn", "ovval": "ovpnpeer",
}
# A password= value that is a flag or a mask, not a secret.
PASSWORD_KEEP = {"true", "false", "null", "none", "hidden", "(hidden)", "redacted", "****", "*****"}
KEEP_REALMS = ("root",)

# The Zulip rules above cost as much as all the others together, so they only
# join the pattern for a line holding one of these. Each rule needs one of
# them to match at all; keep the two lists in step.
ZULIP_RULES = {"zrequester", "zusername", "pushtoken", "uuid", "dispname",
               "uploadpath", "cappath"}
ZULIP_TRIGGERS = ("@", "subdomain=", "] in ", "username=", "APA91b", "device ", "token ", "'",
                  "_uuid': '", "remote user ", "/user_uploads/", "/join/", "/accounts/")

# The same gate for every other rule that only fits one kind of log: each costs
# throughput on every line it is tried against, and together they doubled the
# time a plain daemon log took. A gate opens when its line holds one of its
# triggers -- a substring of the *lowercased* line, or a regex searched in the
# line as is. A gate only ever saves work: every rule needs one of its
# triggers to match at all, so keep each rule and its triggers in step.
RULE_GATES: dict[str, tuple[frozenset[str], tuple]] = {
    "zulip": (frozenset(ZULIP_RULES), tuple(t.lower() for t in ZULIP_TRIGGERS)),
    "secret": (frozenset({"secret"}), ("akia", "asia", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
                                       "github_pat_", "glpat-", "xox", "k_live_", "k_test_",
                                       "aiza", "syt_", "eyj")),
    "authheader": (frozenset({"authheader"}), ("authorization",)),
    "password": (frozenset({"password"}), ("passw", "pwd", "secret", "_token", "_key")),
    "mxid": (frozenset({"mxid"}), ("@", "!", "#", "%21", "%23", "%40")),
    "certcn": (frozenset({"certcn"}), ("cn=",)),
    "ovpnpeer": (frozenset({"ovpnpeer"}), (re.compile(r"/(?:\d{1,3}\.){3}\d{1,3}:\d"),)),
    "sshuser": (frozenset({"sshuser"}), ("accepted ", "failed p", "invalid user ", "for user ")),
    "ztid": (frozenset({"ztid"}), ("200 ", "join ", "leave ", "nwid", '"address"', "zt")),
}
GATED_RULES = frozenset().union(*(rules for rules, _ in RULE_GATES.values()))
# Most lines open no gate at all; one scan for any trigger settles that.
_ANY_TRIGGER = re.compile("|".join(
    re.escape(t) if isinstance(t, str) else t.pattern
    for _, triggers in RULE_GATES.values() for t in triggers
))

# Postgres: after one of these markers the rest of the entry is SQL whose
# literals psycopg interpolated client-side (message content, names), or a
# DETAIL's key/row values. A lookbehind can't anchor that, so scrub() splits
# the entry at the marker instead.
SQL_MARKER = re.compile(r"\b(?:STATEMENT|DETAIL):|\bstatement:|\bexecute [^\s:]+:")
SQL_LITERAL = r"'(?P<sqllit>(?:[^']|'')*)(?P<sqlend>'|$)"
SQL_VALUES = (r"(?<=\)=\()(?P<sqlkey>[^\n]*?)(?=\)(?: already exists| is not present"
              r"| is still referenced|\.|$))"
              r"|(?<=Failing row contains \()(?P<sqlrow>[^\n]*)(?=\))")
SQL_PART = re.compile(f"{SQL_LITERAL}|{SQL_VALUES}", re.M)


# JSON-lines exports whose rows are decoded and scrubbed value by value, keyed
# by the field that holds the message: VictoriaLogs (`_msg`), Loki's `logcli
# --output=jsonl` (`line`), Docker's json-file driver and Synapse (`log`),
# `journalctl -o json` (`MESSAGE`). A logtail row (`text`) is not one of them
# and stays on the text rules.
JSON_MSG_KEYS = ("_msg", "line", "log", "MESSAGE")
# Fields that are a host's or a boot's identity wherever they appear in such a
# row (journald's): the whole value becomes one token.
JSON_ID_FIELDS = {
    "_MACHINE_ID": "machineid", "_BOOT_ID": "bootid", "__CURSOR": "cursor",
    "_STREAM_ID": "streamid", "INVOCATION_ID": "invocation",
    "_SYSTEMD_INVOCATION_ID": "invocation",
}


def json_row(line: str) -> dict | None:
    """A JSON-lines row (one object with a JSON_MSG_KEYS field), else None.
    Its values are scrubbed decoded, so escaping can't hide or break a match."""
    if not line.lstrip().startswith("{") or not any(f'"{k}"' in line for k in JSON_MSG_KEYS):
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if isinstance(row, dict) and any(k in row for k in JSON_MSG_KEYS):
        return row
    return None

# Hosts that only ever appear in a diagnostic context: keep them fully
# readable, since they carry nothing an attacker couldn't already look up.
URL_KEEP_HOST_AND_PATH = ("tailscale.com", "tailscale.io")
URL_KEEP_HOST_ONLY = {
    "adblock.dns.mullvad.net", "base.dns.mullvad.net", "cloudflare-dns.com",
    "d.adguard-dns.com", "dns.adguard-dns.com", "dns.controld.com", "dns.google",
    "dns.mullvad.net", "dns.nextdns.io", "dns.quad9.net", "doh.opendns.com",
    "freedns.controld.com",
}
_SSID_SKIP = ("", "<unknown ssid>")

# Fields whose quoted value is a device or user name. The optional backslash
# before each quote covers both a bare JSON field ("Hostname":"laptop") and
# the same field JSON-escaped inside a logtail "text" value
# (\"Hostname\":\"laptop\", or Go's %v output Hostname:\"laptop\") with one
# rule, without ever touching the escaping itself.
JSON_NAME_FIELDS = (
    r'(?i)\b(?:host)?name(?:\\?")?\s*[:=]\s*\\?"'
    r'(?P<jsonval>(?:[^"\\]|\\.){1,128}?)\\?"'
)


class Redactor:
    def __init__(
        self,
        salt: bytes,
        hosts: list[str] | None = None,
        keep_private: bool = True,
        keep_tailscale_ips: bool = True,
        keep_tailnet_domains: bool = True,
        keep_short_keys: bool = False,
        redact_names: bool = False,
    ) -> None:
        self.salt = salt
        self.keep_private = keep_private
        self.keep_tailscale_ips = keep_tailscale_ips
        self.keep_tailnet_domains = keep_tailnet_domains
        self.stats: Counter[str] = Counter()

        def scoped(pat: str) -> str:
            # Global flags are only legal at the start of a combined pattern,
            # so per-rule (?i) becomes a scoped (?i:...) group.
            return f"(?i:{pat[4:]})" if pat.startswith("(?i)") else pat

        rules = [r for r in RULES if not (keep_short_keys and r[0] == "shortkey")]

        def build(rules: list[tuple[str, str]]) -> re.Pattern[str]:
            parts = [f"(?P<{name}>{scoped(pat)})" for name, pat in rules]
            if redact_names:
                parts.append(scoped(JSON_NAME_FIELDS))
            if hosts:
                alts = "|".join(re.escape(h) for h in sorted(hosts, key=len, reverse=True))
                parts.insert(0, f"(?P<host>(?<![\\w.-])(?:{alts})(?![\\w-]))")
            return re.compile("|".join(parts))

        self._build = build
        self._rules = rules
        # One compiled pattern per combination of open gates, built on first use.
        self._patterns: dict[frozenset[str], re.Pattern[str]] = {}
        self.pattern = build(rules)
        self.base_pattern = self._pattern_for(frozenset())
        self.name_field_re = re.compile(JSON_NAME_FIELDS)

    # -- placeholder construction ------------------------------------------

    def token(self, kind: str, value: str, width: int = 6) -> str:
        digest = hmac.new(self.salt, value.encode("utf-8", "replace"), blake2b).hexdigest()
        self.stats[kind] += 1
        return f"[{kind.upper()}-{digest[:width]}]"

    # -- per-match handling -------------------------------------------------

    def _ip(self, raw: str) -> str:
        text = raw
        zone = ""
        if "%" in text:
            text, _, zone = text.partition("%")
            zone = "%" + zone
        try:
            addr = ipaddress.ip_address(text)
        except ValueError:
            return raw  # not an address: a timestamp, a version, a ratio
        if text in KEEP_EXACT or any(addr in net for net in KEEP_NETS):
            return raw
        if self.keep_private and any(addr in net for net in PRIVATE_NETS):
            return raw
        if any(addr in net for net in TAILSCALE_NETS):
            if self.keep_tailscale_ips:
                return raw
            return self.token("tsip", text) + zone
        kind = "ip6" if addr.version == 6 else "ip4"
        if any(addr in net for net in PRIVATE_NETS):
            kind = "lan" + str(addr.version)
        return self.token(kind, text) + zone

    def _replace(self, m: re.Match[str]) -> str:
        kind = GROUP_KIND.get(m.lastgroup or "", m.lastgroup or "")
        raw = m.group()

        if kind in ("ipv4", "ipv6"):
            return self._ip(raw)

        if kind == "tailnet" and self.keep_tailnet_domains:
            return raw

        if kind == "jsonval":
            # Only the value inside the quotes is replaced; keep the field name.
            inner = m.group("jsonval")
            if not inner or inner.lower() in ("", "unknown", "null"):
                return raw
            return raw.replace(inner, self.token("name", inner.lower()))

        if kind == "bugmarker":
            # Only the public log ID is identifying; the timestamp and random
            # suffix stay visible. Tokenised under "logid", not "bugmarker",
            # so the same ID gets the same token whether it showed up here or
            # behind a "LogID:" label elsewhere in the log.
            bugid = m.group("bugid")
            return raw.replace(bugid, self.token("logid", bugid))

        if kind in ("key", "nodeid", "logid", "oauth"):
            # Keep the label ("nodekey:", "LogID: ") and replace only the value.
            tail = re.search(r"[0-9A-Za-z._~+/-]{8,}={0,2}$", raw)
            if tail:
                return raw[: tail.start()] + self.token(kind, tail.group())
            return self.token(kind, raw)

        if kind == "zrequester":
            group = next(g for g in ("zrealm", "zsub", "zin") if m.group(g))
            realm = m.group(group)
            if realm.lower() in KEEP_REALMS:
                return raw
            start, end = m.start(group) - m.start(), m.end(group) - m.start()
            return raw[:start] + self.token("realm", realm.lower()) + raw[end:]

        if kind == "zusername":
            val = m.group("zuval")
            if "@" in val:
                local, _, domain = val.partition("@")
                new = f"{self.token('user', local.lower())}@{self.token('domain', domain.lower())}"
            else:
                new = self.token("user", val.lower())
            return "username=" + new

        if kind == "dispname":
            if m.group("dnameq") is not None:
                return f'"{self.token("name", m.group("dnameq").lower())}" '
            return self.token("name", m.group("dnameu").lower()) + " "

        if kind == "authheader":
            return m.group("ahkeep") + self.token("auth", m.group("ahval"))

        if kind == "password":
            val = m.group("pwval")
            if val.lower() in PASSWORD_KEEP:
                return raw
            return m.group("pwkeep") + self.token("password", val)

        if kind == "mxid":
            sigil, local = m.group("mxsig"), m.group("mxlocal")
            if sigil in ("@", "%40"):
                local_tok = self.token("user", local.lower())
            else:  # a room id is case-sensitive, an alias is not
                local_tok = self.token("room", local if sigil in ("!", "%21") else local.lower())
            return (f"{sigil}{local_tok}{m.group('mxsep')}"
                    f"{self.token('domain', m.group('mxserver').lower())}{m.group('mxport') or ''}")

        if kind == "certcn":
            return "CN=" + self.token("cn", m.group("cnval"))

        if kind == "sshuser":
            return self.token("user", raw.lower())

        if kind == "ovpnpeer":
            return self.token("cn", m.group("ovval"))

        if kind == "uploadpath":
            return m.group("upkeep") + self.token("upload", m.group("upval"))

        if kind == "cappath":
            return m.group("capkeep") + self.token("cappath", m.group("capval"))

        if kind == "login":
            prefix = m.group("loginprefix")
            local, _, domain = m.group("loginval").partition("@")
            # Same "user"/"domain" tokens the email rule uses, so a login
            # correlates with any matching address seen elsewhere.
            return (f"{prefix}{self.token('user', local.lower())}"
                    f"@{self.token('domain', domain.lower())}")

        if kind == "url":
            try:
                parsed = urlsplit(raw)
            except ValueError:
                # A malformed/unbalanced-looking netloc (e.g. text mangled by
                # an earlier redaction pass): safest is to hash it whole
                # rather than raise, or leave a partial URL unredacted.
                return self.token("url", raw)
            host = (parsed.hostname or "").lower()
            if host in URL_KEEP_HOST_ONLY:
                return f"{parsed.scheme}://{host}"
            if host in URL_KEEP_HOST_AND_PATH or any(
                host.endswith("." + h) for h in URL_KEEP_HOST_AND_PATH
            ):
                return f"{parsed.scheme}://{host}{parsed.path}"
            # Query strings carry the identifying material (profile/device
            # ids); never kept, so the whole thing becomes one token.
            return self.token("url", raw)

        if kind == "ssid":
            for group in ("ssidq", "ssidu", "ssidk"):
                inner = m.group(group)
                if inner is None:
                    continue
                if inner.strip() in _SSID_SKIP:
                    return raw
                return raw.replace(inner, self.token("ssid", inner), 1)
            return raw

        if kind == "userpath":
            head, _, user = raw.rpartition("\\")
            return f"{head}\\{self.token('user', user.lower())}"

        if kind == "unixpath":
            head, _, user = raw.rpartition("/")
            return f"{head}/{self.token('user', user.lower())}"

        if kind == "email":
            sep = "@" if "@" in raw else "%40"
            local, _, domain = raw.partition(sep)
            return f"{self.token('user', local.lower())}{sep}{self.token('domain', domain.lower())}"

        if kind == "host":
            # Same namespace as quoted name= values, so a device gets one token.
            return self.token("name", raw.lower())

        return self.token(kind, raw)

    def _sql(self, m: re.Match[str]) -> str:
        if m.group("sqlkey") is not None or m.group("sqlrow") is not None:
            return self.token("sql", m.group())
        lit = m.group("sqllit")
        if not lit:
            return m.group()
        return f"'{self.token('sql', lit)}{m.group('sqlend')}"

    def _pattern_for(self, gates: frozenset[str]) -> re.Pattern[str]:
        pattern = self._patterns.get(gates)
        if pattern is None:
            allowed = frozenset().union(*(RULE_GATES[g][0] for g in gates))
            pattern = self._build([r for r in self._rules
                                   if r[0] not in GATED_RULES or r[0] in allowed])
            self._patterns[gates] = pattern
        return pattern

    def _sub(self, text: str) -> str:
        low = text.lower()
        if not _ANY_TRIGGER.search(low):
            return self.base_pattern.sub(self._replace, text)
        gates = frozenset(
            gate for gate, (_, triggers) in RULE_GATES.items()
            if any(t in low if isinstance(t, str) else t.search(text) for t in triggers)
        )
        return self._pattern_for(gates).sub(self._replace, text)

    def _scrub_sql(self, text: str) -> str:
        out, pos = [], 0
        for m in SQL_PART.finditer(text):
            out.append(self._sub(text[pos:m.start()]))
            out.append(self._sql(m))
            pos = m.end()
        out.append(self._sub(text[pos:]))
        return "".join(out)

    def _scrub_entry(self, text: str) -> str:
        """One log entry: a line, or a whole JSONL value. Everything after a
        Postgres SQL marker has its literals tokenised as well."""
        m = SQL_MARKER.search(text)
        if not m:
            return self._sub(text)
        return self._sub(text[:m.end()]) + self._scrub_sql(text[m.end():])

    def _scrub_value(self, value):
        if isinstance(value, str):
            return self._scrub_entry(value)
        if isinstance(value, list):
            return [self._scrub_value(v) for v in value]
        if isinstance(value, dict):
            return {k: (self.token(JSON_ID_FIELDS[k], v)
                        if k in JSON_ID_FIELDS and isinstance(v, str) and v
                        else self._scrub_value(v))
                    for k, v in value.items()}
        return value

    def scrub(self, text: str) -> str:
        out = []
        # "\n" only: splitlines() would also cut a JSONL row at a literal U+2028.
        for line in re.findall(r"[^\n]*\n|[^\n]+", text):
            body = line.rstrip("\r\n")
            row = json_row(body)
            if row is not None:
                out.append(json.dumps(self._scrub_value(row), ensure_ascii=False,
                                      separators=(",", ":")) + line[len(body):])
            else:
                out.append(self._scrub_entry(line))
        return "".join(out)


def load_salt(path: Path | None) -> bytes:
    if path is None:
        return os.urandom(32)
    if path.exists():
        return path.read_bytes()
    salt = os.urandom(32)
    path.write_bytes(salt)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows
    return salt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Redact identifying data from VPN, chat-server and service logs.",
        epilog="Reads stdin when no files are given. Output goes to stdout "
               "unless --in-place is used.",
    )
    p.add_argument("files", nargs="*", type=Path, help="log files to scrub")
    p.add_argument("--salt-file", type=Path,
                   help="persist the HMAC salt here for stable tokens across runs")
    p.add_argument("--hosts", default="",
                   help="comma-separated device/user names to redact literally")
    p.add_argument("--hosts-file", type=Path,
                   help="file with one name per line, added to --hosts")
    p.add_argument("--redact-private", dest="keep_private", action="store_false",
                   help="also redact RFC1918 / link-local addresses (default: left intact)")
    p.add_argument("--redact-tailscale-ips", dest="keep_tailscale_ips", action="store_false",
                   help="also redact 100.64.0.0/10 and fd7a:115c:a1e0::/48 (default: left intact)")
    p.add_argument("--redact-tailnet", dest="keep_tailnet_domains", action="store_false",
                   help="also redact *.ts.net domains (default: left intact)")
    p.add_argument("--keep-short-keys", action="store_true",
                   help="leave bracketed [aBcDe] peer key prefixes intact")
    p.add_argument("--redact-names", action="store_true",
                   help="also redact quoted name=/hostname= field values (default: left intact)")
    p.set_defaults(keep_private=True, keep_tailscale_ips=True, keep_tailnet_domains=True)
    p.add_argument("--in-place", action="store_true",
                   help="write <file>.redacted next to each input")
    p.add_argument("--stats", action="store_true",
                   help="print a redaction tally to stderr")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.hosts_file:
        hosts += [ln.strip() for ln in args.hosts_file.read_text().splitlines() if ln.strip()]

    red = Redactor(
        salt=load_salt(args.salt_file),
        hosts=hosts,
        keep_private=args.keep_private,
        keep_tailscale_ips=args.keep_tailscale_ips,
        keep_tailnet_domains=args.keep_tailnet_domains,
        keep_short_keys=args.keep_short_keys,
        redact_names=args.redact_names,
    )

    if not args.files:
        sys.stdout.write(red.scrub(sys.stdin.read()))
    else:
        for path in args.files:
            text = path.read_text(encoding="utf-8", errors="replace")
            out = red.scrub(text)
            if args.in_place:
                dest = path.with_suffix(path.suffix + ".redacted")
                dest.write_text(out, encoding="utf-8")
                print(f"{path} -> {dest}", file=sys.stderr)
            else:
                sys.stdout.write(out)

    if args.stats:
        total = sum(red.stats.values())
        print(f"\n{total} replacements:", file=sys.stderr)
        for kind, n in red.stats.most_common():
            print(f"  {kind:<9} {n}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
