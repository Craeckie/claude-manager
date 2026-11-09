from django.contrib import admin

from .models import Artifact, Event, Idea, Project, Screenshot


class IdeaInline(admin.TabularInline):
    model = Idea
    extra = 0
    fields = ("title", "status", "goal")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "folder", "status", "git_status", "idea_count", "updated_at")
    list_filter = ("status", "git_status")
    search_fields = ("name", "folder")
    readonly_fields = ("git_status", "git_detail", "git_checked_at")
    inlines = [IdeaInline]

    @admin.display(description="Ideas")
    def idea_count(self, obj):
        return obj.ideas.count()


@admin.register(Idea)
class IdeaAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "priority", "kind", "status", "updated_at")
    list_filter = ("status", "priority", "kind", "project")
    search_fields = ("title", "goal", "notes")
    fields = (
        "project",
        "title",
        "kind",
        "status",
        "goal",
        "context",
        "notes",
        "plan",
        "report",
        "review",
        "commits",
    )


@admin.register(Screenshot)
class ScreenshotAdmin(admin.ModelAdmin):
    list_display = ("image", "idea", "caption", "created_at")
    list_filter = ("idea",)
    search_fields = ("caption", "idea__title")


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):
    list_display = ("filename", "idea", "content_type", "size", "created_at")
    list_filter = ("idea", "content_type")
    search_fields = ("caption", "idea__title")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "source", "action", "idea_id", "idea_title", "project_name", "fields")
    list_filter = ("source", "action")
    search_fields = ("idea_title", "project_name")
