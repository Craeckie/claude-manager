from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("offline/", views.offline, name="offline"),
    path("changes/", views.changes, name="changes"),
    path("activity/", views.activity_feed, name="activity"),
    path("projects/", views.projects, name="projects"),
    path("projects/<str:ref>/", views.project_detail, name="project_detail"),
    path("projects/<int:pk>/ideas/new/", views.idea_new, name="idea_new"),
    path("ideas/<int:pk>/", views.idea_detail, name="idea_detail"),
    path("ideas/<int:pk>/transition/", views.idea_transition, name="idea_transition"),
    path("ideas/<int:pk>/edit/", views.idea_edit, name="idea_edit"),
    path("ideas/<int:pk>/card/", views.idea_card, name="idea_card"),
    path("ideas/<int:pk>/body/", views.idea_body, name="idea_body"),
    path("ideas/<int:pk>/prompt/", views.idea_prompt, name="idea_prompt"),
    path("ideas/screenshots/<int:pk>/delete/", views.screenshot_delete, name="screenshot_delete"),
    path("ideas/<int:pk>/artifacts/upload/", views.artifact_upload, name="artifact_upload"),
    path("ideas/artifacts/<int:pk>/delete/", views.artifact_delete, name="artifact_delete"),
    path("ideas/artifacts/<int:pk>/download/", views.artifact_download, name="artifact_download"),
    path("projects/<int:pk>/transition/", views.project_transition, name="project_transition"),
    path("projects/<int:pk>/edit/", views.project_edit, name="project_edit"),
    path("projects/<int:pk>/card/", views.project_card, name="project_card"),
]
