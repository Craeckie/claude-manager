"""Fill a local dev instance with demo projects, ideas in every state, and screenshots.

For looking at the UI — every card state on one page, including the ones that are
awkward to reach by hand (a verify idea with attached images, a plan long enough to
clamp, a closed idea that only appears under ?closed=1).

Never run this against the live instance; `core.management.local_only` enforces that.
"""

import mimetypes
import struct
import zlib
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.management.local_only import refuse_outside_local_dev
from core.models import Artifact, Event, Idea, Project, Screenshot

# ---------------------------------------------------------------------------
# Fake screenshots
#
# Generated rather than committed as binary fixtures: a handful of PNGs in the repo
# would be dead weight, and what the card needs from them is only that thumbnails are
# distinguishable at a glance. Each one is drawn to read as a small window -- a
# coloured title bar, a few grey "text" lines, one highlighted panel -- so the strip
# looks like screenshots instead of colour swatches.
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 480, 300

ACCENTS = {
    "blue": (52, 120, 190),
    "green": (58, 150, 96),
    "red": (196, 74, 66),
    "purple": (124, 82, 168),
    "amber": (198, 148, 44),
}


def _fill(buf, x0, y0, x1, y1, rgb):
    r, g, b = rgb
    for y in range(max(y0, 0), min(y1, HEIGHT)):
        row = y * WIDTH * 3
        for x in range(max(x0, 0), min(x1, WIDTH)):
            i = row + x * 3
            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b


def _chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def fake_screenshot_png(accent="blue", lines=5, panel=True):
    """A small PNG that reads as a window: title bar, text lines, optional panel."""
    rgb = ACCENTS[accent]
    buf = bytearray(b"\xf6" * (WIDTH * HEIGHT * 3))

    _fill(buf, 0, 0, WIDTH, 38, rgb)
    _fill(buf, 14, 14, 26, 26, (255, 255, 255))
    _fill(buf, 34, 16, 150, 24, (255, 255, 255))

    for n in range(lines):
        top = 62 + n * 26
        # Ragged line lengths, so the block reads as text rather than as a bar chart.
        width = 380 - (n * 47) % 210
        _fill(buf, 24, top, 24 + width, top + 11, (188, 190, 194))

    if panel:
        _fill(buf, 24, 62 + lines * 26 + 12, WIDTH - 24, HEIGHT - 24, tuple(min(c + 60, 245) for c in rgb))
        _fill(buf, 40, 62 + lines * 26 + 30, 200, 62 + lines * 26 + 44, (255, 255, 255))

    raw = b"".join(
        b"\x00" + bytes(buf[y * WIDTH * 3 : (y + 1) * WIDTH * 3]) for y in range(HEIGHT)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# The data
#
# Folders are all "demo-" prefixed: this command owns exactly those projects and
# replaces them on every run, so re-seeding never touches anything else in the dev
# database (your own projects, say).
# ---------------------------------------------------------------------------

LONG_PLAN = "\n".join(
    [
        "1. Measure the current card at three widths before changing anything.",
        "2. Pull the day/night split out of `DailyCard` into its own component.",
        "3. Give the summary row its own grid so the icon stops shifting.",
        "4. Move the temperature range under the icon on narrow screens.",
        "5. Decide what the card shows when the forecast is stale.",
        "6. Write the empty state: no data, and no error either.",
        "7. Re-check contrast on the amber and red warning tints.",
        "8. Try it with fourteen days of data, not three.",
        "9. Check the tab order once the grid changes.",
        "10. Make sure the card still collapses on a 320px screen.",
        "11. Replace the hard-coded spacing with the scale tokens.",
        "12. Drop the old `.card--compact` variant if nothing uses it.",
        "13. Screenshot every state for the report.",
        "14. Run the widget tests; they cover the collapsed layout.",
        "15. Check the dark theme, which is where the tints usually break.",
        "16. Ask whether the warning band should be dismissible.",
        "17. Update the design notes in the README.",
        "18. Leave the old card behind a flag for one release.",
        "19. Remove the flag once a week has passed without complaints.",
    ]
)

LONG_REPORT = "\n".join(
    [
        "- The dusk threshold was in local time, not UTC, so it moved with the device.",
        "- Sunrise and sunset come from the API; there is no fallback if they are absent.",
        "- The icon set has no separate dusk variant, so dusk reuses the night icon.",
        "- Fixing the threshold changed two golden-file tests; both were wrong before.",
        "- The same helper now backs the hourly strip, which had drifted by a minute.",
        "- Nothing was done about the polar-circle case; see the open question below.",
        "",
        "The comparison below was taken at 19:40 local time in Karlsruhe. The left half",
        "is the old behaviour, with the day icon still up a good half hour after sunset;",
        "the right half is the corrected threshold. Both were captured against a frozen",
        "clock, so the screenshots can be regenerated without waiting for dusk.",
        "",
        "Timezone handling now goes through one helper rather than three call sites that",
        "each did their own arithmetic. Two of those three were already subtly wrong: one",
        "compared a UTC instant against a local wall time, and the other rounded the",
        "sunset minute down, which put the boundary a minute early every day.",
        "",
        "One open question, deliberately left alone: the API omits sunset entirely above",
        "the polar circle in summer, and the current code falls back to the day icon in",
        "that case. That is probably the right answer for a midnight sun, but nobody has",
        "looked at it with real data from a station that far north, and it is guesswork",
        "until somebody does.",
    ]
)

DEMO_DATA = [
    {
        "project": {
            "name": "Demo: Weather",
            "folder": "demo-weather",
            "status": Project.Status.IMPLEMENTED,
            "description": "A demo project — a small native weather app, used to show every card state.",
        },
        "ideas": [
            {
                "title": "Remember the last location",
                "kind": Idea.Kind.OTHER,
                "status": Idea.Status.NEW,
                "goal": "Open on whatever place was last looked at, instead of the default city.",
            },
            {
                "title": "Crash when the forecast comes back empty",
                "kind": Idea.Kind.FIX,
                "status": Idea.Status.NEW,
                "goal": "Stop the app dying when the API returns an empty days array.",
                "context": (
                    "Happens for a handful of stations at around 02:00 UTC, when the "
                    "provider rebuilds its model and briefly serves an empty payload."
                ),
                "notes": (
                    "The stack trace points at `DailyList.first()`. An empty list is a "
                    "legitimate answer, not an error — show the last cached forecast "
                    "with a stale marker rather than an error screen."
                ),
            },
            {
                "title": "Hourly forecast strip",
                "kind": Idea.Kind.FEATURE,
                "status": Idea.Status.PLANNED,
                "goal": "Show the next twelve hours above the daily cards.",
                "plan": (
                    "1. Add the hourly endpoint to the client.\n"
                    "2. Cache it beside the daily forecast, same expiry.\n"
                    "3. Lay the strip out as a horizontally scrolling row.\n"
                    "4. Hide it entirely when the API has no hourly data."
                ),
            },
            {
                "title": "Rework the daily cards",
                "kind": Idea.Kind.DESIGN,
                "status": Idea.Status.PLANNED,
                "needs_verification": True,
                "goal": "The cards are cramped, and the icon jumps as the temperature width changes.",
                "plan": LONG_PLAN,
            },
            {
                "title": "Offline cache for the last forecast",
                "kind": Idea.Kind.FEATURE,
                "status": Idea.Status.VERIFY,
                "goal": "Show the last forecast when the phone is offline, clearly marked as stale.",
                "plan": (
                    "1. Write the forecast to disk on every successful fetch.\n"
                    "2. Read it back when the network call fails.\n"
                    "3. Show the age of the data in a banner."
                ),
                "report": (
                    "- The banner says how old the data is, not just that it is offline; "
                    "an hour-old forecast and a three-day-old one are not the same thing.\n"
                    "- Cache lives in the app's own files directory, so it is dropped "
                    "with the app and never needs a permission.\n\n"
                    "Nothing is written until a fetch actually succeeds, so a first run "
                    "with no network still shows the ordinary empty state."
                ),
                "screenshots": [
                    ("Offline banner with the cache age", "amber", 4),
                    ("The same view back online", "blue", 5),
                    ("First run, no cache yet", "green", 3),
                ],
                "artifacts": [
                    (
                        "spec.md",
                        "# Offline cache\n\nWrite the forecast to disk on every "
                        "successful fetch; read it back when the network call "
                        "fails.\n",
                        "Design spec",
                    ),
                    (
                        "fetch.log",
                        "12:00:01 fetch ok, cached\n"
                        "12:15:03 fetch failed, serving cache (age 15m)\n"
                        "12:30:02 fetch ok, cached\n",
                        "A sample fetch log",
                    ),
                ],
            },
            {
                "title": "Wrong icon at dusk",
                "kind": Idea.Kind.FIX,
                "status": Idea.Status.VERIFY,
                "goal": "The day icon stays up well after sunset.",
                "report": LONG_REPORT,
                "screenshots": [("Before and after, 19:40 local", "purple", 6)],
            },
            {
                "title": "Widget refresh interval",
                "kind": Idea.Kind.FEATURE,
                "status": Idea.Status.IMPLEMENTED,
                "goal": "Let the home-screen widget refresh on a schedule the user picks.",
                "report": (
                    "- Fifteen minutes is the floor; the OS coalesces anything shorter anyway.\n"
                    "- The setting is per widget, not global, since people pin more than one."
                ),
                "screenshots": [("The interval picker", "green", 4)],
            },
            {
                "title": "Should this support tablets?",
                "kind": Idea.Kind.QUESTION,
                "status": Idea.Status.ABANDONED,
                "goal": "Decide whether a two-pane tablet layout is worth building.",
                "notes": "Dropped for now: nobody asked, and the phone layout stretches acceptably.",
            },
        ],
    },
    {
        "project": {
            "name": "Demo: Recipe box",
            "folder": "demo-recipes",
            "status": Project.Status.DEFINED,
            "description": "A demo project — a recipe keeper, still mostly unbuilt.",
        },
        "ideas": [
            {
                "title": "Import a recipe from a URL",
                "kind": Idea.Kind.QUESTION,
                "status": Idea.Status.NEW,
                "goal": "Work out whether scraping recipe pages is worth the maintenance.",
                "notes": "Most sites ship schema.org Recipe JSON-LD, which would cover the common case.",
            },
            {
                "title": "Duplicate ingredients on save",
                "kind": Idea.Kind.FIX,
                "status": Idea.Status.PLANNED,
                "goal": "Saving an edited recipe twice doubles its ingredient rows.",
                "plan": (
                    "1. Reproduce with a test that saves the same form twice.\n"
                    "2. Key the formset on ingredient id rather than position.\n"
                    "3. Add a uniqueness constraint so it cannot come back."
                ),
            },
        ],
    },
    {
        "project": {
            "name": "Demo: Empty backlog",
            "folder": "demo-empty",
            "status": Project.Status.IDEA,
            "description": "A demo project with no ideas at all, to show the empty card.",
        },
        "ideas": [],
    },
]

DEMO_FOLDERS = [entry["project"]["folder"] for entry in DEMO_DATA]


class Command(BaseCommand):
    help = (
        "Seed the local dev instance with demo projects, ideas in every lifecycle "
        "state, and generated screenshots. Refuses to run on the live instance."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete every project and idea first, not just the demo ones. Asks for "
                "confirmation unless --noinput is given."
            ),
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for confirmation when --reset would delete other records.",
        )

    def handle(self, *args, **options):
        refuse_outside_local_dev("seed_demo")

        if options["reset"] and not self._confirm_reset(options["interactive"]):
            self.stdout.write("Nothing was changed.")
            return

        with transaction.atomic():
            if options["reset"]:
                self._delete(Project.objects.all())
            else:
                # Only what this command owns, so a re-run leaves the rest of the dev
                # database (anything made by hand) alone.
                self._delete(Project.objects.filter(folder__in=DEMO_FOLDERS))
            projects, ideas, shots, files = self._create()
            events = self._create_events()

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {projects} demo projects, {ideas} ideas, {shots} screenshots, "
                f"{files} artifacts, {events} activity events."
            )
        )
        self.stdout.write(
            "Closed ideas (implemented, abandoned) only show up under "
            "http://localhost:8000/?closed=1"
        )

    def _confirm_reset(self, interactive):
        others = Project.objects.exclude(folder__in=DEMO_FOLDERS).count()
        if not interactive or not others:
            return True
        self.stdout.write(
            self.style.WARNING(
                f"--reset will delete {others} project(s) that this command did not "
                f"create, and every idea and screenshot attached to them."
            )
        )
        return input("Type 'yes' to continue: ").strip().lower() == "yes"

    def _delete(self, projects):
        # The rows go with the cascade; the stored files do not, and would pile up in
        # MEDIA_ROOT on every re-seed.
        for screenshot in Screenshot.objects.filter(idea__project__in=projects):
            screenshot.image.delete(save=False)
        for artifact in Artifact.objects.filter(idea__project__in=projects):
            artifact.file.delete(save=False)
        Event.objects.filter(project_id__in=projects.values("pk")).delete()
        projects.delete()

    def _create(self):
        ideas = shots = files = 0
        for entry in DEMO_DATA:
            project = Project.objects.create(**entry["project"])
            for spec in entry["ideas"]:
                spec = dict(spec)
                screenshots = spec.pop("screenshots", [])
                artifacts = spec.pop("artifacts", [])
                # Status is set straight on the model on purpose: core/transitions.py
                # governs what the UI and the API may do to a record, not what a
                # fixture may create.
                idea = Idea.objects.create(project=project, **spec)
                ideas += 1
                for caption, accent, lines in screenshots:
                    Screenshot.objects.create(
                        idea=idea,
                        caption=caption,
                        image=ContentFile(
                            fake_screenshot_png(accent=accent, lines=lines),
                            name=f"{accent}-{lines}.png",
                        ),
                    )
                    shots += 1
                for filename, content, caption in artifacts:
                    encoded = content.encode()
                    Artifact.objects.create(
                        idea=idea,
                        caption=caption,
                        size=len(encoded),
                        content_type=mimetypes.guess_type(filename)[0] or "text/plain",
                        file=ContentFile(encoded, name=filename),
                    )
                    files += 1
        return len(DEMO_DATA), ideas, shots, files

    def _create_events(self):
        """A short activity history on the demo ideas, so /activity/ shows grouping:
        an agent's plan-then-transition folds into one entry, a user edit in between
        splits another. The command itself runs with no source, so nothing above was
        recorded; these are written directly, back-dated."""
        ideas = Idea.objects.filter(project__folder__in=DEMO_FOLDERS)
        by_status = {}
        for idea in ideas:
            by_status.setdefault(idea.status, idea)
        now = timezone.now()
        S, A = Event.Source, Event.Action
        script = [
            # (status of the demo idea, minutes ago, source, action, fields, from, to, detail)
            ("new", 26 * 60, S.MCP, A.IDEA_CREATED, [], "", "new", ""),
            ("implemented", 22 * 60, S.WEB, A.IDEA_CHANGED, [], "verify", "implemented", ""),
            ("verify", 48, S.API, A.IDEA_CHANGED, ["report", "commits"], "", "", ""),
            ("verify", 47, S.API, A.SCREENSHOT_ADDED, [], "", "", "blue-5.png"),
            ("verify", 47, S.API, A.SCREENSHOT_ADDED, [], "", "", "green-4.png"),
            ("verify", 46, S.MCP, A.IDEA_CHANGED, [], "planned", "verify", ""),
            ("new", 20, S.WEB, A.IDEA_CHANGED, ["goal"], "", "", ""),
            ("planned", 6, S.MCP, A.IDEA_CHANGED, ["plan"], "", "", ""),
            ("planned", 5, S.MCP, A.IDEA_CHANGED, [], "new", "planned", ""),
        ]
        events = [
            Event(
                created_at=now - timedelta(minutes=ago),
                source=source,
                action=action,
                idea_id=by_status[status].pk,
                idea_title=by_status[status].title,
                project_id=by_status[status].project_id,
                project_name=by_status[status].project.name,
                fields=fields,
                from_status=from_status,
                to_status=to_status,
                detail=detail,
            )
            for status, ago, source, action, fields, from_status, to_status, detail in script
            if status in by_status
        ]
        Event.objects.bulk_create(events)
        return len(events)
