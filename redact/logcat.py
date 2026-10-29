"""Android logcat: subtraction before scrubbing.

An Android VPN client's log is not its desktop daemon's log. The Tailscale and
WireGuard apps both log their Go backend into logcat (Tailscale through
`libtailscale/log.go` as `gojni`, WireGuard as `WireGuard/GoBackend/<iface>`),
and the export people actually hand over is a markdown-fenced dump of *every*
buffer at verbose level:

    ```
    type: logcat
    osVersion: google/oriole/oriole:16/BP2A.250605.031/2025061200:user/release-keys
    buffers: main,system,crash,events,kernel
    level: verbose

    --------- beginning of main
    01-15 08:50:48.696  2967 29513 D gojni   : magicsock: adding connection ...

`buffers: main,system,...` at `level: verbose` is the whole device: notification
text, account emails, Wi-Fi SSIDs and BSSIDs, `/storage/emulated/0/...`
filenames, location fixes. The redactor's RULES were written against daemon
output and catch none of that, so this module does not ask them to. It keeps the
VPN client's own lines (see `ANCHOR_APPS`) and drops the rest *before* anything
is written, which is the right order for any wide capture (a `journalctl`
window, too): the wider net is cut down first, and dropped text never becomes a
file.

That subtraction is for an **unscoped** export -- the whole-device dump above,
with no `package:` header, where every process's lines are interleaved and the
client's process has to be picked out by pid. A `package: <pkg>:<versionCode>,
targetSdk N` header line means the export was already narrowed to one app. The
fenced header format is written by on-device log-viewer apps; in the one whose
source was checked (GrapheneOS's Log Viewer, `LogcatActivity.java`), that header
is written only when a target package was picked, and picking a target package
is the same branch that passes `--uid=<pkg's uid>` to logcat itself. logcat has
therefore already done the subtraction; doing it again here on that one app's
own tags (`AndroidRuntime`, `ActivityManager`, `Process`, ...) would drop the
export, since none of those are anchor tags. That is exactly what happened to a
package-scoped export once: every body line was dropped, the output was header
plus bare dividers, and the stats still reported "leak check: clean" -- a green
result on an empty file, with the raw upload never written and the evidence
gone. A package-scoped export now skips the process filter entirely and keeps
every line; see `PACKAGE_RE` and `LogcatFilter.scoped_to`.

One thing about that stream is easy to miss and was a leak: the export merges
`main`, `system`, `events`, `crash` and `kernel` onto one clock, so the line
physically above a continuation line is not necessarily its parent. Fate is
therefore inherited **per buffer**, keyed on the `--------- switch to <buffer>`
dividers logcat itself emits, and never per position in the merged stream.

Everything here is a single streaming pass. The input must never be buffered
into a file, and only a bounded head (SNIFF_LINES) is ever held in memory.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator

__all__ = ["LogcatFilter", "sniff", "SNIFF_LINES"]

# `01-15 08:50:48.696  2967 29513 D gojni   : message`
# The tag is non-greedy up to the separating ": " because a real tag can contain
# a space ("Main Activity" is one of the client's own).
LOGCAT_LINE_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEF])\s+"
    r"(?P<tag>.*?)\s*: "
)

FENCE_RE = re.compile(r"^\s*```")
# logcat's own dividers, always present because the exporter passes
# --dividers: "--------- beginning of main", "--------- switch to system".
# The buffer name is captured because fate is inherited per buffer, not per
# position in the merged stream (see _body_line).
BUFFER_MARKER_RE = re.compile(r"^-{3,} (?:beginning of|switch to) (?P<buffer>\S+)")
OS_VERSION_RE = re.compile(r"^(?P<key>osVersion:\s*)(?P<value>.+?)\s*$")
# `package: com.example.notes:84186, targetSdk 36`. A log-viewer export
# writes this header line only when it was already scoped to one target
# package (checked in GrapheneOS Log Viewer's LogcatActivity.java) -- the same branch
# that passes `--uid=` to logcat for that package. When it is present, the
# process-subtraction below is redundant: logcat has already done it, and
# doing it again on this app's own unrelated tags (AndroidRuntime,
# ActivityManager, Process...) would drop the entire export, since none of
# them are ANCHOR_TAGS.
PACKAGE_RE = re.compile(r"^package:\s*(?P<pkg>[\w.]+):\d+")

# How many lines sniff() may look at. Bounded so a misdetect cannot cost memory.
SNIFF_LINES = 50
# How many logcat-shaped lines a header-less dump needs before it counts as one.
_MIN_SHAPED_LINES = 3

# Per client app: tags that identify its process with enough confidence to
# *learn* its pid from (exact names, and prefixes for tags built at runtime),
# and tags it also logs under that are ordinary words any app may use. Those
# generic ones are kept only once the pid is already known -- anchoring on them
# would risk adopting another app's pid, and adopting a pid is how somebody
# else's notifications would end up in a case folder.
ANCHOR_APPS = {
    # The client's own TAG constants (android/src/main/java) plus `gojni`, the
    # gomobile Go runtime tag the backend logs under.
    "tailscale": {
        "tags": frozenset({
            "gojni",
            "AppSourceChecker",
            "AppViewModel",
            "DeepLinkNavigator",
            "InlineShareConsumer",
            "IntegrationLoginWorker",
            "IPNService",
            "LocalAPIRequest",
            "NetworkChangeCallback",
            "PendingTaildropViewModel",
            "SubnetRoutingViewModel",
            "TaildropNotifier",
            "TaildropView",
            "UninitializedApp",
            "VpnViewModel",
        }),
        "prefixes": (),
        "generic": frozenset({"App", "health", "Main Activity", "SharedViews", "TimeUtil"}),
    },
    # Every TAG in wireguard-android is "WireGuard/<Class>", and libwg-go logs
    # as "WireGuard/GoBackend/<interface name>", so the prefix covers both.
    "wireguard": {
        "tags": frozenset(),
        "prefixes": ("WireGuard/",),
        "generic": frozenset({"QrCodeFromFileScanner"}),
    },
}

ANCHOR_TAGS = frozenset().union(*(app["tags"] for app in ANCHOR_APPS.values()))
ANCHOR_TAG_PREFIXES = tuple(p for app in ANCHOR_APPS.values() for p in app["prefixes"])
GENERIC_TAGS = frozenset().union(*(app["generic"] for app in ANCHOR_APPS.values()))


def is_anchor_tag(tag: str) -> bool:
    return tag in ANCHOR_TAGS or tag.startswith(ANCHOR_TAG_PREFIXES)


def sniff(lines: Iterable[str]) -> bool:
    """Does this look like an Android logcat dump?

    Two independent signals, because both shapes turn up: the `type: logcat`
    header an export tool writes, and the bare line shape of `adb logcat` output
    with no header at all. A single stray match is not enough -- a journald log
    quoting one Android line must not flip the whole file onto this path.
    """
    shaped = 0
    for line in lines:
        if FENCE_RE.match(line):
            continue
        if line.strip().lower() == "type: logcat":
            return True
        if LOGCAT_LINE_RE.match(line):
            shaped += 1
            if shaped >= _MIN_SHAPED_LINES:
                return True
    return False


class LogcatFilter:
    """Keeps the VPN client's process lines and drops the rest -- unless the
    export is already package-scoped (a `package: <pkg>:<versionCode>` header),
    in which case logcat's own `--uid=` already did the subtraction and this
    filter keeps every line instead of guessing wrong from an unrelated tag.

    Usage is one instance per upload:

        filt = LogcatFilter(redactor)
        for line in filt.run(lines):
            ...
        filt.detected, filt.kept, filt.kept_body, filt.dropped, filt.scoped_to

    `detected` is False for anything that is not logcat, and `run` is then the
    identity function -- a journald upload takes exactly the path it took before
    this module existed.
    """

    def __init__(self, redactor) -> None:
        self.redactor = redactor
        self.detected = False
        self.kept = 0
        # Body lines kept -- parsed logcat lines and kept continuations, never
        # dividers or header lines. Distinct from `kept` so a nothing-survived
        # result can be told apart from a header-and-dividers-only file, whose
        # `kept` is not zero even though there is nothing to analyse.
        self.kept_body = 0
        self.dropped = 0
        self.continuations_kept = 0
        # Set from the header's `package:` line when the export was already
        # scoped to one app by logcat itself. None for an unscoped, whole-device
        # export, where the pid-based subtraction below still applies.
        self.scoped_to: str | None = None
        self._pids: set[str] = set()
        # Fate of the last parsed line *in each buffer*, for continuations. The
        # export merges main/system/events/crash/kernel onto one clock, so the
        # line above a continuation is only its parent within the same buffer.
        self._keeping: dict[str | None, bool] = {}
        self._buffer: str | None = None

    # -- public ------------------------------------------------------------

    def run(self, lines: Iterable[str]) -> Iterator[str]:
        it = iter(lines)
        head = []
        for line in it:
            head.append(line)
            if len(head) >= SNIFF_LINES:
                break

        self.detected = sniff(head)
        if not self.detected:
            yield from head
            yield from it
            return

        in_header = True
        for line in _chain(head, it):
            if FENCE_RE.match(line):
                continue  # markdown packaging, not log content
            if in_header and not (LOGCAT_LINE_RE.match(line) or BUFFER_MARKER_RE.match(line)):
                pkg_match = PACKAGE_RE.match(line)
                if pkg_match:
                    self.scoped_to = pkg_match.group("pkg")
                self.kept += 1
                yield self._header_line(line)
                continue
            in_header = False
            out = self._body_line(line)
            if out is None:
                self.dropped += 1
            else:
                self.kept += 1
                yield out

    # -- internals ---------------------------------------------------------

    def _header_line(self, line: str) -> str:
        """The metadata block. Only osVersion is touched: it is a build
        fingerprint (device model plus the exact build), which identifies the
        handset far more sharply than the Android version anyone actually wants
        from it. Tokenised through the case's own salt, so it correlates across
        that case's files like every other placeholder."""
        m = OS_VERSION_RE.match(line)
        if not m:
            return line
        token = self.redactor.token("android", m.group("value"))
        return f"{m.group('key')}{token}\n"

    def _body_line(self, line: str) -> str | None:
        """Returns the line to write, or None to drop it."""
        marker = BUFFER_MARKER_RE.match(line)
        if marker:
            # Structure, no content: always emitted, never inheriting a fate.
            # It also says which buffer the following lines came from, which is
            # what makes per-buffer inheritance possible.
            self._buffer = marker.group("buffer")
            return line

        m = LOGCAT_LINE_RE.match(line)
        if not m:
            # A continuation: the wrapped remainder of a long message, a Go
            # panic's goroutine dump, a Java stack trace. It belongs to the entry
            # above it *in its own buffer* -- logcat emits an entry and its
            # embedded-newline lines as one unit, but the exporter interleaves
            # several buffers onto one clock, so the line physically above may
            # belong to another process entirely. Inheriting per buffer is what
            # keeps a Go panic's dump across an excursion into `events` while
            # still dropping somebody else's stack trace that lands behind a kept
            # client line. A dump with no dividers has one implicit buffer
            # (key None) and behaves exactly as it did before.
            if self._keeping.get(self._buffer, False):
                self.continuations_kept += 1
                self.kept_body += 1
                return line
            return None

        pid, tag = m.group("pid"), m.group("tag")
        if self.scoped_to is not None:
            # logcat already ran --uid=<this package> to produce this export --
            # the package: header only exists on that branch of the producer.
            # Subtracting by pid/tag again would drop the app's own unrelated
            # tags (AndroidRuntime, ActivityManager, Process, ...) and lose the
            # whole export, as happened to a real package-scoped upload.
            # Still recorded in self._keeping so a continuation
            # line inherits consistently.
            keeping = True
        elif is_anchor_tag(tag):
            self._pids.add(pid)
            keeping = True
        elif pid in self._pids and tag in GENERIC_TAGS:
            keeping = True
        elif pid in self._pids:
            # Same process, some other tag -- a library, a crash handler. The pid
            # was anchored, so this is the client's own output.
            keeping = True
        else:
            keeping = False
        self._keeping[self._buffer] = keeping
        if keeping:
            self.kept_body += 1
        return line if keeping else None


def _chain(first: list[str], rest: Iterator[str]) -> Iterator[str]:
    yield from first
    yield from rest
