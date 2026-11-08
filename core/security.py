from pathlib import PurePosixPath

from django.http import HttpResponse
from django.utils.html import escape

from .models import ARTIFACT_SANDBOXED_EXTENSIONS, SANDBOXED_HTML_CSP, _render_markdown

# A rendered .md page has no scripts at all: sandboxed (opaque origin), inline CSS only.
MARKDOWN_PAGE_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"

_MARKDOWN_PAGE = '''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{color-scheme:light dark}}
body{{font:16px/1.6 system-ui,sans-serif;max-width:50rem;margin:0 auto;padding:1rem 1rem 4rem}}
pre,code{{font-family:ui-monospace,monospace;font-size:.9em}}
pre{{overflow-x:auto;padding:.75rem;background:rgba(127,127,127,.15);border-radius:6px}}
code{{background:rgba(127,127,127,.15);padding:.1em .3em;border-radius:3px}}
pre code{{background:none;padding:0}}
blockquote{{margin-left:0;padding-left:1rem;border-left:4px solid rgba(127,127,127,.4);opacity:.85}}
table{{border-collapse:collapse;display:block;overflow-x:auto}}
th,td{{border:1px solid rgba(127,127,127,.5);padding:.3rem .6rem}}
img{{max-width:100%}}
</style></head><body>
{body}
</body></html>
'''


def is_markdown_name(name):
    return PurePosixPath(name).suffix.lower() == ".md"


def markdown_page_response(raw, name):
    """A standalone, sanitized HTML page for Markdown source `raw` (bytes)."""
    body = _render_markdown(raw.decode("utf-8", errors="replace"))
    page = _MARKDOWN_PAGE.format(title=escape(PurePosixPath(name).name), body=body)
    response = HttpResponse(page, content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = MARKDOWN_PAGE_CSP
    response["X-Content-Type-Options"] = "nosniff"
    return response


def harden_media_response(response, name):
    """Headers for every response that hands back an uploaded file, whichever route
    serves it (/media/ or the artifact download view): nosniff always, so no upload is
    ever sniffed into HTML, and the sandbox CSP when `name` is an HTML file."""
    response["X-Content-Type-Options"] = "nosniff"
    if PurePosixPath(name).suffix.lower() in ARTIFACT_SANDBOXED_EXTENSIONS:
        response["Content-Security-Policy"] = SANDBOXED_HTML_CSP
    return response
