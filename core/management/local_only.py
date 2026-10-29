"""A guard for management commands that write demo or fixture data.

The two instances of this service run the same code and look alike, and only one of
them holds real records (see CLAUDE.md, "Two instances"). A seeding command pointed at
the live one does not fail — it quietly overwrites or deletes the user's actual
projects and ideas. So those commands refuse to run anywhere that looks like it, and
the check lives here rather than in each command so a new seeder cannot forget it.

Picking the signal took two false starts, both worth recording:

- `DEBUG` says nothing. docker-compose.yml leaves the production.py mount commented
  out, so the live container runs the dev defaults and `DEBUG` is True in both places.
- `/.dockerenv` says nothing either, tempting as it looks: this project is *developed*
  inside a container as well, so the marker is present on the dev side too.

What does separate them is that the live instance runs from the image: `WORKDIR /app`,
and `.dockerignore` excludes `.git/`, so the image has no repository metadata while
every source checkout does — including a linked worktree, where `.git` is a file rather
than a directory, hence a plain existence test.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError

IMAGE_WORKDIR = Path("/app")


def local_dev_refusals():
    """Reasons this process must not be seeded, most conclusive first. Empty = local."""
    base_dir = Path(settings.BASE_DIR)
    reasons = []
    if base_dir == IMAGE_WORKDIR:
        reasons.append(f"BASE_DIR is {IMAGE_WORKDIR}, the image's working directory")
    if not (base_dir / ".git").exists():
        reasons.append(f"{base_dir} is not a git checkout, so it looks like the image")
    if (base_dir / "claude_manager" / "production.py").exists():
        reasons.append("a production.py overlay is present")
    engine = settings.DATABASES["default"]["ENGINE"]
    if "sqlite" not in engine:
        reasons.append(f"the database is {engine}, not the dev SQLite file")
    return reasons


def refuse_outside_local_dev(command_name):
    reasons = local_dev_refusals()
    if not reasons:
        return
    raise CommandError(
        f"{command_name} writes fixture data and only runs on a local dev instance. "
        f"Refusing here: {'; '.join(reasons)}. "
        "The live instance holds the real projects and ideas — "
        "change those through the API or the MCP tools, never with a seeder."
    )
