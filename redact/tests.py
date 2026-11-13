"""Tests for the redact app.

HARD RULE for anyone touching these tests: never read, print, or assert on the
*contents* of a case's salt or hosts file. Existence and byte-length are the
most a test may check (see redact/state.py's module docstring). Correlation
between uploads is verified by observing which *placeholder tokens* come back
in the (public) redacted output, never by peeking at the salt.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from django.core.exceptions import SuspiciousFileOperation
from django.test import Client, TestCase, override_settings

from . import logcat, paths, state
from .engine import Redactor, leak_check, redact_stream
from .log_redact import looks_like_build_log
from .views import ARCHIVE_MAGICS, MAX_UPLOAD_BYTES

# Root writes through a 0o555 directory, so a read-only dir can't be faked (CI runs as root).
skip_if_root = unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions"
)

# One line per class that any log can carry, whatever produced it. Each
# product's own shapes have their own tables further down.
FIXTURE_LINES = [
    'contact me at person@example.com please',
    'interface has mac 3a:9c:11:0e:7b:22 configured',
    'dialing 203.0.113.9 wait that one is TEST-NET so try 198.18.0.5 instead',
    'v6 full form 2001:0db8:0000:0000:0000:0000:0000:0001 seen',
    'v6 compressed 2001:db8:85a3::8a2e:370:7334 seen',
    'v6 double-colon-only :: seen',
    'v6 leading-compressed ::1234:5678 seen',
    'mWifiInfo: SSID: "Guest-Net", BSSID: 3a:9c:11:0e:7b:22',
]

# The two gaps log_redact.py documents as *not* generically caught: a bare
# syslog hostname field and a local username / search-domain fragment. These
# only get scrubbed when passed explicitly via --hosts / hosts=.
GAP_LINES = [
    "Jan 15 15:01:03 desk1-desktop cron[812]: something happened",
    "resolv.conf search corpnet.example.internal",
]

# Shapes that once survived the redactor while "leak check: clean" was still
# reported (idea #130), with synthetic values: two DoH resolvers, logins from
# two providers, stable node ids, and SSIDs from a `dumpsys wifi` excerpt.
LEAKY_SHAPES = [
    ("doh_url_google", 'dns: upstream https://dns.google/resolve?name=intranet.example.org'
                       '&type=A failed'),
    ("doh_url_nextdns", 'dns: Set: {DefaultResolvers:[https://dns.nextdns.io/prof1x'
                        '?device_id=nTestNodeAa1CNTRL&device_name=phone-a-1'
                        '&device_model=android&device_ip=[TSIP-cd4ab0]] Routes:{}}'),
    ("login", 'netmap: self: [aBcDe] auth=machine-authorized u=bob@passkey '
              '[[TSIP-cd4ab0]/32]'),
    ("active_login", 'active login: carol@github'),
    ("exit_node_metric", 'metrics: exit_node=nTestB1CNTRL netcheck=29 tun_in=1 tun_out=1'),
    ("bugreport_stableid", 'user bugreport node info: nodeid="nodeid:[NODEID-5f0d39]" '
                           'stableid="nTestB1CNTRL" expiry="2027-03-15T08:32:54Z"'),
    ("suggested_exit_node_id", 'netmap: suggested exit node: desk1 (nTestB1CNTRL)'),
    ("dumpsys_wifiinfo_ssid", 'mWifiInfo: SSID: "Guest-Net", BSSID: '
                              '3a:9c:11:0e:7b:22, MAC: 02:00:00:00:00:00'),
    ("dumpsys_scanresult_ssid", 'ScanResult: SSID: Guest-Net, BSSID: '
                                '3a:9c:11:0e:7b:22, capabilities=[WPA2-PSK]'),
    ("dumpsys_configkey_ssid", 'WifiConfiguration: ID: 1 "Guest-Net"WPA_PSK '
                               'PRIORITY: 5'),
]
LEAKY_VALUES = ("intranet.example.org", "device_id", "phone-a-1", "nTestNodeAa1CNTRL",
                "nTestB1CNTRL", "bob@passkey", "carol@github", "Guest-Net")


class EngineRoundTripTests(TestCase):
    def _redactor(self, hosts=None):
        return Redactor(salt=b"\x01" * 32, hosts=hosts or [])

    def test_every_documented_class_is_scrubbed(self):
        red = self._redactor()
        for line in FIXTURE_LINES:
            out = "".join(redact_stream([line], red))
            self.assertNotIn("person@example.com", out)
            self.assertNotIn("3a:9c:11:0e:7b:22", out)
            self.assertNotIn("198.18.0.5", out)
            self.assertNotIn("2001:0db8:0000:0000:0000:0000:0000:0001", out)
            self.assertNotIn("2001:db8:85a3::8a2e:370:7334", out)
            self.assertNotIn("Guest-Net", out)
        # TEST-NET is on the keep-list.
        self.assertIn("203.0.113.9", "".join(redact_stream(FIXTURE_LINES[2:3], red)))

    def test_known_gaps_require_explicit_hosts(self):
        red_no_hosts = self._redactor()
        out = "".join(redact_stream(GAP_LINES, red_no_hosts))
        # Documented gap: survives without --hosts.
        self.assertIn("desk1-desktop", out)

        red_with_hosts = self._redactor(hosts=["desk1-desktop", "corpnet.example.internal"])
        out2 = "".join(redact_stream(GAP_LINES, red_with_hosts))
        self.assertNotIn("desk1-desktop", out2)
        self.assertNotIn("corpnet.example.internal", out2)

    def test_hyphen_adjacent_ipv4_ranges_are_both_scrubbed(self):
        # Both sides of a range notation are addresses (idea #116 gap 1). Uses
        # the opt-in redact flags, since these ranges are kept by default and
        # this test is about the regex, not the default policy.
        red = Redactor(salt=b"\x01" * 32, keep_private=False, keep_tailscale_ips=False)
        out = "".join(redact_stream(
            ["Black listing: 10.0.0.0-10.255.255.255"], red
        ))
        self.assertNotIn("10.0.0.0", out)
        self.assertNotIn("10.255.255.255", out)
        # The two placeholders are still joined by the hyphen that was there.
        self.assertRegex(out, r"\[LAN4-[0-9a-f]{6}\]-\[LAN4-[0-9a-f]{6}\]")

        out2 = "".join(redact_stream(["range 100.64.1.5-100.64.1.9"], red))
        self.assertNotIn("100.64.1.5", out2)
        self.assertNotIn("100.64.1.9", out2)
        self.assertRegex(out2, r"\[TSIP-[0-9a-f]{6}\]-\[TSIP-[0-9a-f]{6}\]")

    def test_hyphen_adjacent_ipv4_fix_does_not_swallow_dotted_versions(self):
        # These are why "-" was presumably in the ipv4 lookaround to begin
        # with; pin the observed (unredacted, since these are not addresses
        # ipaddress.ip_address accepts) behaviour after widening the rule.
        red = self._redactor()
        out = "".join(redact_stream(["running 1.86.0.1-beta now"], red))
        self.assertIn("1.86.0.1", out)
        out2 = "".join(redact_stream(["tag build-1.2.3.4-x here"], red))
        self.assertIn("1.2.3.4", out2)

    def _logtail_line(self, text):
        return json.dumps({
            "logtail": {
                "client_time": "2025-01-15T10:00:00Z",
                "proc_id": 1,
                "proc_seq": 2,
            },
            "v": 1,
            "text": text,
        })

    def test_names_survive_by_default(self):
        # Quoted name=/hostname= fields are only redacted on request (idea #136).
        red = self._redactor()
        line = self._logtail_line('Hostname:"laptop" reporting')
        out = "".join(redact_stream([line], red))
        self.assertIn("laptop", out)

    def test_escaped_quote_names_inside_a_logtail_envelope_are_scrubbed(self):
        # Go's %v output and embedded JSON put an escaped quote around the
        # name; all three shapes must match (idea #117). Names are opt-in
        # (idea #136), so redact_names=True is passed explicitly here.
        red = Redactor(salt=b"\x01" * 32, redact_names=True)
        cases = [
            ('Hostname:"laptop" reporting', "laptop"),        # Go %v, unquoted key
            ('"Hostname":"nas-synology" seen', "nas-synology"),  # embedded JSON
            ('hostname="Test Phone" connected', "Test Phone"),      # key=value
        ]
        for text, name in cases:
            line = self._logtail_line(text)
            out = "".join(redact_stream([line], red))
            self.assertNotIn(name, out, text)
            # The fix must only ever replace the inner value -- the envelope
            # and its escaping must still parse as JSON.
            parsed = json.loads(out)
            self.assertNotIn(name, parsed["text"], text)
            self.assertIn("[NAME-", parsed["text"], text)

    def test_unescaped_quoted_names_still_scrub_without_an_envelope(self):
        # The plain JSON and key=value forms, outside any envelope.
        red = Redactor(salt=b"\x01" * 32, redact_names=True)
        out = "".join(redact_stream(['"Hostname":"media-server" seen'], red))
        self.assertNotIn("media-server", out)
        out2 = "".join(redact_stream(['hostname="Test Phone" connected'], red))
        self.assertNotIn("Test Phone", out2)

    def test_logtail_json_lines_are_not_sniffed_as_logcat(self):
        # Logtail files go straight to the redactor, never through the
        # Android logcat pre-filter (idea #117).
        lines = [
            self._logtail_line("magicsock: home is derp-10") + "\n",
            self._logtail_line("control: sendStatus") + "\n",
            self._logtail_line('Hostname:"laptop" reporting') + "\n",
        ]
        self.assertFalse(logcat.sniff(lines))


class LeakyShapesTests(TestCase):
    """These shapes survived the redactor while the stats file said "leak
    check: clean" (idea #130): both the scrub and the leak check were blind to
    them. This class proves the leak check sees each one on the raw line (a
    leak check that cannot see a leak is worse than none), then that the scrub
    removes every one of them and the leak check goes quiet on the output."""

    def _redactor(self):
        return Redactor(salt=b"\x03" * 32)

    def test_leak_check_flags_every_shape_on_the_raw_line(self):
        for name, line in LEAKY_SHAPES:
            hits = leak_check(line)
            self.assertGreater(sum(hits.values()), 0, f"{name}: {line}")

    def test_scrub_removes_every_leaky_shape(self):
        red = self._redactor()
        for name, line in LEAKY_SHAPES:
            out = "".join(redact_stream([line], red))
            for value in LEAKY_VALUES:
                self.assertNotIn(value, out, name)
            hits = leak_check(out)
            self.assertEqual(sum(hits.values()), 0, f"{name}: {out}")

    def test_exit_node_line_keeps_hostname_loses_only_id(self):
        # Node names stay readable for debugging; only the stable node id in
        # parens is identifying.
        red = self._redactor()
        out = "".join(redact_stream(
            ["netmap: suggested exit node: desk1 (nTestB1CNTRL)"], red
        ))
        self.assertIn("desk1", out)
        self.assertNotIn("nTestB1CNTRL", out)
        self.assertIn("[STABLEID-", out)

    def test_tailscale_control_url_keeps_host_and_path(self):
        red = self._redactor()
        out = "".join(redact_stream(
            ["control: connecting to https://controlplane.tailscale.com/machine/map"], red
        ))
        self.assertIn("controlplane.tailscale.com/machine/map", out)

    def test_unrecognised_url_is_fully_tokenised(self):
        red = self._redactor()
        out = "".join(redact_stream(
            ["fetching https://example.com/some/path?token=secret"], red
        ))
        self.assertNotIn("example.com", out)
        self.assertNotIn("token=secret", out)
        self.assertIn("[URL-", out)

    def test_login_rule_does_not_match_inside_another_word(self):
        red = self._redactor()
        out = "".join(redact_stream(["menu=settings shown"], red))
        self.assertIn("menu=settings", out)

    def test_unknown_and_empty_ssid_are_kept(self):
        red = self._redactor()
        out = "".join(redact_stream(
            ['SSID: <unknown ssid>, BSSID: 3a:9c:11:0e:7b:22'], red
        ))
        self.assertIn("<unknown ssid>", out)
        out2 = "".join(redact_stream(['SSID: "", BSSID: 3a:9c:11:0e:7b:22'], red))
        self.assertIn('SSID: ""', out2)


_DNS_QUERY = "AAABAAABAAAAAAAAB2ludHJhbmV0B2V4YW1wbGUDb3JnAAABAAE"  # RFC 8484 ?dns=

# DNS-over-HTTPS resolvers, each in its own provider's URL shape: the RFC 8484
# `?dns=` query, a JSON API query, a client id or profile in the path, and the
# device query a VPN's DoH integration appends. (provider, url, parts that
# must not survive)
DOH_PROVIDERS = [
    ("AdGuard", "https://d.adguard-dns.com/dns-query/a1b2c3d4", ("/dns-query", "a1b2c3d4")),
    ("Cloudflare", f"https://cloudflare-dns.com/dns-query?dns={_DNS_QUERY}",
     ("/dns-query", "dns=", _DNS_QUERY)),
    ("ControlD", "https://dns.controld.com/abc123def/laptop-alice", ("abc123def", "laptop-alice")),
    ("Google", "https://dns.google/resolve?name=intranet.example.org&type=A",
     ("/resolve", "intranet.example.org", "type=A")),
    ("Mullvad", f"https://dns.mullvad.net/dns-query?dns={_DNS_QUERY}", ("/dns-query", _DNS_QUERY)),
    ("NextDNS", "https://dns.nextdns.io/prof1x?device_id=nTestNodeAa1CNTRL"
                "&device_name=laptop-alice&device_model=android",
     ("prof1x", "device_id", "nTestNodeAa1CNTRL", "laptop-alice")),
    ("Quad9", f"https://dns.quad9.net/dns-query?dns={_DNS_QUERY}", ("/dns-query", _DNS_QUERY)),
]


class DohProviderTests(TestCase):
    def test_each_provider_keeps_only_scheme_and_host(self):
        red = Redactor(salt=b"\x03" * 32)
        for provider, url, gone in DOH_PROVIDERS:
            with self.subTest(provider):
                line = f"dns: resolver {url} ready"
                self.assertGreater(leak_check(line)["url"], 0)
                out = "".join(redact_stream([line], red))
                host = urlsplit(url).hostname
                self.assertEqual(out, f"dns: resolver https://{host} ready")
                self.assertNotIn(url[len(f"https://{host}"):], out)
                for part in gone:
                    self.assertNotIn(part, out)
                self.assertEqual(sum(leak_check(out).values()), 0, out)


# Logins as a VPN coordination server prints them, "<user>@<provider>": the
# provider is an identity service, so not always a mail domain.
LOGINS = ["alice@example.org", "alice@github", "alice@gmail.com", "alice@passkey"]
LOGIN_SHAPES = [
    "netmap: self: [aBcDe] auth=machine-authorized u={login} [100.64.0.1/32]",
    'Persist{o=, n=[aBcDe] u="{login}"}',
    "active login: {login}",
]


class LoginProviderTests(TestCase):
    def test_every_provider_is_scrubbed_in_every_shape(self):
        red = Redactor(salt=b"\x03" * 32)
        for login in LOGINS:
            for shape in LOGIN_SHAPES:
                line = shape.replace("{login}", login)
                with self.subTest(line):
                    self.assertGreater(sum(leak_check(line).values()), 0)
                    out = "".join(redact_stream([line], red))
                    self.assertNotIn("alice", out)
                    self.assertNotIn(login.split("@")[1], out)
                    self.assertRegex(
                        out, r'(?:u="?|active login: )\[USER-[0-9a-f]{6}\]@\[DOMAIN-[0-9a-f]{6}\]'
                    )
                    self.assertEqual(sum(leak_check(out).values()), 0, out)


class LeakCheckTests(TestCase):
    def test_clean_line_is_not_flagged(self):
        red = Redactor(salt=b"\x02" * 32)
        scrubbed = "".join(redact_stream(["contact person@example.com now"], red))
        hits = leak_check(scrubbed)
        self.assertEqual(sum(hits.values()), 0)

    def test_deliberately_leaky_fixture_is_flagged(self):
        # Only the email is a leak: 203.0.113.9 (TEST-NET-3) is kept.
        leaky = "still has person@example.com and 203.0.113.9"
        hits = leak_check(leaky)
        self.assertGreater(hits["email"], 0)
        self.assertEqual(hits.get("ipv4", 0), 0)

    def test_surviving_link_local_is_not_flagged(self):
        # fe80::/10 is in PRIVATE_NETS, kept by default (idea #136), so one
        # surviving in the output is intended, not a leak.
        hits = leak_check("peer at fe80::1c2d:3e4f:5a6b:7c8d%eth0 still here")
        self.assertEqual(hits.get("ipv6", 0), 0)

    def test_keep_listed_addresses_are_not_flagged(self):
        line = "loopback 127.0.0.1 and doc range 203.0.113.5 and multicast 224.0.0.1"
        hits = leak_check(line)
        self.assertEqual(sum(hits.values()), 0)

    def test_hyphen_adjacent_ipv4_range_is_flagged(self):
        # Both sides of a leaked public range are counted (idea #116 gap 1).
        hits = leak_check("x 93.184.216.0-93.184.216.255")
        self.assertEqual(hits["ipv4"], 2)


TMP_ROOT_PREFIX = "redact-tests-"


def _mk_tmp(prefix):
    return tempfile.mkdtemp(prefix=f"{TMP_ROOT_PREFIX}{prefix}-")


# What an uploaded file is called since idea #319: <YYYY-MM-DD-HHMMSS>-<name>.log
STAMPED = r"\d{4}-\d{2}-\d{2}-\d{6}-"


def _one_log(log_root, case, name):
    """The single file in <case>/logs/ that an upload named `name` produced.

    Since idea #319 the server prefixes every file with its own timestamp, so a
    test cannot spell the path out; it finds the one file whose name is that
    timestamp plus `name` (a `.redaction-stats` name works the same way).
    """
    logs = Path(log_root) / case / "logs"
    hits = [
        p for p in logs.iterdir()
        if re.fullmatch(STAMPED + re.escape(name), p.name)
    ] if logs.is_dir() else []
    assert len(hits) == 1, f"expected one {name} in {logs}, found {hits}"
    return hits[0]


def _picker_options(html):
    """The option values, read the way an upload client that scrapes the page
    reads them: the block from id="redact-case-select" to </select>, then every
    <option value="...">."""
    block = re.search(r'id="redact-case-select"[\s\S]*?</select>', html)
    if not block:
        return []
    return re.findall(r'<option value="([^"]+)"', block.group(0))


class CaseIsProjectTests(TestCase):
    """idea #319: a case is the project folder (or any free name), and every
    uploaded file carries its own timestamp instead of the folder carrying one."""

    def setUp(self):
        from core.models import Project

        self.log_root = _mk_tmp("logs")
        self.state_root = _mk_tmp("state")
        self.addCleanup(shutil.rmtree, self.log_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.override = override_settings(
            LOG_ANALYSIS_ROOT=self.log_root, REDACT_STATE_DIR=self.state_root
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        Project.objects.create(name="MusicBox", folder="MusicBox")
        Project.objects.create(name="Router", folder="router")
        self.client = Client()

    def _upload(self, case, filename, body):
        return self.client.post(
            "/redact/upload/?" + urlencode({"case": case}),
            data=body,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=filename,
        )

    # -- names --------------------------------------------------------------

    def test_a_project_name_resolves_to_its_folder_whatever_the_capitals(self):
        self.assertEqual(paths.resolve_case_name("musicbox"), "MusicBox")
        self.assertEqual(paths.resolve_case_name("MUSICBOX"), "MusicBox")
        self.assertEqual(paths.resolve_case_name(" MusicBox "), "MusicBox")
        self.assertEqual(paths.resolve_case_name("Router"), "router")

    def test_a_free_name_is_its_own_case(self):
        self.assertEqual(paths.resolve_case_name("acme-shop"), "acme-shop")
        self.assertEqual(paths.resolve_case_name("My_Bank-2"), "My_Bank-2")
        # Not a valid name as typed: slugified, but still no timestamp.
        self.assertEqual(paths.resolve_case_name("desk1 DNS!"), "desk1-dns")

    def test_an_existing_case_folder_is_matched_whatever_the_capitals(self):
        (Path(self.log_root) / "MyCase").mkdir()
        self.assertEqual(paths.resolve_case_name("mycase"), "MyCase")

    def test_blank_still_gives_the_timestamp_fallback(self):
        now = datetime(2025, 1, 15, 8, 53)
        self.assertEqual(paths.resolve_case_name("", now=now), "2025-01-15-0853")
        self.assertEqual(paths.resolve_case_name("!!!", now=now), "2025-01-15-0853")

    def test_old_dated_cases_still_pass_through(self):
        for name in ("2025-01-15-router", "2025-01-15-0853", "2025-01-15-1355-musicbox"):
            self.assertEqual(paths.resolve_case_name(name), name)

    def test_traversal_is_still_contained(self):
        self.assertEqual(paths.resolve_case_name("../etc"), "etc")
        self.assertEqual(paths.resolve_case_name("/etc/passwd"), "etc-passwd")
        for bad in ("../", "..", "a/b", "a.b", ".git", "-x", "x" * 65, ""):
            self.assertFalse(paths.is_valid_case_name(bad), bad)
        self.assertTrue(paths.is_valid_case_name("x" * 64))

    def test_reserved_names_are_refused(self):
        tool = Path(self.log_root) / "toolbox"
        tool.mkdir()
        (tool / "CLAUDE.md").write_text("# a shared tool\n")
        for name in ("uploaded-logs", "Uploaded-Logs", "../uploaded-logs", "toolbox", "ToolBox"):
            with self.assertRaises(paths.ReservedCaseName, msg=name):
                paths.resolve_case_name(name)
        with self.assertRaises(SuspiciousFileOperation):
            paths.case_dir("uploaded-logs", create=True)

    # -- files ---------------------------------------------------------------

    def test_the_file_name_carries_the_server_timestamp(self):
        now = datetime(2025, 1, 15, 9, 5, 7)
        first = paths.destination_path("router", "host.log", create_case=True, now=now)
        self.assertEqual(first.name, "2025-01-15-090507-host.log")
        first.write_text("one")
        # Same second, same name: the -2 backstop, never an overwrite.
        second = paths.destination_path("router", "host.log", create_case=True, now=now)
        self.assertEqual(second.name, "2025-01-15-090507-host-2.log")

    def test_two_uploads_to_a_project_share_its_folder_and_its_salt(self):
        line = b"peer at 93.184.216.34 right now\n"
        a = self._upload("router", "a.log", line)
        b = self._upload("Router", "b.log", line)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a.context["result"]["case"], "router")
        self.assertEqual(b.context["result"]["case"], "router")

        self.assertEqual(sorted(p.name for p in Path(self.log_root).iterdir()), ["router"])
        token_a = _one_log(self.log_root, "router", "a.log").read_text().split("peer at ")[1]
        token_b = _one_log(self.log_root, "router", "b.log").read_text().split("peer at ")[1]
        self.assertNotIn("93.184.216.34", token_a)
        self.assertEqual(token_a, token_b, "one salt per case means one token")
        _one_log(self.log_root, "router", "a.log.redaction-stats")
        # One salt file, checked by existence only -- never by its bytes.
        self.assertEqual(
            sorted(p.name for p in Path(self.state_root).iterdir()), ["router"]
        )
        self.assertTrue(state.salt_path("router").is_file())

    def test_the_result_shows_the_timestamped_file_name(self):
        resp = self._upload("musicbox", "player.log", b"hello\n")
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(resp.content.decode(), STAMPED + r"player\.log")
        self.assertContains(resp, 'data-case="MusicBox"')

    def test_an_upload_to_a_reserved_name_is_refused_and_writes_nothing(self):
        resp = self._upload("uploaded-logs", "a.log", b"hello\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("reserved", resp.json()["error"])
        self.assertEqual(list(Path(self.log_root).iterdir()), [])
        self.assertEqual(list(Path(self.state_root).iterdir()), [])

    def test_a_paste_to_a_reserved_name_is_refused(self):
        resp = self.client.post(
            "/redact/paste/",
            data=urlencode({"case": "uploaded-logs", "content": "hello\n"}),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "reserved", status_code=400)
        self.assertEqual(list(Path(self.log_root).iterdir()), [])

    # -- the picker ----------------------------------------------------------

    def _seed_cases(self):
        root = Path(self.log_root)
        for case, files in {
            "router": ["2025-01-14-080000-a.log", "2025-01-15-201500-b.log"],
            "2025-01-15-old": ["host.log"],
        }.items():
            (root / case / "logs").mkdir(parents=True)
            for f in files:
                (root / case / "logs" / f).write_text("x\n")
                (root / case / "logs" / (f + ".redaction-stats")).write_text("x\n")
        (root / "uploaded-logs").mkdir()
        (root / "toolbox").mkdir()
        (root / "toolbox" / "CLAUDE.md").write_text("# tool\n")

    def test_the_picker_lists_projects_then_other_cases(self):
        self._seed_cases()
        html = self.client.get("/redact/").content.decode()
        self.assertIn('<optgroup label="Projects">', html)
        self.assertIn('<optgroup label="Other cases">', html)
        self.assertIn("(2 logs, last 2025-01-15)", html)
        self.assertNotIn("uploaded-logs</option>", html)
        self.assertNotIn("toolbox", html)
        # Projects (by folder, case-insensitive order) first, then the rest.
        self.assertEqual(
            _picker_options(html), ["MusicBox", "router", "2025-01-15-old"]
        )

    def test_a_selected_case_lists_its_logs_newest_first(self):
        self._seed_cases()
        resp = self.client.get("/redact/?case=Router")
        html = resp.content.decode()
        self.assertEqual(resp.context["selected_case"], "router")
        self.assertIn('<option value="router" selected', html)
        newer = html.index("2025-01-15-201500-b.log")
        older = html.index("2025-01-14-080000-a.log")
        self.assertLess(newer, older)
        self.assertIn("2025-01-15 20:15:00", html)
        self.assertNotIn(".redaction-stats", html)


class CaseNamingTests(TestCase):
    def test_slugify_keyword(self):
        self.assertEqual(paths.slugify_keyword("router"), "router")
        self.assertEqual(paths.slugify_keyword("desk1 DNS!"), "desk1-dns")
        self.assertEqual(paths.slugify_keyword("  Spaced  Out  "), "spaced-out")
        self.assertEqual(paths.slugify_keyword("!!!"), "")
        self.assertEqual(paths.slugify_keyword(""), "")
        self.assertLessEqual(len(paths.slugify_keyword("x" * 200)), paths.MAX_KEYWORD)

    def test_slugify_cannot_escape_the_root(self):
        # A traversal attempt becomes a duller keyword, never a path.
        for attempt in ("../etc", "/etc/passwd", "..", "a/../../b"):
            slug = paths.slugify_keyword(attempt)
            self.assertNotIn("/", slug)
            self.assertNotIn(".", slug)

    def test_generate_case_name(self):
        now = datetime(2025, 1, 15, 8, 53)
        self.assertEqual(paths.generate_case_name("", now=now), "2025-01-15-0853")
        self.assertEqual(
            paths.generate_case_name("router", now=now), "2025-01-15-0853-router"
        )
        self.assertEqual(
            paths.generate_case_name("desk1 DNS!", now=now), "2025-01-15-0853-desk1-dns"
        )

    def test_generated_names_are_always_valid_case_names(self):
        now = datetime(2025, 1, 15, 8, 53)
        for keyword in ("", "router", "desk1 DNS!", "!!!", "x" * 200, "../etc"):
            name = paths.generate_case_name(keyword, now=now)
            self.assertTrue(paths.is_valid_case_name(name), name)

    def test_resolve_passes_an_existing_case_name_through(self):
        # This is what makes the second file of a batch land in the first one's
        # folder, and how an incident-dated case is targeted deliberately.
        for name in ("2025-01-15-router", "2025-01-15-0853", "2025-01-15"):
            self.assertEqual(paths.resolve_case_name(name), name)

    def test_resolve_slugifies_an_invalid_name_and_dates_only_a_blank_one(self):
        now = datetime(2025, 1, 15, 8, 53)
        self.assertEqual(paths.resolve_case_name("", now=now), "2025-01-15-0853")
        self.assertEqual(paths.resolve_case_name("   ", now=now), "2025-01-15-0853")
        # idea #319: a name is the case, not a keyword after a timestamp.
        self.assertEqual(paths.resolve_case_name("router", now=now), "router")
        self.assertEqual(paths.resolve_case_name("../etc", now=now), "etc")

    def test_bare_traversal_is_still_not_a_valid_case_name(self):
        for bad in ("../", "..", "/etc", "2025-01-15/../x", "a.b"):
            self.assertFalse(paths.is_valid_case_name(bad), bad)


class PathsTests(TestCase):
    def setUp(self):
        self.log_root = _mk_tmp("logs")
        self.addCleanup(shutil.rmtree, self.log_root, ignore_errors=True)
        self.override = override_settings(LOG_ANALYSIS_ROOT=self.log_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_valid_case_name(self):
        self.assertTrue(paths.is_valid_case_name("2025-01-15-router"))
        self.assertFalse(paths.is_valid_case_name("../../etc"))
        self.assertFalse(paths.is_valid_case_name("/etc/passwd"))
        self.assertTrue(paths.is_valid_case_name("not-a-date"))  # a free name, #319
        self.assertTrue(paths.is_valid_case_name("HomeLab"))
        self.assertFalse(paths.is_valid_case_name("2025-01-15-" + "x" * 54))

    def test_case_dir_rejects_traversal(self):
        from django.core.exceptions import SuspiciousFileOperation

        with self.assertRaises(SuspiciousFileOperation):
            paths.case_dir("../../etc", create=True)
        with self.assertRaises(SuspiciousFileOperation):
            paths.case_dir("/etc/passwd", create=True)

    def test_new_case_gets_logs_and_gitignore(self):
        d = paths.case_dir("2025-01-15-router", create=True)
        self.assertTrue((d / "logs").is_dir())
        self.assertTrue((d / ".gitignore").is_file())

    def test_destination_path_never_overwrites(self):
        d = paths.case_dir("2025-01-15-router", create=True)
        first = paths.destination_path("2025-01-15-router", "host.log", create_case=True)
        first.write_text("one")
        second = paths.destination_path("2025-01-15-router", "host.log", create_case=True)
        self.assertNotEqual(first, second)
        second.write_text("two")
        self.assertEqual(first.read_text(), "one")

    def test_missing_log_analysis_root_is_not_configured(self):
        override = override_settings(LOG_ANALYSIS_ROOT="")
        override.enable()
        try:
            with self.assertRaises(paths.NotConfigured):
                paths.log_analysis_root()
        finally:
            override.disable()


class StateTests(TestCase):
    def setUp(self):
        self.state_root = _mk_tmp("state")
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.override = override_settings(REDACT_STATE_DIR=self.state_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_salt_created_once_and_reused(self):
        p = state.salt_path("2025-01-15-router")
        self.assertFalse(p.exists())
        state.load_or_create_salt("2025-01-15-router")
        self.assertTrue(p.exists())
        self.assertEqual(p.stat().st_size, 32)
        self.assertEqual(oct(p.stat().st_mode)[-3:], "600")
        # Second call must not mint a new salt (checked by byte-length staying
        # constant and mtime not changing — never by reading the bytes).
        mtime = p.stat().st_mtime
        state.load_or_create_salt("2025-01-15-router")
        self.assertEqual(p.stat().st_mtime, mtime)

    def test_case_dir_validates_the_name_itself(self):
        # views._do_redact checks first, but state must not take that on trust.
        for bad in ("../escape", "/etc", "a.b", "2025-01-15-router/../.."):
            with self.assertRaises(SuspiciousFileOperation):
                state.salt_path(bad)

    def test_merge_hosts_never_drops_a_remembered_name(self):
        state.save_hosts("2025-01-15-router", ["desk1", "srv2"])
        # An upload arriving with an empty names box keeps both...
        self.assertEqual(
            state.merge_hosts("2025-01-15-router", []), ["desk1", "srv2"]
        )
        # ...and one carrying a new name adds it rather than replacing.
        self.assertEqual(
            state.merge_hosts("2025-01-15-router", ["nas"]),
            ["desk1", "srv2", "nas"],
        )
        self.assertEqual(
            state.load_hosts("2025-01-15-router"), ["desk1", "srv2", "nas"]
        )

    @skip_if_root
    def test_unwritable_state_dir_is_not_configured_not_a_crash(self):
        """Regression: deployed with REDACT_STATE_DIR unset, state_root() fell back
        to BASE_DIR/.redact-state -- inside the image, on a read-only filesystem --
        and the OSError escaped the view as a 500 traceback
        ("OSError: [Errno 30] Read-only file system: '/app/.redact-state'").
        LOG_ANALYSIS_ROOT already degrades to a panel; this must too."""
        readonly = _mk_tmp("readonly")
        os.chmod(readonly, 0o555)
        self.addCleanup(os.chmod, readonly, 0o755)
        with override_settings(REDACT_STATE_DIR=os.path.join(readonly, "nope")):
            with self.assertRaises(paths.NotConfigured):
                state.state_root()

    def test_hosts_round_trip_without_reading_salt(self):
        state.save_hosts("2025-01-15-router", ["desk1", "srv2"])
        self.assertEqual(state.load_hosts("2025-01-15-router"), ["desk1", "srv2"])
        p = state.hosts_path("2025-01-15-router")
        self.assertEqual(oct(p.stat().st_mode)[-3:], "600")


@override_settings()
class ViewTests(TestCase):
    def setUp(self):
        self.log_root = _mk_tmp("logs")
        self.state_root = _mk_tmp("state")
        self.addCleanup(shutil.rmtree, self.log_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.override = override_settings(
            LOG_ANALYSIS_ROOT=self.log_root, REDACT_STATE_DIR=self.state_root
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = Client()

    def _upload(self, case, filename, body, hosts=""):
        url = f"/redact/upload/?case={case}&hosts={hosts}"
        return self.client.post(
            url,
            data=body,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=filename,
        )

    def test_index_not_configured_panel(self):
        override = override_settings(LOG_ANALYSIS_ROOT="")
        override.enable()
        try:
            resp = self.client.get("/redact/")
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "Not configured")
        finally:
            override.disable()

    def test_upload_writes_redacted_file(self):
        resp = self._upload(
            "2025-01-15-router", "host.log", b"contact person@example.com now\n"
        )
        self.assertEqual(resp.status_code, 200)
        out = self.log_root
        dest = _one_log(out, "2025-01-15-router", "host.log")
        self.assertTrue(dest.is_file())
        content = dest.read_text()
        self.assertNotIn("person@example.com", content)
        stats = dest.with_name(dest.name + ".redaction-stats")
        self.assertTrue(stats.is_file())

    def test_package_scoped_upload_keeps_every_line_and_skips_the_process_filter(self):
        """A package-scoped export (here com.example.notes) once went through here
        and every one of its body lines was dropped because none carried a
        anchor tag -- the process filter is redundant once the
        export is already package-scoped (see redact/logcat.py's PACKAGE_RE)."""
        body = (
            "```\n"
            "type: logcat\n"
            "osVersion: google/oriole/oriole:16/BP2A.250605.031/2025061200:user/release-keys\n"
            "package: com.example.notes:84186, targetSdk 37\n"
            "buffers: main,system,crash,events,kernel\n"
            "level: verbose\n"
            "\n"
            "--------- beginning of main\n"
            "01-15 08:50:48.696  3311 33110 D AndroidRuntime: VM starting up\n"
            "01-15 08:50:49.001  3311 33111 I ActivityManager: Start proc 3311\n"
            "01-15 08:50:50.000  3311 33110 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "\tat com.foo.Bar.baz(Bar.java:12)\n"
        ).encode()
        resp = self._upload("2025-01-15-notes", "notes.log", body)
        self.assertEqual(resp.status_code, 200)
        dest = _one_log(self.log_root, "2025-01-15-notes", "notes.log")
        content = dest.read_text()
        for needle in (
            "VM starting up",
            "Start proc 3311",
            "FATAL EXCEPTION: main",
            "at com.foo.Bar.baz(Bar.java:12)",
        ):
            self.assertIn(needle, content)

        stats = dest.with_name(dest.name + ".redaction-stats").read_text()
        self.assertIn("scoped by logcat to com.example.notes", stats)
        self.assertIn("process filter skipped", stats)

        result = resp.context["result"]
        self.assertEqual(result["dropped"], 0)

    def test_empty_result_is_flagged_with_a_warning(self):
        """A logcat export where no line survives the process filter must be a
        loud failure, not a silent 'leak check: clean' on an empty file."""
        body = (
            "01-15 08:50:42.113  1881  1881 I NotificationService: posted from user@example.com\n"
            "01-15 08:50:44.500  1881  1881 D App     : generic tag, unknown pid\n"
            "01-15 08:50:45.500  1881  1881 D health  : also generic\n"
        ).encode()
        resp = self._upload("2025-01-15-empty", "empty.log", body)
        self.assertEqual(resp.status_code, 200)
        dest = _one_log(self.log_root, "2025-01-15-empty", "empty.log")
        stats = dest.with_name(dest.name + ".redaction-stats").read_text()
        warning_lines = [line for line in stats.splitlines() if line.startswith("WARNING:")]
        self.assertEqual(len(warning_lines), 1)

        result = resp.context["result"]
        self.assertTrue(result["warning"])

    def test_case_name_traversal_cannot_escape_the_root(self):
        """Since the field became a keyword box, a traversal string is no longer a
        400 -- it is slugified into a duller keyword. What must still hold is that
        nothing is written outside LOG_ANALYSIS_ROOT."""
        root = Path(self.log_root).resolve()
        for bad in ("../../etc", "/etc/passwd", "not-a-case"):
            resp = self._upload(bad, "host.log", b"hello\n")
            self.assertEqual(resp.status_code, 200, bad)
            dest = Path(resp.context["result"]["path"]).resolve()
            self.assertTrue(
                dest.is_relative_to(root), f"{bad} escaped to {dest}"
            )
        # ...and every directory created still matches the case convention.
        for d in Path(self.log_root).iterdir():
            self.assertTrue(paths.is_valid_case_name(d.name), d.name)

    def test_archive_magic_rejected_and_no_partial_file_left(self):
        for magic in ARCHIVE_MAGICS:
            body = magic + b"\x00" * 20
            resp = self._upload("2025-01-15-router", "archive.log", body)
            self.assertEqual(resp.status_code, 400)
        logs_dir = Path(self.log_root) / "2025-01-15-router" / "logs"
        if logs_dir.exists():
            self.assertEqual(list(logs_dir.iterdir()), [])

    def test_second_upload_keeps_scrubbing_a_remembered_name(self):
        body = b"Jan 15 15:02:07 desk1 sshd[697]: hello\n"
        self._upload("2025-01-15-router", "first.log", body, hosts="desk1")
        # Same case, empty names box — the name must still be scrubbed, or this
        # file would be less redacted than the previous one with nothing saying so.
        self._upload("2025-01-15-router", "second.log", body)
        second = _one_log(self.log_root, "2025-01-15-router", "second.log")
        text = second.read_text()
        self.assertNotIn("desk1", text)
        self.assertIn("[NAME-", text)

    def test_upload_with_no_case_name_generates_one(self):
        resp = self._upload("", "auto.log", b"peer at 10.20.30.40\n")
        self.assertEqual(resp.status_code, 200)
        cases = [p.name for p in Path(self.log_root).iterdir() if p.is_dir()]
        self.assertEqual(len(cases), 1)
        self.assertTrue(paths.is_valid_case_name(cases[0]), cases[0])
        self.assertRegex(cases[0], r"^\d{4}-\d{2}-\d{2}-\d{4}$")

    def test_upload_with_a_name_uses_it_as_the_case(self):
        # Since idea #319 a name is the case itself, no longer a keyword after
        # a timestamp; the file carries the timestamp instead.
        self._upload("router", "auto.log", b"peer at 10.20.30.40\n")
        cases = [p.name for p in Path(self.log_root).iterdir() if p.is_dir()]
        self.assertEqual(cases, ["router"])
        _one_log(self.log_root, "router", "auto.log")

    def test_a_batch_sharing_one_resolved_name_stays_one_case(self):
        """The reason the browser uploads a batch sequentially: every file after
        the first carries the case the server resolved, so a drop that straddles a
        minute tick cannot split into two cases with two salts."""
        # A genuinely public address, which the redactor always tokenizes
        # (private/Tailscale ranges are kept in the clear since idea #136).
        line = b"peer at 93.184.216.34 right now\n"
        first = self._upload("router", "a.log", line)
        case = first.context["result"]["case"]
        self._upload(case, "b.log", line)
        self._upload(case, "c.log", line)

        cases = [p.name for p in Path(self.log_root).iterdir() if p.is_dir()]
        self.assertEqual(cases, [case])
        logs = Path(self.log_root) / case / "logs"
        tokens = {
            f.read_text().strip().split("peer at ", 1)[1]
            for f in sorted(logs.glob("*.log"))
        }
        self.assertEqual(len(sorted(logs.glob("*.log"))), 3)
        self.assertEqual(len(tokens), 1, "one salt per case means one token")

    @skip_if_root
    def test_unwritable_state_dir_shows_the_panel_instead_of_500ing(self):
        readonly = _mk_tmp("readonly")
        os.chmod(readonly, 0o555)
        self.addCleanup(os.chmod, readonly, 0o755)
        with override_settings(REDACT_STATE_DIR=os.path.join(readonly, "nope")):
            resp = self._upload("router", "a.log", b"peer at 10.20.30.40\n")
        self.assertEqual(resp.status_code, 409)
        self.assertContains(resp, "REDACT_STATE_DIR", status_code=409)

    def test_size_cap_rejected_and_partial_file_removed(self):
        big = (b"x" * 1024 + b"\n") * ((MAX_UPLOAD_BYTES // 1024) + 10)
        resp = self._upload("2025-01-15-router", "big.log", big)
        self.assertEqual(resp.status_code, 400)
        logs = Path(self.log_root) / "2025-01-15-router" / "logs"
        self.assertEqual(list(logs.glob("*big*")) if logs.is_dir() else [], [])

    def test_tokens_correlate_within_a_case_and_differ_across_cases(self):
        # A genuinely public address, which the redactor always tokenizes
        # (TEST-NET ranges are on the keep-list and pass through raw, and
        # private/Tailscale ranges are kept in the clear since idea #136).
        line = b"peer at 93.184.216.34 right now\n"
        self._upload("2025-01-15-router", "a.log", line)
        self._upload("2025-01-15-router", "b.log", line)
        dest_a = _one_log(self.log_root, "2025-01-15-router", "a.log")
        dest_b = _one_log(self.log_root, "2025-01-15-router", "b.log")
        token_a = dest_a.read_text().strip().split("peer at ", 1)[1]
        token_b = dest_b.read_text().strip().split("peer at ", 1)[1]
        self.assertEqual(token_a, token_b)

        self._upload("2025-01-16-other-case", "c.log", line)
        dest_c = _one_log(self.log_root, "2025-01-16-other-case", "c.log")
        token_c = dest_c.read_text().strip().split("peer at ", 1)[1]
        self.assertNotEqual(token_a, token_c)

    def test_no_temp_file_engaged(self):
        # A private temp dir: the shared one changes under the test on CI
        # runners (systemd-private-* dirs come and go).
        tmp_watch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_watch, ignore_errors=True)
        old_tempdir = tempfile.tempdir
        tempfile.tempdir = tmp_watch
        self.addCleanup(setattr, tempfile, "tempdir", old_tempdir)
        # Larger than Django's default FILE_UPLOAD_MAX_MEMORY_SIZE (2.5 MB),
        # which is exactly the size at which a *multipart* upload would spill
        # to a temp file -- this path must never engage that handler at all.
        body = (b"line with 10.20.30.40 in it\n") * 200000
        with override_settings(FILE_UPLOAD_TEMP_DIR=tmp_watch):
            resp = self._upload("2025-01-15-router", "large.log", body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(os.listdir(tmp_watch), [])

    def test_paste_fallback_is_ordinary_post_no_multipart(self):
        # Mirrors the real <form enctype="application/x-www-form-urlencoded">
        # in templates/redact/index.html -- deliberately not the test client's
        # default multipart encoding, since the whole point of this fallback
        # is to never engage the multipart/temp-file path.
        body = urlencode(
            {
                "case": "2025-01-15-router",
                "hosts": "",
                "filename": "pasted",
                "content": "contact person@example.com now\n",
            }
        )
        resp = self.client.post(
            "/redact/paste/",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(resp.status_code, 200)
        dest = _one_log(self.log_root, "2025-01-15-router", "pasted.log")
        self.assertTrue(dest.is_file())
        self.assertNotIn("person@example.com", dest.read_text())


# ---------------------------------------------------------------------------
# Android logcat: filter before scrub (idea #52)
# ---------------------------------------------------------------------------

# Deliberately no `package:` line: this fixture is the whole-device,
# multi-process export (the client's pid plus 1881/1204 below), and a `package:`
# header only appears on a real device when the export was already narrowed to
# one app by logcat's own --uid= filter (see redact/logcat.py's PACKAGE_RE and
# module docstring) -- a shape that by construction never mixes foreign pids
# in. A separate, explicitly package-scoped fixture covers that case.
LOGCAT_HEADER = """```
type: logcat
osVersion: google/oriole/oriole:16/BP2A.250605.031/2025061200:user/release-keys
flags: dev options enabled
buffers: main,system,crash,events,kernel
level: verbose

--------- beginning of main
"""

# Every anchored client app, as its own process in a whole-device export: its
# pid, two tags that anchor it, a generic tag it also logs under, two of its
# own messages, and a Go stack frame for continuation lines. Each filter test
# runs once per app. 1881 and 1204 are other apps in every fixture.
LOGCAT_APPS = {
    "tailscale": {
        "pid": "2967", "tag": "gojni", "tag2": "IPNService", "generic": "App",
        "first": "magicsock: adding connection to derp-19",
        "second": "VPN state: Running",
        "own_generic": "bindSocketToActiveNetwork: binding fd=144 to net=122 iface=wlan0",
        "frame": "tailscale.com/wgengine.(*userspaceEngine).Reconfig()",
        "source": "/build/wgengine/userspace.go:812 +0x1a4",
    },
    "wireguard": {
        "pid": "3311", "tag": "WireGuard/GoBackend/wg0", "tag2": "WireGuard/TunnelManager",
        "generic": "QrCodeFromFileScanner",
        "first": "peer(XyZ1…aBc2) - Handshake did not complete after 5 seconds",
        "second": "Tunnel wg0 state UP",
        "own_generic": "decoding a tunnel config from a picked image",
        "frame": "golang.zx2c4.com/wireguard/device.(*Peer).SendHandshakeInitiation()",
        "source": "/build/device/send.go:212 +0x1a4",
    },
}


def _lc(pid, tag, msg, level="D", ts="08:50:48.696", tid=None):
    """One logcat line in `adb logcat -v threadtime` layout."""
    return f"01-15 {ts} {pid:>5} {tid or pid:>5} {level} {tag:<8}: {msg}\n"


def _logcat_body(app):
    a = LOGCAT_APPS[app]
    return (
        _lc(a["pid"], a["tag"], a["first"], tid="29513")
        + _lc(a["pid"], a["generic"], a["own_generic"], ts="08:50:41.680", tid="29508")
        + _lc("1881", "NotificationService", "posted from user@example.com", "I", "08:50:42.113")
        + _lc("1204", "WifiService", 'connected to SSID "HomeNet" BSSID 3a:9c:11:0e:7b:22',
              ts="08:50:43.002")
        + _lc("1881", "App", "some other app's generic tag", ts="08:50:44.500")
    )


def _panic(app, ts="08:50:48.696"):
    """The client's Go panic: an anchored line, then continuation lines."""
    a = LOGCAT_APPS[app]
    return (_lc(a["pid"], a["tag"], "panic: send on closed channel", "E", ts, "29513")
            + "\tgoroutine 1 [running]:\n")


# The repro. The exporter merges main/system/events onto one clock, so a
# foreign entry's continuation can land directly behind a kept client line --
# with the `switch to` dividers between them, which the filter once treated as
# continuations and therefore let a "keep" fate survive across. Every
# sensitive-looking string is invented (`SSID "Guest-Net"`, the /storage path).
def _interleaved(app):
    a = LOGCAT_APPS[app]
    return (
        _lc(a["pid"], a["tag"], a["first"], tid="29513")
        + "--------- switch to system\n"
        + _lc("1204", "WifiService", 'connected to SSID "Guest-Net"', ts="08:50:49.100")
        + "--------- switch to main\n"
        + _lc(a["pid"], a["tag2"], a["second"], ts="08:50:49.200", tid="29513")
        + "--------- switch to system\n"
        + "\tpath /storage/emulated/0/DCIM/holiday.jpg\n"
    )


JOURNALD_SAMPLE = (
    "2025-01-15T14:58:19+00:00 desk1 sshd[812]: Server listening on port 22.\n"
    "2025-01-15T14:58:20+00:00 desk1 cron[813]: (root) CMD (run-parts /etc/cron.hourly)\n"
    "2025-01-15T14:58:21+00:00 desk1 tailscaled[814]: magicsock: home is derp-10\n"
)


def _mk_redactor():
    return Redactor(salt=b"\x00" * 32)


class LogcatSniffTests(TestCase):
    def test_fenced_export_with_header_is_detected(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                text = LOGCAT_HEADER + _logcat_body(app)
                self.assertTrue(logcat.sniff(text.splitlines(keepends=True)))

    def test_bare_adb_logcat_without_header_is_detected(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                self.assertTrue(logcat.sniff(_logcat_body(app).splitlines(keepends=True)))

    def test_journald_log_is_not_detected(self):
        self.assertFalse(logcat.sniff(JOURNALD_SAMPLE.splitlines(keepends=True)))

    def test_a_single_stray_logcat_shaped_line_is_not_enough(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                lines = JOURNALD_SAMPLE.splitlines(keepends=True)
                lines.append(_lc(a["pid"], a["tag"], "one line"))
                self.assertFalse(logcat.sniff(lines))


class LogcatFilterTests(TestCase):
    def _run(self, text):
        filt = logcat.LogcatFilter(_mk_redactor())
        out = "".join(filt.run(text.splitlines(keepends=True)))
        return out, filt

    def test_markdown_fences_are_stripped(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app) + "```\n")
                self.assertNotIn("```", out)

    def test_header_is_kept_but_the_build_fingerprint_is_tokenised(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertIn("buffers: main,system,crash,events,kernel", out)
                self.assertIn("osVersion:", out)
                self.assertNotIn("BP2A.250605.031", out)
                self.assertNotIn("oriole", out)
                self.assertIn("[ANDROID-", out)

    def test_anchor_tag_keeps_its_line(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertIn(a["first"], out)

    def test_generic_tag_on_the_anchored_pid_is_kept(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertIn(a["own_generic"], out)

    def test_generic_tag_on_another_pid_is_dropped(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertNotIn("some other app's generic tag", out)

    def test_other_processes_are_dropped_entirely(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertNotIn("NotificationService", out)
                self.assertNotIn("user@example.com", out)
                self.assertNotIn("HomeNet", out)
                self.assertNotIn("WifiService", out)

    def test_continuation_lines_follow_the_line_they_belong_to(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                text = LOGCAT_HEADER + _panic(app) + f"\t{a['frame']}\n" + (
                    _lc("1881", "AndroidRuntime", "FATAL EXCEPTION", "E", "08:50:49.000")
                    + "\tat com.other.app.Secret.thing(Secret.java:42)\n"
                )
                out, _ = self._run(text)
                self.assertIn(a["frame"], out)
                self.assertNotIn("com.other.app.Secret", out)

    def test_counts_report_the_subtraction(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                _, filt = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertTrue(filt.detected)
                self.assertEqual(filt.dropped, 3)
                self.assertGreater(filt.kept, 0)

    def test_non_logcat_input_passes_through_untouched(self):
        out, filt = self._run(JOURNALD_SAMPLE)
        self.assertFalse(filt.detected)
        self.assertEqual(out, JOURNALD_SAMPLE)
        self.assertEqual(filt.dropped, 0)

    # -- per-buffer fate ---------------------------------------------------

    def test_a_foreign_continuation_behind_a_kept_line_is_dropped(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _interleaved(app))
                self.assertNotIn("Guest-Net", out)
                self.assertNotIn("/storage/emulated/0", out)
                self.assertIn(a["second"], out)

    def test_both_marker_forms_are_always_emitted(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, _ = self._run(LOGCAT_HEADER + _interleaved(app))
                self.assertEqual(out.count("--------- switch to system"), 2)
                self.assertEqual(out.count("--------- switch to main"), 1)
                self.assertEqual(out.count("--------- beginning of main"), 1)

    def test_evidence_survives_an_excursion_into_another_buffer(self):
        """A Go panic's goroutine dump is exactly what continuations exist to
        carry. An interruption by another buffer must not cost it."""
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                text = LOGCAT_HEADER + _panic(app) + (
                    "--------- switch to events\n"
                    + _lc("1881", "am_proc_start", "another app", "I", "08:50:48.700")
                    + "--------- switch to main\n"
                    + f"\t{a['frame']}\n"
                    + f"\t{a['source']}\n"
                )
                out, _ = self._run(text)
                self.assertIn("goroutine 1 [running]", out)
                self.assertIn(a["source"], out)
                self.assertNotIn("am_proc_start", out)

    def test_a_dump_with_no_markers_behaves_as_before(self):
        """Plain `adb logcat`: one implicit buffer, so a continuation follows the
        line above it exactly as it did before per-buffer fate existed."""
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                text = (
                    _lc(a["pid"], a["tag"], a["first"], ts="08:50:48.500", tid="29513")
                    + _panic(app)
                    + _lc("1881", "AndroidRuntime", "FATAL EXCEPTION", "E", "08:50:49.000")
                    + "\tat com.other.app.Secret.thing(Secret.java:42)\n"
                )
                out, filt = self._run(text)
                self.assertTrue(filt.detected)
                self.assertIn("goroutine 1 [running]", out)
                self.assertNotIn("com.other.app.Secret", out)
                self.assertEqual(filt.continuations_kept, 1)

    def test_continuations_kept_is_counted(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                _, filt = self._run(LOGCAT_HEADER + _interleaved(app))
                self.assertEqual(filt.continuations_kept, 0)

    def test_a_dump_with_no_anchor_keeps_nothing_rather_than_guessing(self):
        """Fail-safe: with no high-confidence tag to learn the pid from, the
        filter costs evidence rather than admitting another app's lines."""
        text = (
            _lc("1881", "NotificationService", "posted from user@example.com", "I", "08:50:42.113")
            + _lc("1881", "App", "generic tag, unknown pid", ts="08:50:44.500")
            + _lc("1881", "health", "also generic", ts="08:50:45.500")
        )
        out, filt = self._run(text)
        self.assertTrue(filt.detected)
        self.assertNotIn("user@example.com", out)
        self.assertNotIn("generic tag", out)

    # -- package-scoped exports ---------------------------
    #
    # A log-viewer export adds a `package: <pkg>:<versionCode>, targetSdk N`
    # header line only when the export was already scoped to that app by
    # logcat's own `--uid=` filter (in GrapheneOS Log Viewer's
    # LogcatActivity.java, the branch that picks the target package passes
    # --uid= and emits this header). The process-subtraction is therefore
    # redundant -- and actively wrong -- for these: none of the lines carry an
    # anchor tag, so subtracting would drop every one of them.

    SCOPED_HEADER = """```
type: logcat
osVersion: google/oriole/oriole:16/BP2A.250605.031/2025061200:user/release-keys
package: com.example.notes:84186, targetSdk 37
buffers: main,system,crash,events,kernel
level: verbose

--------- beginning of main
"""

    SCOPED_BODY = (
        "01-15 08:50:48.696  3311 33110 D AndroidRuntime: VM starting up\n"
        "01-15 08:50:49.001  3311 33111 I ActivityManager: Start proc 3311 for com.example.notes\n"
        "01-15 08:50:49.500  3311 33112 W Process : Sending signal. PID: 3311 SIG: 9\n"
        "01-15 08:50:50.000  3311 33110 E AndroidRuntime: FATAL EXCEPTION: main\n"
        "\tat com.foo.Bar.baz(Bar.java:12)\n"
    )

    def test_package_scoped_export_keeps_every_process(self):
        out, filt = self._run(self.SCOPED_HEADER + self.SCOPED_BODY)
        for needle in (
            "VM starting up",
            "Start proc 3311",
            "Sending signal. PID: 3311",
            "FATAL EXCEPTION: main",
            "at com.foo.Bar.baz(Bar.java:12)",
        ):
            self.assertIn(needle, out)
        self.assertEqual(filt.dropped, 0)
        self.assertEqual(filt.scoped_to, "com.example.notes")

    def test_unscoped_export_still_subtracts(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                out, filt = self._run(LOGCAT_HEADER + _logcat_body(app))
                self.assertIsNone(filt.scoped_to)
                # Non-anchored pids are still dropped.
                self.assertGreater(filt.dropped, 0)
                self.assertNotIn("NotificationService", out)

    def test_empty_result_is_flagged(self):
        text = (
            "01-15 08:50:42.113  1881  1881 I NotificationService: posted from user@example.com\n"
            "01-15 08:50:44.500  1881  1881 D App     : generic tag, unknown pid\n"
            "01-15 08:50:45.500  1881  1881 D health  : also generic\n"
        )
        out, filt = self._run(text)
        self.assertTrue(filt.detected)
        self.assertEqual(filt.kept_body, 0)


class LogcatViewTests(TestCase):
    def setUp(self):
        self.log_root = _mk_tmp("logs")
        self.state_root = _mk_tmp("state")
        self.addCleanup(shutil.rmtree, self.log_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.override = override_settings(
            LOG_ANALYSIS_ROOT=self.log_root, REDACT_STATE_DIR=self.state_root
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = Client()

    def _upload(self, case, filename, text):
        return self.client.post(
            f"/redact/upload/?case={case}",
            data=text.encode(),
            content_type="application/octet-stream",
            HTTP_X_FILENAME=filename,
        )

    def test_upload_filters_before_scrubbing_and_says_so_in_the_stats(self):
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                resp = self._upload(f"android-{app}", "phone.log", LOGCAT_HEADER + _logcat_body(app))
                self.assertEqual(resp.status_code, 200)
                dest = _one_log(self.log_root, f"android-{app}", "phone.log")
                content = dest.read_text()

                # The whole-device lines never reached disk.
                self.assertNotIn("NotificationService", content)
                self.assertNotIn("user@example.com", content)
                self.assertNotIn("HomeNet", content)
                # The client's own lines did, scrubbed.
                self.assertIn(a["first"], content)

                stats = dest.with_name(dest.name + ".redaction-stats").read_text()
                self.assertIn("android-logcat", stats)
                self.assertIn("dropped", stats)

    def test_a_foreign_continuation_never_reaches_disk(self):
        """The whole point of subtracting before scrubbing: the fixture's
        invented SSID and /storage path must not be in the written file."""
        for app, a in LOGCAT_APPS.items():
            with self.subTest(app):
                resp = self._upload(f"interleaved-{app}", "phone.log",
                                    LOGCAT_HEADER + _interleaved(app))
                self.assertEqual(resp.status_code, 200)
                content = _one_log(self.log_root, f"interleaved-{app}", "phone.log").read_text()
                self.assertNotIn("Guest-Net", content)
                self.assertNotIn("/storage/emulated/0", content)
                self.assertIn(a["second"], content)

    def test_kept_continuations_are_reported_in_the_stats(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                resp = self._upload(f"continuations-{app}", "panic.log",
                                    LOGCAT_HEADER + _panic(app))
                self.assertEqual(resp.status_code, 200)
                stats = _one_log(self.log_root, f"continuations-{app}",
                                 "panic.log.redaction-stats").read_text()
                self.assertIn("1 continuation lines kept", stats)

    def test_no_continuations_means_no_such_stats_line(self):
        for app in LOGCAT_APPS:
            with self.subTest(app):
                self._upload(f"plain-{app}", "phone.log", LOGCAT_HEADER + _logcat_body(app))
                stats = _one_log(self.log_root, f"plain-{app}",
                                 "phone.log.redaction-stats").read_text()
                self.assertNotIn("continuation", stats)

    def test_a_journald_upload_is_unaffected_by_the_new_path(self):
        resp = self._upload("journald", "desk1.log", JOURNALD_SAMPLE)
        self.assertEqual(resp.status_code, 200)
        dest = _one_log(self.log_root, "journald", "desk1.log")
        self.assertEqual(len(dest.read_text().splitlines()), 3)


# ---------------------------------------------------------------------------
# Credentials and logins
# ---------------------------------------------------------------------------

def _t(*parts):
    """A credential-shaped fixture, joined at runtime so that no literal token
    sits in the source for a secret scanner to trip over. All values are made up."""
    return "".join(parts)


def _b64key(seed):
    import base64
    return base64.b64encode(bytes((seed + i) % 256 for i in range(32))).decode()


# (kind, line, the value that must not survive)
SECRET_SAMPLES = [
    ("aws", "boto: using AKIAIOSFODNN7EXAMPLE for s3", "AKIAIOSFODNN7EXAMPLE"),
    ("github", "git: auth with " + _t("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8") + " ok",
     _t("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")),
    ("github_pat", "token=" + _t("github", "_pat_", "11ABCDEFG0aBcDeFgHiJkL_", "m" * 40),
     _t("github", "_pat_", "11ABCDEFG0aBcDeFgHiJkL_", "m" * 40)),
    ("gitlab", "runner registered " + _t("gl", "pat-", "aB3dE5fG7hI9jK1lM2nO"),
     _t("gl", "pat-", "aB3dE5fG7hI9jK1lM2nO")),
    ("google", "maps: key=" + _t("AI", "za", "SyA-abcdefghijklmnopqrstuvwxyz01234"),
     _t("AI", "za", "SyA-abcdefghijklmnopqrstuvwxyz01234")),
    ("jwt", "id_token " + _t("eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJ", "zdWIiOiJhbGljZSJ9", ".",
                             "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
     _t("eyJ", "zdWIiOiJhbGljZSJ9")),
    ("matrix", "synapse: token " + _t("syt_", "YWxpY2U_", "AbCdEfGhIjKlMnOpQrSt_0aBcDe"),
     _t("syt_", "YWxpY2U_", "AbCdEfGhIjKlMnOpQrSt_0aBcDe")),
    ("slack", "webhook bot " + _t("xo", "xb-", "1234567890-0987654321-AbCdEfGhIjKlMnOp"),
     _t("xo", "xb-", "1234567890-0987654321-AbCdEfGhIjKlMnOp")),
    ("stripe", "charge failed, key " + _t("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc"),
     _t("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc")),
    # A Tailscale/Headscale key's id part ends in CNTRL, which the stable-id
    # rule would also catch; the random tail is what only the key rule covers.
    ("tailscale_auth", "up: --auth-key=" + _t("tskey-", "auth-", "kTestK7CNTRL-", "abc123DEF456ghi789JKL"),
     "abc123DEF456ghi789JKL"),
    ("tailscale_api", "api: using " + _t("tskey-", "api-", "kTestK8CNTRL-", "mno345PQR678stu901VWX")
     + " for the admin API", "mno345PQR678stu901VWX"),
    ("tailscale_client", "oauth client " + _t("tskey-", "client-", "kTestK9CNTRL-", "yz0123ABC456def789GHI"),
     "yz0123ABC456def789GHI"),
    ("basic_auth", "GET /api HTTP/1.1 Authorization: Basic dXNlcjpwYXNzd29yZA==",
     "dXNlcjpwYXNzd29yZA=="),
    ("bearer_json", '{"headers": {"Authorization": "Bearer abcd1234efgh"}}', "abcd1234efgh"),
    ("password", "db: connecting with password=hunter2hunter2 user=app", "hunter2hunter2"),
    ("password_env", "PASSWORD: s3cr3t-value", "s3cr3t-value"),
]
SECRET_LINES = {kind: line for kind, line, _ in SECRET_SAMPLES}

SSHD_SAMPLE = [
    "sshd[812]: Accepted publickey for alice from 93.184.216.34 port 22 ssh2",
    "sshd[813]: Failed password for invalid user bob from 198.18.7.7 port 2222 ssh2",
    "sshd[814]: Invalid user carol from 198.18.7.7 port 2223",
    "sshd[812]: pam_unix(sshd:session): session opened for user alice(uid=1000) by (uid=0)",
]


class _ScrubMixin:
    def _red(self):
        return Redactor(salt=b"\x07" * 32)

    def _scrub(self, lines, red=None):
        red = red or self._red()
        return "".join(redact_stream([line + "\n" for line in lines], red))


class CredentialTests(_ScrubMixin, TestCase):
    def test_credentials_are_redacted_and_seen_by_the_leak_check(self):
        for kind, line, secret in SECRET_SAMPLES:
            with self.subTest(kind):
                self.assertGreater(sum(leak_check(line).values()), 0)
                out = self._scrub([line])
                self.assertNotIn(secret, out)
                self.assertEqual(sum(leak_check(out).values()), 0, out)

    def test_header_names_and_schemes_stay_readable(self):
        out = self._scrub([SECRET_LINES["basic_auth"], SECRET_LINES["password"]])
        self.assertIn("Authorization: Basic [AUTH-", out)
        self.assertIn("password=[PASSWORD-", out)
        self.assertIn("user=app", out)

    def test_flags_and_masks_are_not_passwords(self):
        line = "config: secret: false, password=(hidden), pwd=****"
        self.assertEqual(self._scrub([line]), line + "\n")
        self.assertEqual(sum(leak_check(line).values()), 0)

    def test_sshd_logins_are_gone(self):
        out = self._scrub(SSHD_SAMPLE)
        for value in ("alice", "bob", "carol"):
            self.assertNotIn(value, out)
        self.assertIn("for invalid user [USER-", out)
        self.assertIn("(uid=1000)", out)
        for line in SSHD_SAMPLE:
            self.assertGreater(leak_check(line)["sshuser"], 0, line)
        self.assertEqual(sum(leak_check(out).values()), 0)


# ---------------------------------------------------------------------------
# VPNs and overlay networks: OpenVPN, Tailscale/Headscale, WireGuard, ZeroTier
# ---------------------------------------------------------------------------

OPENVPN_SAMPLE = [
    "2025-01-15 10:00:00 VERIFY OK: depth=1, CN=Example-CA",
    "2025-01-15 10:00:00 VERIFY OK: depth=0, CN=laptop-alice",
    "2025-01-15 10:00:01 laptop-alice/93.184.216.34:51234 MULTI_sva: pool returned IPv4=10.8.0.6",
    "2025-01-15 10:00:01 93.184.216.34:51234 Peer Connection Initiated with [AF_INET]93.184.216.34:51234",
]

TS_NODEKEY = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab"
TS_DISCOKEY = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef12345678"
TAILSCALE_SAMPLE = [
    f"nodekey:{TS_NODEKEY} node key",
    f"discokey:{TS_DISCOKEY} disco key",
    "magicsock: derp-10 does not know about peer [aBcDe], removing route",
    "metrics: exit_node=nTestB1CNTRL tun_in=1",
    'user bugreport node info: stableid="nTestB1CNTRL" expiry="2027-03-15T08:32:54Z"',
]

WG_PRIV, WG_PUB, WG_PEER, WG_PSK = (_b64key(s) for s in (1, 50, 100, 150))
WIREGUARD_SAMPLE = [
    "interface: wg0",
    f"  public key: {WG_PUB}",
    "  private key: (hidden)",
    "  listening port: 51820",
    f"peer: {WG_PEER}",
    "  preshared key: (hidden)",
    "  endpoint: 93.184.216.34:51820",
    "  allowed ips: 10.8.0.2/32",
    "  latest handshake: 1 minute, 2 seconds ago",
    f"PrivateKey = {WG_PRIV}",
    f"PresharedKey = {WG_PSK}",
    "Endpoint = 198.18.7.7:51820",
]

ZEROTIER_SAMPLE = [
    "200 info 89e92ceee5 1.14.0 ONLINE",
    "200 listnetworks 8056c2e21c000001 home 2a:4b:6c:8d:1e:2f OK PRIVATE ztabcdef12 10.147.17.5/24",
    "200 listpeers 62f865ae71 1.14.0 LEAF 12 DIRECT 93.184.216.34/9993",
    '  "nwid": "8056c2e21c000001",',
    '  "address": "89e92ceee5",',
]


class VpnTests(_ScrubMixin, TestCase):
    """Each VPN's own identity -- keys, node and network ids, certificate
    names -- is scrubbed, and correlates where it shows up twice."""

    # -- OpenVPN -------------------------------------------------------------

    def test_openvpn_common_names_correlate_across_both_shapes(self):
        out = self._scrub(OPENVPN_SAMPLE)
        self.assertNotIn("laptop-alice", out)
        self.assertNotIn("93.184.216.34", out)
        self.assertIn("10.8.0.6", out)
        lines = out.splitlines()
        cn = re.search(r"CN=(\[CN-[0-9a-f]{6}\])", lines[1]).group(1)
        self.assertTrue(lines[2].split(" ")[2].startswith(cn + "/[IP4-"), lines[2])
        self.assertGreater(leak_check(OPENVPN_SAMPLE[2])["ovpnpeer"], 0)
        self.assertGreater(leak_check(OPENVPN_SAMPLE[1])["certcn"], 0)
        self.assertEqual(sum(leak_check(out).values()), 0)

    # -- Tailscale / Headscale -----------------------------------------------

    def test_tailscale_keys_and_node_ids_are_gone(self):
        out = self._scrub(TAILSCALE_SAMPLE)
        for value in (TS_NODEKEY, TS_DISCOKEY, "[aBcDe]", "nTestB1CNTRL"):
            self.assertNotIn(value, out)
        for kept in ("nodekey:[KEY-", "discokey:[KEY-", "peer [SHORTKEY-", "exit_node=[STABLEID-",
                     'stableid="[STABLEID-', "derp-10"):
            self.assertIn(kept, out)
        self.assertEqual(sum(leak_check(out).values()), 0)

    def test_tailnet_and_tailscale_ips_survive_by_default(self):
        # Tailnet names and the CGNAT/ULA ranges are kept by default (idea #136).
        red = Redactor(salt=b"\x01" * 32)
        for line, kept in (("peer is myhost.tail1234ab.ts.net right now", "myhost.tail1234ab.ts.net"),
                           ("v6 compressed fd7a:115c:a1e0::a401:333a seen", "fd7a:115c:a1e0::a401:333a")):
            self.assertIn(kept, "".join(redact_stream([line], red)))

    def test_tailnet_and_tailscale_ips_can_still_be_redacted(self):
        # The opt-in flags must still exercise the underlying mechanism.
        # fd7a:115c:a1e0::/48 also falls inside fc00::/7 (ULA/PRIVATE_NETS),
        # which is checked first in _ip(), so keep_private must be turned off
        # too or the private check would keep it regardless of this flag.
        red = Redactor(
            salt=b"\x01" * 32,
            keep_private=False,
            keep_tailscale_ips=False,
            keep_tailnet_domains=False,
        )
        out = "".join(redact_stream(
            ["peer is myhost.tail1234ab.ts.net right now"], red
        ))
        self.assertNotIn("myhost.tail1234ab.ts.net", out)
        out2 = "".join(redact_stream(
            ["v6 compressed fd7a:115c:a1e0::a401:333a seen"], red
        ))
        self.assertNotIn("fd7a:115c:a1e0::a401:333a", out2)

    def test_multilabel_tailnet_fqdn_survives_by_default(self):
        red = Redactor(salt=b"\x01" * 32)
        out = "".join(redact_stream(
            ["setting up VpnService on phone-b.tail1234ab.ts.net"], red
        ))
        self.assertIn("phone-b.tail1234ab.ts.net", out)

    def test_multilabel_tailnet_fqdn_is_a_single_token_when_redacted(self):
        # The device label before ".ts.net" is part of the one token (idea
        # #116 gap 2), via the opt-in --redact-tailnet flag.
        red = Redactor(salt=b"\x01" * 32, keep_tailnet_domains=False)
        out = "".join(redact_stream(
            ["setting up VpnService on phone-b.tail1234ab.ts.net"], red
        ))
        self.assertNotIn("phone-b", out)
        self.assertNotIn("tail1234ab", out)
        self.assertRegex(out, r"\[TAILNET-[0-9a-f]{6}\]")
        self.assertEqual(out.count("[TAILNET-"), 1)

        # Bare (already single-label) form still works.
        out2 = "".join(redact_stream(["peer is tail1234ab.ts.net right now"], red))
        self.assertNotIn("tail1234ab", out2)
        self.assertIn("[TAILNET-", out2)

        # A trailing dot, as DNS output writes it, still matches the FQDN.
        out3 = "".join(redact_stream(
            ["resolved laptop.tail1234ab.ts.net. via mdns"], red
        ))
        self.assertNotIn("laptop", out3)
        self.assertNotIn("tail1234ab", out3)

    def test_multilabel_tailnet_fqdn_is_not_flagged(self):
        # Kept by default, so not something the leak check flags.
        hits = leak_check("still on phone-b.tail1234ab.ts.net")
        self.assertEqual(sum(hits.values()), 0)

    def test_bugreport_marker_is_scrubbed_and_correlates_with_logid(self):
        # The BUG-<publicLogID>-<timestamp>-<random> marker embeds the node's
        # public log ID outside any of the labels the "logid" rule requires
        # (idea #117). Only the log-ID field is identifying enough to
        # tokenize; the timestamp and trailing random suffix stay visible.
        red = Redactor(salt=b"\x01" * 32)
        bugid = "a1b2c3d4" * 8  # 64 hex chars
        timestamp = "20250115100000"  # 14 digits
        suffix = "0123456789abcdef"  # 16 hex chars

        marker_out = "".join(redact_stream(
            [f"user bugreport: BUG-{bugid}-{timestamp}-{suffix}"], red
        ))
        self.assertNotIn(bugid, marker_out)
        self.assertIn(timestamp, marker_out)
        self.assertIn(suffix, marker_out)
        token = re.search(r"\[LOGID-[0-9a-f]{6}\]", marker_out)
        self.assertIsNotNone(token, marker_out)

        logid_out = "".join(redact_stream([f"LogID: {bugid}"], red))
        self.assertNotIn(bugid, logid_out)
        # Same value, same salt, same "logid" kind -> identical token, which
        # is the correlation the idea asks for.
        self.assertIn(token.group(), logid_out)

    # -- WireGuard -----------------------------------------------------------

    def test_wireguard_keys_and_endpoints_are_gone(self):
        out = self._scrub(WIREGUARD_SAMPLE)
        for value in (WG_PRIV, WG_PUB, WG_PEER, WG_PSK, "93.184.216.34", "198.18.7.7"):
            self.assertNotIn(value, out)
        for kept in ("interface: wg0", "private key: (hidden)", "10.8.0.2/32", ":51820"):
            self.assertIn(kept, out)
        self.assertEqual(sum(leak_check(out).values()), 0)

    # -- ZeroTier ------------------------------------------------------------

    def test_zerotier_ids_are_gone_and_correlate(self):
        out = self._scrub(ZEROTIER_SAMPLE)
        for value in ("89e92ceee5", "8056c2e21c000001", "ztabcdef12", "62f865ae71", "93.184.216.34"):
            self.assertNotIn(value, out)
        self.assertIn("1.14.0", out)
        self.assertIn("10.147.17.5/24", out)
        lines = out.splitlines()
        nwid = re.search(r"\[ZTID-[0-9a-f]{6}\]", lines[1]).group()
        self.assertIn(nwid, lines[3])
        node = re.search(r"\[ZTID-[0-9a-f]{6}\]", lines[0]).group()
        self.assertIn(node, lines[4])
        for line in ZEROTIER_SAMPLE:
            self.assertGreater(leak_check(line)["ztid"], 0, line)
        self.assertEqual(sum(leak_check(out).values()), 0)


# ---------------------------------------------------------------------------
# Chat servers: Matrix (Synapse), Zulip
# ---------------------------------------------------------------------------

MATRIX_SAMPLE = [
    "synapse.access.http.8008 - 172.18.0.1 - 8008 - {@alice:example.org} Processed request: "
    "0.012sec GET /_matrix/client/v3/rooms/!AbCdEfGh123:example.org/messages",
    "synapse.access.http.8008 - 93.184.216.34 - 8008 - {None} Processed request: "
    "0.004sec POST /_matrix/client/v3/login",
    "synapse.handlers.auth - WARNING - Failed password login for user @carol:example.org",
    "synapse.handlers.room_member - INFO - @bob:chat.example.net joined #general:example.org",
    "GET /_matrix/client/v3/profile/%40alice%3Aexample.org/displayname",
    "PUT /_matrix/client/v3/rooms/%21AbCdEfGh123%3Aexample.org/send/m.room.message/txn1",
]
MATRIX_SECRETS = ["alice", "bob", "carol", "AbCdEfGh123", "general", "example.org",
                  "chat.example.net", "93.184.216.34"]


class MatrixTests(_ScrubMixin, TestCase):
    def test_matrix_ids_are_gone_and_correlate_with_their_encoded_form(self):
        out = self._scrub(MATRIX_SAMPLE)
        for value in MATRIX_SECRETS:
            self.assertNotIn(value, out)
        self.assertIn("/_matrix/client/v3/rooms/![ROOM-", out)
        self.assertIn("172.18.0.1", out)
        lines = out.splitlines()
        alice = re.search(r"\{@(\[USER-[0-9a-f]{6}\]):", lines[0]).group(1)
        self.assertIn(f"%40{alice}%3A", lines[4])
        room = re.search(r"!(\[ROOM-[0-9a-f]{6}\]):", lines[0]).group(1)
        self.assertIn(f"%21{room}%3A", lines[5])
        self.assertIn("Failed password login for user @[USER-", lines[2])
        for line in MATRIX_SAMPLE:
            self.assertGreater(sum(leak_check(line).values()), 0, line)
        for line in MATRIX_SAMPLE[:1] + MATRIX_SAMPLE[2:]:
            self.assertGreater(leak_check(line)["mxid"], 0, line)
        self.assertEqual(sum(leak_check(out).values()), 0)

    def test_email_and_decorators_are_not_matrix_ids(self):
        out = self._scrub(["mail from bob@example.org", "tick 12:30:45 and ratio 3:4"])
        self.assertIn("[USER-", out)
        self.assertNotIn("[ROOM-", out)
        self.assertIn("12:30:45", out)


# Zulip server lines, one per rule shape. Shapes from the Zulip source
# (zerver/middleware.py, zproject/backends.py, zerver/lib/push_notifications.py,
# zerver/lib/send_email.py) and Postgres' error reporting (idea #488); every
# value is synthetic.
Z_IP = "93.184.216.34"
Z_REALM = "acme-corp"
Z_FCM = ("dXyZ1a2B3c4:APA91bHq9xYzAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
         "_-AbCdEfGhIjKlMnOpQrStUv")
Z_APNS = "a3f1" * 16
Z_UUID = "6f1c2b9e-7d4a-4e1b-9c3d-2a5b8e0f1d47"
Z_UPLOAD = "x7Kq2LmN9pRsT4vW/quarterly-report.pdf"
Z_SECRETS = [
    Z_IP, Z_REALM, Z_FCM, Z_APNS, Z_UUID, "x7Kq2LmN9pRsT4vW", "quarterly-report",
    "alice@example.com", "Alice Example", "Doe, John", "jdoe",
    "Zr8kQ2wYp4LmN7vX1cBt5HsD9fGj3KaE", "Gf4hJ6kL8mN0pQ2rS4tU6vW8xY0zA2bC",
    "kq3rjv5wnxe2ybzo7d4hufsa", "hello Bob, the door code is 2222", "Bobs Garage", "top-secret",
]
_ZT = "2025-01-15 20:01:"
ZULIP_MSGS = [
    f"{_ZT}02.345 INFO [zr] {Z_IP}   POST    200   8ms (db: 2ms/3q) "
    f"/json/messages [send_message] (8@{Z_REALM} via website)",
    f"{_ZT}03.000 INFO [zr] ::ffff:{Z_IP} GET     200  12ms "
    f"/user_uploads/2/{Z_UPLOAD} (8@{Z_REALM} via ZulipMobile/27.235)",
    f"{_ZT}04.000 INFO [zr] {Z_IP}   GET     302   3ms "
    f"/user_uploads/temporary/kq3rjv5wnxe2ybzo7d4hufsa/report.pdf (unauth@{Z_REALM} via website)",
    f"{_ZT}05.000 INFO [zr] {Z_IP}   GET     200   3ms "
    "/join/kq3rjv5wnxe2ybzo7d4hufsa/ (unauth@root via website)",
    f"{_ZT}06.001 INFO status=400, data=b'{{\"msg\":\"x\"}}', uid=8@{Z_REALM}",
    f"{_ZT}07.000 INFO [zulip.auth.email] Authentication attempt from {Z_IP}: "
    f"subdomain={Z_REALM};username=alice@example.com;outcome=failed;return_data={{}}",
    f"{_ZT}07.500 INFO [zulip.auth.ldap] Authentication attempt from {Z_IP}: "
    "subdomain=;username=jdoe;outcome=success;return_data={}",
    f"{_ZT}08.000 INFO [zerver.lib.push_notifications] FCM: Sent message "
    f"with ID: projects/zulip/messages/0:1700000000%abc to {Z_FCM}",
    f"{_ZT}08.200 INFO [zerver.lib.push_notifications] APNs: Success sending "
    f"for user <id:8> to device {Z_APNS}",
    f"{_ZT}08.300 INFO [zerver.lib.push_notifications] APNs: Removing "
    f"invalid/expired token {Z_APNS} (Unregistered)",
    f"{_ZT}08.400 INFO Registering push device: 8 '{Z_APNS}' 2 'org.zulip.Zulip'",
    f"{_ZT}08.500 INFO Sending new push device to bouncer: "
    f"{{'server_uuid': '{Z_UUID}', 'user_uuid': '{Z_UUID}', 'realm_uuid': '{Z_UUID}', "
    f"'user_id': '8', 'token': '{Z_FCM}', 'token_kind': 2}}",
    f"{_ZT}09.000 INFO [zulip.send_email] Sending password_reset email to "
    f"['Alice Example <alice@example.com>'] in {Z_REALM} (triggered from {Z_IP})",
    f"{_ZT}09.100 INFO [zulip.send_email] Sending followup_day1 email to "
    f"['\"Doe, John\" <alice@example.com>'] in {Z_REALM}",
    f"{_ZT}10.000 INFO [zr] {Z_IP}   POST    200   3ms "
    "/api/v1/external/github?api_key=Zr8kQ2wYp4LmN7vX1cBt5HsD9fGj3KaE&stream=dev "
    "(9@acme-corp via ZulipGitHubWebhook)",
    f"{_ZT}10.200 INFO [zr] {Z_IP}   POST    200   3ms "
    "/api/v1/external/grafana?api_key=Gf4hJ6kL8mN0pQ2rS4tU6vW8xY0zA2bC&stream=alerts "
    "(9@acme-corp via ZulipGrafanaWebhook)",
    f"{_ZT}10.500 INFO [zr] {Z_IP}   GET     200   3ms "
    "/accounts/send_confirm/?email=alice%40example.com (unauth@root via website)",
    f"{_ZT}11.000 UTC [123] zulip@zulip DETAIL:  Key (string_id)=({Z_REALM}) already exists.",
    f"{_ZT}11.000 UTC [123] zulip@zulip STATEMENT:  INSERT INTO "
    "\"zerver_message\" (\"sender_id\", \"content\", \"subject\") VALUES (8, "
    "'hello Bob, the door code is 2222', 'Bobs Garage')",
    f"{_ZT}12.000 UTC [124] zulip@zulip DETAIL:  Failing row contains (5, top-secret, null).",
]


class ZulipTests(_ScrubMixin, TestCase):
    """A Zulip server's lines come out with nothing identifying left, and the
    leak check sees every Zulip-specific shape on the raw lines."""

    def _red(self):
        return Redactor(salt=b"\x05" * 32)

    def _scrubbed(self):
        return [self._red().scrub(m) for m in ZULIP_MSGS]

    def _line(self, marker):
        """The scrubbed line whose raw form contains `marker`."""
        red = self._red()
        return red.scrub(next(m for m in ZULIP_MSGS if marker in m))

    def test_every_secret_is_gone(self):
        out = self._scrub(ZULIP_MSGS)
        self.assertEqual(len(out.splitlines()), len(ZULIP_MSGS))
        for secret in Z_SECRETS:
            self.assertNotIn(secret, out)

    def test_each_kind_leaves_its_token(self):
        joined = "\n".join(self._scrubbed())
        for token in ("[IP4-", "[IP6-", "[REALM-", "[USER-", "[DOMAIN-", "[PUSHTOKEN-",
                      "[UUID-", "[NAME-", "[UPLOAD-", "[CAPPATH-", "[OAUTH-", "[SQL-"):
            self.assertIn(token, joined, token)
        # What stays readable: the paths' fixed parts, the user id, root.
        self.assertIn("/user_uploads/2/[UPLOAD-", self._line("/user_uploads/2/"))
        self.assertIn("/user_uploads/temporary/[UPLOAD-", self._line("/user_uploads/temporary/"))
        join = self._line("/join/")
        self.assertIn("/join/[CAPPATH-", join)
        self.assertIn("(unauth@root via website)", join)
        self.assertRegex(self._line("/json/messages"), r"\(8@\[REALM-[0-9a-f]{6}\] via website\)")
        self.assertRegex(self._line("status=400"), r"uid=8@\[REALM-[0-9a-f]{6}\]$")
        self.assertIn("subdomain=;username=[USER-", self._line("zulip.auth.ldap"))
        self.assertIn("subdomain=[REALM-", self._line("zulip.auth.email"))
        self.assertIn("'user_id': '8'", self._line("bouncer"))
        statement = self._line("STATEMENT:")
        self.assertIn("VALUES (8, '[SQL-", statement)
        self.assertIn("zulip@zulip STATEMENT:", statement)
        for client in ("ZulipGitHubWebhook", "ZulipGrafanaWebhook"):
            self.assertIn(f"(9@[REALM-", self._line(client))
            self.assertIn("api_key", self._line(client))

    def test_tokens_correlate_across_sightings(self):
        out = "\n".join(self._scrubbed())
        # The FCM token correlates across its sightings, the realm across
        # requester, subdomain=, "in <realm>" and the Postgres key.
        fcm = set(re.findall(r"\[PUSHTOKEN-[0-9a-f]{6}\]", out))
        self.assertEqual(len(fcm), 2)  # one FCM token, one APNs token
        self.assertEqual(len(set(re.findall(r"\[REALM-[0-9a-f]{6}\]", out))), 1)

    def test_leak_check_flags_every_zulip_kind_on_the_raw_lines(self):
        hits = Counter()
        for line in ZULIP_MSGS:
            hits.update(leak_check(line))
        for kind in ("zrequester", "zusername", "pushtoken", "uuid", "dispname",
                     "uploadpath", "cappath", "oauth", "sql", "email", "ipv4"):
            self.assertGreater(hits[kind], 0, kind)

    def test_leak_check_is_clean_on_the_scrubbed_lines(self):
        for line in self._scrubbed():
            self.assertEqual(sum(leak_check(line).values()), 0, line)

    def test_unterminated_sql_literal_is_scrubbed_to_the_end(self):
        # A multi-line statement: the first line still opens the literal.
        out = self._scrub(["UTC [1] STATEMENT:  INSERT INTO x VALUES ('first line of a secret"])
        self.assertNotIn("secret", out)
        self.assertIn("'[SQL-", out)

    def test_the_trigger_gate_never_changes_a_result(self):
        # Zulip rules are left out of the pattern for a line without one of
        # ZULIP_TRIGGERS; each rule's shape must carry one of them.
        lines = ZULIP_MSGS + ["sync: username=jdoe;outcome=failed"]
        for line in lines:
            full = self._red().pattern.sub(self._red()._replace, line)
            self.assertEqual(self._red()._sub(line), full, line)
            self.assertNotIn("jdoe", self._red().scrub(line), line)

    def test_java_identity_hash_is_not_a_requester(self):
        # A logcat line: Foo$1@3f2a1b is a Java identity hash, not 1@<realm>.
        line = "D WindowOnBackDispatcher: callback androidx.Foo$1@3f2a1b9 set"
        self.assertEqual(self._red().scrub(line), line)
        self.assertEqual(sum(leak_check(line).values()), 0)

    def test_realm_equal_to_the_user_id_keeps_the_id(self):
        out = self._red().scrub("x (8@8 via website)")
        self.assertRegex(out, r"^x \(8@\[REALM-[0-9a-f]{6}\] via website\)$")


# ---------------------------------------------------------------------------
# JSON-lines exports: Docker json-file, journald, Loki, VictoriaLogs
# ---------------------------------------------------------------------------

def _json_lines(*rows):
    return [json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows]


# How each export wraps one message: (export, key of the message, row builder).
JSON_FORMATS = [
    ("docker", "log", lambda msg, i: {
        "log": msg + "\n", "stream": "stderr", "time": f"2025-01-15T10:00:{i % 60:02d}.000000000Z"}),
    ("journald", "MESSAGE", lambda msg, i: {
        "__CURSOR": f"s=4f2a;i={i:x};b=0123456789abcdef0123456789abcdef;m=1;t=2;x=3",
        "_MACHINE_ID": "fedcba9876543210fedcba9876543210",
        "_BOOT_ID": "0123456789abcdef0123456789abcdef", "_HOSTNAME": "host1",
        "SYSLOG_IDENTIFIER": "chat", "MESSAGE": msg}),
    ("loki", "line", lambda msg, i: {
        "labels": {"app": "chat", "host": "host1"}, "line": msg,
        "timestamp": f"2025-01-15T10:00:{i % 60:02d}Z"}),
    ("victorialogs", "_msg", lambda msg, i: {
        "_msg": msg, "_stream": '{app="chat",host="host1"}',
        "_stream_id": "00000000000000001234567890abcdef1234567890abcdef",
        "_time": f"2025-01-15T10:00:{i % 60:02d}.123456789Z", "app": "chat", "host": "host1"}),
]

CHAT_MSGS = MATRIX_SAMPLE + ZULIP_MSGS
CHAT_SECRETS = MATRIX_SECRETS + Z_SECRETS
# Ordinary service lines in the same export, which must stay readable.
SERVICE_NOISE = [
    "2025-01-15 20:01:13.000 [info] <0.1234.0> accepting AMQP connection "
    "<0.1234.0> (172.18.0.5:41234 -> 172.18.0.3:5672)",
    "1:M 15 Jan 2025 20:01:14.000 * Background saving terminated with success",
    "magicsock: home is now derp-10 (sea), 93.184.216.34:41641 via 192.168.1.5",
]


def _export(build, msgs, escape_html=False):
    """One JSON line per message. `escape_html` mimics a Go encoder that
    writes < > ' as \\u003c etc."""
    out = []
    for i, msg in enumerate(msgs):
        line = json.dumps(build(msg, i), ensure_ascii=False, separators=(",", ":"))
        if escape_html:
            line = (line.replace("<", "\\u003c").replace(">", "\\u003e")
                    .replace("'", "\\u0027"))
        out.append(line + "\n")
    return out


class JsonLinesTests(TestCase):
    """Docker json-file, `journalctl -o json`, Loki and VictoriaLogs rows are
    decoded and scrubbed value by value, and stay valid JSON."""

    ROWS = [
        {"log": "connection from 93.184.216.34 for alice@example.org\n", "stream": "stderr",
         "time": "2025-01-15T10:00:00.000000000Z"},
        {"labels": {"app": "chat", "host": "host1"},
         "line": "{@alice:example.org} Processed request from 93.184.216.34",
         "timestamp": "2025-01-15T10:00:01Z"},
        {"__CURSOR": "s=4f2a;i=1a2b;b=0123456789abcdef0123456789abcdef;m=1;t=2;x=3",
         "_MACHINE_ID": "fedcba9876543210fedcba9876543210",
         "_BOOT_ID": "0123456789abcdef0123456789abcdef", "_HOSTNAME": "desk1",
         "SYSLOG_IDENTIFIER": "sshd",
         "MESSAGE": "Accepted publickey for alice from 93.184.216.34 port 22"},
        {"_msg": "login by alice@example.org from 93.184.216.34",
         "_stream": '{app="chat",host="host1"}', "_time": "2025-01-15T10:00:02.123456789Z"},
    ]

    def _red(self):
        return Redactor(salt=b"\x08" * 32)

    def test_every_row_stays_json_and_loses_its_identifiers(self):
        out = list(redact_stream(_json_lines(*self.ROWS), self._red()))
        self.assertEqual(len(out), len(self.ROWS))
        for line in out:
            json.loads(line)
            for value in ("93.184.216.34", "alice", "@alice:",
                          "fedcba9876543210", "0123456789abcdef0123456789abcdef"):
                self.assertNotIn(value, line)
        docker, loki, journal, vlogs = (json.loads(line) for line in out)
        self.assertEqual(docker["stream"], "stderr")
        self.assertTrue(docker["log"].endswith("\n"))
        self.assertEqual(loki["labels"], {"app": "chat", "host": "host1"})
        self.assertEqual(journal["_HOSTNAME"], "desk1")
        self.assertRegex(journal["_MACHINE_ID"], r"^\[MACHINEID-[0-9a-f]{6}\]$")
        self.assertRegex(journal["__CURSOR"], r"^\[CURSOR-[0-9a-f]{6}\]$")
        self.assertEqual(vlogs["_stream"], '{app="chat",host="host1"}')

    def test_leak_check_sees_the_raw_rows_and_not_the_scrubbed_ones(self):
        raw = _json_lines(*self.ROWS)
        hits = Counter()
        for line in raw:
            hits.update(leak_check(line))
        for kind in ("ipv4", "email", "mxid", "jsonid"):
            self.assertGreater(hits[kind], 0, kind)
        for line in redact_stream(raw, self._red()):
            self.assertEqual(sum(leak_check(line).values()), 0, line)

    def test_every_chat_message_in_every_export_stays_json_and_loses_its_secrets(self):
        for export, key, build in JSON_FORMATS:
            for escape_html in (False, True):
                with self.subTest(export=export, escape_html=escape_html):
                    out = list(redact_stream(_export(build, CHAT_MSGS, escape_html), self._red()))
                    self.assertEqual(len(out), len(CHAT_MSGS))
                    for raw, line in zip(CHAT_MSGS, out):
                        row = json.loads(line)  # raises on a broken line
                        self.assertTrue(line.endswith("\n"))
                        for secret in CHAT_SECRETS:
                            self.assertNotIn(secret, row[key], raw)
                            self.assertNotIn(secret, line, raw)

    def test_same_value_same_token_in_json_and_plain_text(self):
        red = self._red()
        plain_out = "".join(redact_stream([m + "\n" for m in CHAT_MSGS], red))
        for export, _, build in JSON_FORMATS:
            with self.subTest(export):
                json_out = "".join(redact_stream(_export(build, CHAT_MSGS, escape_html=True), red))
                for kind in ("REALM", "PUSHTOKEN", "IP4", "NAME", "USER", "ROOM"):
                    plain = set(re.findall(rf"\[{kind}-[0-9a-f]{{6}}\]", plain_out))
                    self.assertTrue(plain, kind)
                    self.assertEqual(
                        plain, set(re.findall(rf"\[{kind}-[0-9a-f]{{6}}\]", json_out)), kind
                    )

    def test_leak_check_flags_the_raw_rows_and_clears_the_scrubbed_ones(self):
        for export, _, build in JSON_FORMATS:
            with self.subTest(export):
                hits = Counter()
                for line in _export(build, CHAT_MSGS):
                    hits.update(leak_check(line))
                for kind in ("zrequester", "zusername", "pushtoken", "uuid", "dispname",
                             "uploadpath", "cappath", "oauth", "sql", "email", "ipv4", "mxid"):
                    self.assertGreater(hits[kind], 0, kind)
                for escape_html in (False, True):
                    rows = _export(build, CHAT_MSGS, escape_html)
                    for line in redact_stream(rows, self._red()):
                        self.assertEqual(sum(leak_check(line).values()), 0, line)

    def test_service_noise_stays_readable(self):
        for export, key, build in JSON_FORMATS:
            with self.subTest(export):
                out = [json.loads(line)[key]
                       for line in redact_stream(_export(build, SERVICE_NOISE), self._red())]
                self.assertIn("172.18.0.5:41234", out[0])
                self.assertIn("Background saving terminated with success", out[1])
                self.assertIn("derp-10 (sea)", out[2])
                self.assertNotIn("93.184.216.34", out[2])

    def test_a_line_separator_inside_a_row_does_not_split_it(self):
        for export, key, build in JSON_FORMATS:
            with self.subTest(export):
                line = json.dumps(build(f"from {Z_IP} and on", 0), ensure_ascii=False) + "\n"
                out = list(redact_stream([line], self._red()))
                self.assertEqual(len(out), 1)
                self.assertNotIn(Z_IP, json.loads(out[0])[key])

    def test_logtail_json_is_left_to_the_text_rules(self):
        # A logtail row has none of the message keys: it is not decoded and
        # re-encoded.
        line = json.dumps({"text": "contact person@example.com", "v": 1}) + "\n"
        out = "".join(redact_stream([line], self._red()))
        self.assertIn('"text": "contact [USER-', out)


class RuleGateTests(TestCase):
    def test_the_rule_gates_never_change_a_result(self):
        lines = ([line for _, line, _ in SECRET_SAMPLES] + SSHD_SAMPLE
                 + OPENVPN_SAMPLE + TAILSCALE_SAMPLE + WIREGUARD_SAMPLE + ZEROTIER_SAMPLE
                 + MATRIX_SAMPLE + ZULIP_MSGS
                 + [shape.replace("{login}", login) for login in LOGINS for shape in LOGIN_SHAPES]
                 + [f"dns: resolver {url} ready" for _, url, _ in DOH_PROVIDERS])
        red = Redactor(salt=b"\x07" * 32)
        for line in lines:
            self.assertEqual(red._sub(line), red.pattern.sub(red._replace, line), line)


BUILD_LOG_HEAD = [
    "#1 [internal] load build definition from Dockerfile",
    "#5 [stage-0 2/6] RUN apt-get update",
    "#5 3.1 Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]",
    "#5 3.2 Get:2 http://deb.debian.org/debian bookworm/main amd64 curl amd64 7.88 [315 kB]",
]


class BuildProfileTests(_ScrubMixin, TestCase):
    def _build(self, lines):
        return self._scrub(lines, Redactor(salt=b"\x07" * 32, profile="build"))

    def test_public_package_sources_stay_readable_only_in_the_build_profile(self):
        urls = [
            "http://de.archive.ubuntu.com/ubuntu/pool/main/c/curl_7.deb",
            "https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/APKINDEX.tar.gz",
            "https://pypi.org/simple/requests/",
            "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
            "https://registry-1.docker.io/v2/library/python/manifests/3.12",
            "https://proxy.golang.org/example.com/mod/@v/list",
            "https://box1.tail1234ab.ts.net/api/v1/health",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self._build([f"Get {url}"]), f"Get {url}\n")
                self.assertIn("[URL-", self._scrub([f"Get {url}"]))

    def test_query_string_is_still_dropped_on_a_kept_host(self):
        out = self._build(["GET https://pypi.org/simple/x/?token=abc123 ok"])
        self.assertNotIn("abc123", out)
        self.assertIn("https://pypi.org/simple/x/", out)

    def test_other_hosts_and_credentials_are_still_tokenised(self):
        for url in (
            "https://git.example.net/alice/repo.git",
            "https://registry.example.net/v2/app/manifests/latest",
            "https://alice:s3cret@pypi.org/simple/",
            "https://pypi.org.example.net/simple/",
            "https://notgithub.com/x",
        ):
            with self.subTest(url=url):
                out = self._build([f"fetch {url}"])
                self.assertIn("[URL-", out)
                self.assertNotIn("example.net", out)
                self.assertNotIn("s3cret", out)

    def test_image_users_stay_but_the_operators_home_does_not(self):
        out = self._build(["WORKDIR /home/node/app", "cd /home/alice/projects"])
        self.assertIn("/home/node/app", out)
        self.assertNotIn("alice", out)
        self.assertIn("[USER-", out)
        self.assertIn("[USER-", self._scrub(["WORKDIR /home/node/app"]))

    def test_webpack_chunk_tags_are_not_peer_keys_in_the_build_profile(self):
        line = "./app/utils.js 6.74 KiB {1} [aBcDe]"
        self.assertIn("[aBcDe]", self._build([line]))
        self.assertIn("[SHORTKEY-", self._scrub([line]))

    def test_a_public_address_in_a_build_line_is_still_redacted(self):
        out = self._build(["#5 4.0 curl: connect to 93.184.216.34 failed"])
        self.assertNotIn("93.184.216.34", out)
        self.assertIn("[IP4-", out)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            Redactor(salt=b"x", profile="nope")

    def test_package_versions_are_not_addresses_for_the_leak_check(self):
        for line in (
            "Get:193 http://x/ubuntu noble erlang-base amd64 1:25.3.2.8+dfsg-1ubuntu4.8 [10.2 MB]",
            "Get:56 imagemagick-6-common all 8:6.9.12.98+dfsg1-5.2build2 [69.5 kB]",
            "apt install pkg=4:26.2.6.3-0ubuntu0.1",
            "(1/2) Installing libfoo (7.1.2.34-r0)",
            "libbar 2.4.6.1~deb12u1",
        ):
            with self.subTest(line=line):
                self.assertEqual(leak_check(line).get("ipv4", 0), 0)

    def test_the_version_exemption_does_not_hide_a_real_address(self):
        for line in (
            "peer 93.184.216.34 refused",
            "from 93.184.216.34:443 closed",
            "addr=93.184.216.34, retrying",
            "listening on 0.0.0.0 and 93.184.216.34 (public)",
        ):
            with self.subTest(line=line):
                self.assertGreater(leak_check(line).get("ipv4", 0), 0)

    def test_detector_needs_several_build_markers(self):
        self.assertTrue(looks_like_build_log("\n".join(BUILD_LOG_HEAD)))
        self.assertFalse(looks_like_build_log("tailscaled: Get:1 https://x is mentioned once"))
        self.assertFalse(looks_like_build_log("\n".join(["router: link up"] * 50)))


class BuildProfileViewTests(TestCase):
    def setUp(self):
        self.log_root = _mk_tmp("logs")
        self.state_root = _mk_tmp("state")
        self.addCleanup(shutil.rmtree, self.log_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        override = override_settings(
            LOG_ANALYSIS_ROOT=self.log_root, REDACT_STATE_DIR=self.state_root
        )
        override.enable()
        self.addCleanup(override.disable)
        self.client = Client()

    def _run(self, case, body, profile=None):
        url = f"/redact/upload/?case={case}"
        if profile is not None:
            url += f"&profile={profile}"
        resp = self.client.post(url, data=body, content_type="application/octet-stream",
                                HTTP_X_FILENAME="b.log")
        return resp

    def _out(self, case):
        return _one_log(self.log_root, case, "b.log").read_text()

    def _body(self):
        return ("\n".join(BUILD_LOG_HEAD) + "\n#9 WORKDIR /home/node/app\n"
                "#9 fetch https://git.example.net/alice/x.git\n").encode()

    def test_auto_detects_a_build_log_from_its_first_lines(self):
        self.assertEqual(self._run("proj", self._body()).status_code, 200)
        out = self._out("proj")
        self.assertIn("http://deb.debian.org/debian", out)
        self.assertIn("/home/node/app", out)
        self.assertNotIn("example.net", out)
        stats = _one_log(self.log_root, "proj", "b.log")
        self.assertIn("profile: build", stats.with_name(stats.name + ".redaction-stats").read_text())

    def test_auto_leaves_an_ordinary_log_on_the_default_profile(self):
        self._run("plain", b"fetch http://deb.debian.org/debian once\n")
        self.assertNotIn("deb.debian.org", self._out("plain"))

    def test_explicit_profile_overrides_detection(self):
        self._run("forced", b"fetch http://deb.debian.org/debian once\n", profile="build")
        self.assertIn("deb.debian.org", self._out("forced"))
        self._run("off", self._body(), profile="default")
        self.assertNotIn("deb.debian.org", self._out("off"))

    def test_configured_case_always_uses_the_build_profile(self):
        with override_settings(REDACT_BUILD_CASES=frozenset({"nightly"})):
            self._run("nightly", b"fetch http://deb.debian.org/debian once\n")
        self.assertIn("deb.debian.org", self._out("nightly"))

    def test_unknown_profile_is_a_400(self):
        self.assertEqual(self._run("x", b"a\n", profile="bogus").status_code, 400)
