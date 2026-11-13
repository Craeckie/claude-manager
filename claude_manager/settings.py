"""
Django settings for claude_manager.

Development values are set here. Production overrides live in production.py
(gitignored; see production.py.example).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# A git-ignored .env next to manage.py holds this instance's own values (domains,
# paths). Plain KEY=VALUE lines; a variable already set in the environment wins.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _key, _sep, _value = _line.partition("=")
        _key = _key.strip()
        if _sep and _key and not _key.startswith("#"):
            os.environ.setdefault(_key, _value.strip().strip("'\""))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-change-this-before-deploying-to-production"

# SECURITY WARNING: don't run with debug turned on in production!
# os.environ values are strings, so DEBUG=False would otherwise be the truthy
# string "False" — parse it instead of taking it at face value.
DEBUG = os.environ.get('DEBUG', 'True').strip().lower() not in ('false', '0', 'no', '')

# DOMAIN is a comma-separated list, because an instance is often reached under more
# than one name at once: e.g. an HTTPS host name from a browser, and the Docker
# service name from other containers on the same network. Blank entries are
# dropped so a trailing comma or "a, b" spacing is harmless.
DOMAINS = [
    host.strip()
    for host in os.environ.get('DOMAIN', '').split(',')
    if host.strip()
]

# Dev/QA: run the dev server on 0.0.0.0:8000 (reachable off-box). The Host header
# is checked even in DEBUG, so list every name used to reach it.
# localhost/127.0.0.1 are listed too so the same dev server answers a local browser
# or curl; DEBUG alone does not make Django trust them.
ALLOWED_HOSTS = [*DOMAINS, "localhost", "127.0.0.1"]

# A host reached over HTTPS (a TLS proxy in front of plain-HTTP runserver/uvicorn).
# Django's CSRF origin check only auto-trusts the request's detected scheme (http),
# so the https origin must be trusted explicitly or POSTs fail with "Origin checking
# failed … does not match any trusted origins". Plain-http names (the container
# name) need no entry; listing them costs nothing and keeps this one line.
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in DOMAINS]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    # Must precede django.contrib.staticfiles: it disables runserver's own static
    # handler so WhiteNoise serves /static/ in dev too (see WHITENOISE_* below).
    "whitenoise.runserver_nostatic",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "redact",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serve static files efficiently
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.activity.ActivitySourceMiddleware",  # tags model writes with web/api/mcp/admin
]

ROOT_URLCONF = "claude_manager.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "claude_manager.wsgi.application"
ASGI_APPLICATION = "claude_manager.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get('DB_PATH', BASE_DIR / "db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# The activity feed's clock and day headings; storage stays UTC.
DISPLAY_TIME_ZONE = os.environ.get("DISPLAY_TIME_ZONE", "UTC")

# --- workspace -------------------------------------------------------------------
# Every project is a folder directly under CM_WORKSPACE_ROOT; an idea's prompt tells
# the agent to cd there and to read CM_WORKSPACE_ROOT/CLAUDE.md first.
WORKSPACE_ROOT = os.environ.get("CM_WORKSPACE_ROOT", "/workspace").rstrip("/")

# Commit links on an idea: a project whose origin is on GitHub links there; any other
# links to f"{CM_GIT_WEB_BASE}/{folder}/commit/{hash}", or not at all when unset.
GIT_WEB_BASE = os.environ.get("CM_GIT_WEB_BASE", "").rstrip("/")

# How agents reach this server (named in MCP tool texts, e.g. for large uploads).
BASE_URL = os.environ.get("CM_BASE_URL", "http://localhost:8000").rstrip("/")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "static_collected"
STATICFILES_DIRS = [BASE_DIR / "static"]

# WhiteNoise has its own built-in media-type map (not the stdlib mimetypes module)
# and does not know ".webmanifest" by default, serving it as application/octet-stream.
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}

# Dev static caching. runserver's built-in static handler wraps the WSGI app
# *outside* the middleware chain and answers with a bare Last-Modified and no
# Cache-Control, so browsers apply heuristic freshness (~10% of the file's age)
# and serve an edited style.css from disk cache for hours without revalidating.
# Letting WhiteNoise serve dev static instead fixes that: MAX_AGE=0 emits an
# explicit "max-age=0, public", turning every hit into a conditional request.
# USE_FINDERS serves straight from STATICFILES_DIRS (no collectstatic needed) and
# AUTOREFRESH re-reads files that changed on disk.
if DEBUG:
    WHITENOISE_USE_FINDERS = True
    WHITENOISE_AUTOREFRESH = True
    WHITENOISE_MAX_AGE = 0

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- redact app --------------------------------------------------------------
# LOG_ANALYSIS_ROOT: the directory holding one folder per case, where a case's
# logs/ lives. Unset or missing -> the /redact/ screen renders "not configured" and
# refuses uploads rather than 500ing (see redact/paths.py NotConfigured).
LOG_ANALYSIS_ROOT = os.environ.get("LOG_ANALYSIS_ROOT", "")

# REDACT_BUILD_CASES: comma-separated case names whose logs always use the
# redactor's "build" profile (package-manager / image-build runs). Other logs
# are detected from their first lines; see redact/log_redact.py.
REDACT_BUILD_CASES = frozenset(
    c.strip().lower() for c in os.environ.get("REDACT_BUILD_CASES", "").split(",") if c.strip()
)

# REDACT_STATE_DIR: per-case salt + hosts list, deliberately a *different* tree
# from LOG_ANALYSIS_ROOT so neither ever lands inside a case's logs/. Gitignored
# in dev; a named Docker volume in production (see docker-compose.yml).
REDACT_STATE_DIR = os.environ.get("REDACT_STATE_DIR", str(BASE_DIR / ".redact-state"))

# A screenshot reaches MCP as base64 inside the JSON body, and Django rejects a
# non-multipart body over DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB by default) before any
# view runs -- the caller would get an HTML 400 page instead of the isError result it
# can read and correct itself from. Base64 adds about a third, so the cap has to clear
# services.SCREENSHOT_MAX_BYTES encoded, with room for the JSON around it.
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024

# WhiteNoise: compress and cache static files. The manifest storage requires
# collectstatic to have run, so only use it when DEBUG is off (production overlay).
# "default" is the storage for FileField/ImageField uploads (screenshots land here,
# under MEDIA_ROOT) -- Django 5+ requires it to be spelled out once STORAGES is
# customized at all, since defining the dict opts out of the implicit defaults.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ── Production overlay ─────────────────────────────────────────────────────────
# When production.py exists it is imported here, overriding the dev defaults
# above. Create it from production.py.example and keep it out of version control.
_production_settings = Path(__file__).parent / "production.py"
if _production_settings.exists():
    from .production import *  # noqa: F401, F403
