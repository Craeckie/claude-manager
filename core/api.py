"""HTTP JSON views adapting core/services.py to /api/. See design doc § 5.

Every view is a thin wrapper: parse the request, call one function in core/services.py,
hand the result back. The repetitive parts -- method checking, CSRF exemption, JSON body
parsing, response serialization, and mapping ServiceError subclasses to status codes --
live once in @api_view.
"""

import json
from functools import wraps

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from . import services
from .openapi import build_schema


def _service_error_response(exc):
    """Map a ServiceError to the same JSON shape api_view uses for it, for a view
    that cannot go through api_view itself -- a multipart body cannot be parsed as
    the JSON body api_view expects."""
    if isinstance(exc, services.NotFound):
        return JsonResponse({"error": str(exc)}, status=404)
    if isinstance(exc, services.ValidationFailed):
        return JsonResponse({"errors": exc.errors}, status=422)
    if isinstance(exc, services.Invalid):
        return JsonResponse({"error": str(exc)}, status=400)
    raise exc


def api_view(*methods):
    """Decorator that turns a plain function(request, *, body, **kwargs) -> data
    into a full Django view. `data` may be a dict/list (-> 200 JSON) or a
    (data, status) pair for a non-200 success (e.g. 201 on create). A view may
    also return an HttpResponse directly (the docs page), which passes through
    untouched.
    """

    def decorator(view_fn):
        @csrf_exempt
        @wraps(view_fn)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = JsonResponse(
                    {"error": f"method {request.method} not allowed"}, status=405
                )
                response["Allow"] = ", ".join(methods)
                return response

            body = {}
            if request.method in ("POST", "PATCH") and request.body:
                try:
                    body = json.loads(request.body)
                except ValueError:
                    return JsonResponse({"error": "malformed JSON body"}, status=400)
                if not isinstance(body, dict):
                    return JsonResponse({"error": "JSON body must be an object"}, status=400)

            try:
                result = view_fn(request, *args, body=body, **kwargs)
            except services.NotFound as exc:
                return JsonResponse({"error": str(exc)}, status=404)
            except services.ValidationFailed as exc:
                return JsonResponse({"errors": exc.errors}, status=422)
            except services.Invalid as exc:
                return JsonResponse({"error": str(exc)}, status=400)

            # The docs view renders a template and returns an HttpResponse directly;
            # everything else is JSON data to serialize.
            if isinstance(result, HttpResponse):
                return result
            if isinstance(result, tuple):
                data, status = result
            else:
                data, status = result, 200
            # safe=False: several routes return a bare JSON array (design § 5.1).
            return JsonResponse(data, safe=False, status=status)

        return wrapper

    return decorator


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true")


@api_view("GET", "POST")
def project_list(request, body):
    if request.method == "GET":
        return services.list_projects(status=request.GET.get("status"), folder=request.GET.get("folder"))
    return services.create_project(body), 201


@api_view("GET", "PATCH")
def project_detail(request, pk, body):
    if request.method == "GET":
        return services.get_project(pk)
    return services.update_project(pk, body)


@api_view("POST")
def project_transition(request, pk, body):
    return services.transition_project(pk, body.get("target"))


@api_view("GET", "POST")
def project_ideas(request, pk, body):
    if request.method == "GET":
        return services.list_ideas(project=pk)
    return services.create_idea(pk, body), 201


@api_view("GET")
def idea_list(request, body):
    return services.list_ideas(
        project=request.GET.get("project"),
        status=request.GET.get("status"),
        kind=request.GET.get("kind"),
        open_only=_truthy(request.GET.get("open")),
        commit=request.GET.get("commit"),
        q=request.GET.get("q"),
        # Full by default, unlike the MCP tool: the web card, the curl uploads and
        # CLAUDE.md's "GET /api/ideas/<id>/" checks all expect the whole record.
        full=(request.GET.get("full") or "1").strip().lower() not in ("0", "false"),
    )


@api_view("GET", "PATCH")
def idea_detail(request, pk, body):
    if request.method == "GET":
        return services.get_idea(pk)
    return services.update_idea(pk, body)


@api_view("POST")
def idea_transition(request, pk, body):
    return services.transition_idea(pk, body.get("target"))


@api_view("POST")
def sync_git_status(request, body):
    return services.sync_git_status()


@api_view("GET")
def openapi_schema(request, body):
    return build_schema(request)


@api_view("GET")
def docs(request, body):
    return render(request, "core/api_docs.html")


@csrf_exempt
def idea_screenshots(request, pk):
    # Multipart, like curl -F, not the JSON body api_view expects -- this view goes
    # around that decorator and maps ServiceError itself, matching its error shapes.
    if request.method != "POST":
        response = JsonResponse({"error": f"method {request.method} not allowed"}, status=405)
        response["Allow"] = "POST"
        return response

    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "missing 'file' part"}, status=400)

    try:
        # Checked against the upload's own size first: a rejected file should never be
        # pulled into memory whole, least of all on an unauthenticated endpoint.
        services.check_screenshot_upload(upload.name, upload.size)
        data = services.add_screenshot(
            pk, upload.name, upload.read(), caption=request.POST.get("caption", "")
        )
    except services.ServiceError as exc:
        return _service_error_response(exc)
    return JsonResponse(data, status=201)


@csrf_exempt
def screenshot_detail(request, pk):
    if request.method != "DELETE":
        response = JsonResponse({"error": f"method {request.method} not allowed"}, status=405)
        response["Allow"] = "DELETE"
        return response

    try:
        data = services.delete_screenshot(pk)
    except services.ServiceError as exc:
        return _service_error_response(exc)
    return JsonResponse(data)


@csrf_exempt
def idea_artifacts(request, pk):
    # Multipart, like idea_screenshots above -- goes around api_view for the same
    # reason: a multipart body isn't the JSON body it expects.
    if request.method != "POST":
        response = JsonResponse({"error": f"method {request.method} not allowed"}, status=405)
        response["Allow"] = "POST"
        return response

    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "missing 'file' part"}, status=400)

    try:
        services.check_artifact_upload(upload.name, upload.size, services.ARTIFACT_MAX_BYTES)
        data = services.add_artifact(
            pk, upload.name, upload.read(), caption=request.POST.get("caption", "")
        )
    except services.ServiceError as exc:
        return _service_error_response(exc)
    return JsonResponse(data, status=201)


@csrf_exempt
def artifact_detail(request, pk):
    if request.method == "GET":
        try:
            data = services.read_artifact(pk)
        except services.ServiceError as exc:
            return _service_error_response(exc)
        if not _truthy(request.GET.get("content")):
            data.pop("content", None)
            data.pop("content_truncated", None)
        return JsonResponse(data)

    if request.method == "DELETE":
        try:
            data = services.delete_artifact(pk)
        except services.ServiceError as exc:
            return _service_error_response(exc)
        return JsonResponse(data)

    response = JsonResponse({"error": f"method {request.method} not allowed"}, status=405)
    response["Allow"] = "GET, DELETE"
    return response
