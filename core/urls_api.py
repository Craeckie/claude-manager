"""URLconf for the JSON API. Included at /api/ from claude_manager/urls.py."""

from django.urls import path

from . import api

app_name = "api"

urlpatterns = [
    path("openapi.json", api.openapi_schema, name="openapi"),
    path("docs/", api.docs, name="docs"),
    path("projects/", api.project_list, name="project_list"),
    path("projects/<int:pk>/", api.project_detail, name="project_detail"),
    path("projects/<int:pk>/transition/", api.project_transition, name="project_transition"),
    path("projects/sync_git_status/", api.sync_git_status, name="sync_git_status"),
    path("projects/<int:pk>/ideas/", api.project_ideas, name="project_ideas"),
    path("ideas/", api.idea_list, name="idea_list"),
    path("ideas/<int:pk>/", api.idea_detail, name="idea_detail"),
    path("ideas/<int:pk>/transition/", api.idea_transition, name="idea_transition"),
    path("ideas/<int:pk>/screenshots/", api.idea_screenshots, name="idea_screenshots"),
    path("screenshots/<int:pk>/", api.screenshot_detail, name="screenshot_detail"),
    path("ideas/<int:pk>/artifacts/", api.idea_artifacts, name="idea_artifacts"),
    path("artifacts/<int:pk>/", api.artifact_detail, name="artifact_detail"),
]
