from pathlib import PurePosixPath

from .models import ARTIFACT_SANDBOXED_EXTENSIONS, SANDBOXED_HTML_CSP


def harden_media_response(response, name):
    """Headers for every response that hands back an uploaded file, whichever route
    serves it (/media/ or the artifact download view): nosniff always, so no upload is
    ever sniffed into HTML, and the sandbox CSP when `name` is an HTML file."""
    response["X-Content-Type-Options"] = "nosniff"
    if PurePosixPath(name).suffix.lower() in ARTIFACT_SANDBOXED_EXTENSIONS:
        response["Content-Security-Policy"] = SANDBOXED_HTML_CSP
    return response
