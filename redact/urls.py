from django.urls import path

from . import views

app_name = "redact"

urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("preview/", views.preview, name="preview"),
    path("paste/", views.paste, name="paste"),
]
