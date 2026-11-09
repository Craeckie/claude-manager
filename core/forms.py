from django import forms

from .models import Idea, Project


class IdeaForm(forms.ModelForm):
    class Meta:
        model = Idea
        fields = [
            "title", "priority", "kind", "needs_verification", "goal", "context", "notes", "plan",
            "report",
            "review", "commits",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model's blank=True only exists so a create that never mentions a type is
        # legal (the JS-off add form, POST /api/ideas/, MCP create_idea). It is not an
        # unset state: "other" is what unset means. So the select drops Django's empty
        # choice, which would otherwise read as a sixth, nameless type.
        self.fields["kind"].required = False
        self.fields["kind"].choices = Idea.Kind.choices
        # Same for priority: omitted means the default, and there is no empty choice.
        self.fields["priority"].required = False
        self.fields["priority"].choices = Idea.Priority.choices

    def clean_priority(self):
        # Like kind: a create or edit that never mentions it means the default.
        return self.cleaned_data.get("priority") or Idea.Priority.MEDIUM

    def clean_kind(self):
        return self.cleaned_data.get("kind") or Idea.Kind.OTHER

    def clean_commits(self):
        commits = self.cleaned_data.get("commits")
        return commits if commits is not None else []


class IdeaEditForm(IdeaForm):
    # Only an existing idea can be moved: a create takes its project from the URL or
    # the create_idea argument, never from the body, so IdeaForm stays without it and
    # the edit paths (the inline form, PATCH /api/ideas/<pk>/, MCP update_idea) bind
    # this one. First in the field order so the select heads the edit form.
    class Meta(IdeaForm.Meta):
        fields = ["project", *IdeaForm.Meta.fields]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No "---------" row: an idea always belongs to exactly one project. Not
        # required either: a POST that never mentions project (a client written
        # before the select existed) is an edit, not a move -- see clean_project.
        self.fields["project"].empty_label = None
        self.fields["project"].required = False
        self.fields["project"].queryset = Project.objects.order_by("name")

    def clean_project(self):
        return self.cleaned_data.get("project") or self.instance.project


class IdeaCreateForm(IdeaForm):
    # The add-idea form (htmx and the JS-off page) shows no plan box: a plan is written
    # in a separate pass, after the idea exists. MCP/API can still create with a plan by
    # using IdeaForm directly (see services.create_idea). Unlike plan, kind and
    # needs_verification are one click each, so they belong in the add form too.
    class Meta(IdeaForm.Meta):
        fields = ["title", "priority", "kind", "needs_verification", "goal", "context", "notes"]


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "folder", "description"]
