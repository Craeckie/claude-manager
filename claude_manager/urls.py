from django.conf import settings
from django.contrib import admin
from django.urls import path, include, re_path

from core import mcp as core_mcp
from core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("sw.js", core_views.service_worker, name="service_worker"),
    path("api/", include("core.urls_api")),
    # Both spellings: APPEND_SLASH answers a POST to the unslashed path with a 301,
    # which drops the JSON-RPC body and is a confusing failure to debug.
    path("mcp", core_mcp.mcp_endpoint),
    path("mcp/", core_mcp.mcp_endpoint, name="mcp"),
    # Unconditional, not DEBUG-only: the service is unauthenticated by design and sits
    # behind one reverse proxy, so Django serving a handful of screenshot PNGs is fine
    # and avoids standing up a second static root just for this in production.
    # core_views.media wraps django.views.static.serve to add nosniff everywhere and
    # the sandbox CSP on uploaded HTML (idea #248).
    re_path(rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$", core_views.media),
    path("redact/", include("redact.urls")),
    path("", include("core.urls")),
]
