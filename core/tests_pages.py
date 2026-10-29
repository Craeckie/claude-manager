"""The separate pages of #384: Ideas, Projects, a project's page and an idea's page,
and the page context (`?view=`) every card endpoint answers for."""

from django.test import TestCase
from django.urls import reverse

from .models import Event, Idea, Project
from .views import Page


class PageContextTests(TestCase):
    def test_query_string_names_the_view_and_the_closed_flag(self):
        self.assertEqual(Page("ideas").qs, "")
        self.assertEqual(Page("ideas", True).qs, "?closed=1")
        self.assertEqual(Page("idea").qs, "?view=idea")
        self.assertEqual(Page("project", True).qs, "?view=project&closed=1")

    def test_unknown_view_falls_back_to_ideas(self):
        request = self.client.get("/").wsgi_request
        request.GET = request.GET.copy()
        request.GET["view"] = "bogus"
        self.assertEqual(Page.from_request(request).view, "ideas")


class IdeaDetailPageTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Alpha", folder="alpha")
        self.idea = Idea.objects.create(project=self.project, title="Detailed", goal="Reach the goal.")
        self.other = Idea.objects.create(project=self.project, title="Unrelated")

    def page(self, idea=None):
        return self.client.get(reverse("core:idea_detail", args=[(idea or self.idea).pk]))

    def test_detail_url_is_the_idea_page(self):
        self.assertEqual(self.idea.detail_url, f"/ideas/{self.idea.pk}/")
        self.assertEqual(self.page().status_code, 200)

    def test_edit_url_keeps_working(self):
        # Hundreds of stored reports link to /ideas/<id>/edit/.
        self.assertEqual(self.client.get(f"/ideas/{self.idea.pk}/edit/").status_code, 200)

    def test_unknown_idea_is_404(self):
        self.assertEqual(self.client.get("/ideas/999999/").status_code, 404)

    def test_card_is_expanded_and_not_lazy(self):
        html = self.page().content.decode()
        self.assertIn(f'id="idea-expand-{self.idea.pk}" class="idea-expand-toggle" checked', html)
        self.assertNotIn('class="idea-lazy"', html)
        self.assertIn("Reach the goal.", html)
        self.assertIn("### Goal", html)

    def test_breadcrumb_names_project_and_id(self):
        html = self.page().content.decode()
        self.assertIn('class="breadcrumb"', html)
        self.assertIn("Alpha", html)
        self.assertIn(f">#{self.idea.pk}</span>", html)

    def test_closed_idea_is_shown(self):
        self.idea.status = Idea.Status.ABANDONED
        self.idea.save()
        self.assertContains(self.page(), f'id="idea-{self.idea.pk}"')

    def test_card_urls_carry_the_view(self):
        html = self.page().content.decode()
        transition = reverse("core:idea_transition", args=[self.idea.pk])
        self.assertIn(f'hx-post="{transition}?view=idea"', html)
        self.assertNotIn("idea-open-link", html)

    def test_transition_to_closed_keeps_the_card(self):
        self.idea.status = Idea.Status.VERIFY
        self.idea.save()
        url = reverse("core:idea_transition", args=[self.idea.pk]) + "?view=idea"
        response = self.client.post(url, {"target": "implemented"}, headers={"HX-Request": "true"})
        self.assertContains(response, f'id="idea-{self.idea.pk}"')
        self.assertContains(response, "badge-implemented")

    def test_js_off_transition_returns_to_the_idea_page(self):
        url = reverse("core:idea_transition", args=[self.idea.pk]) + "?view=idea"
        response = self.client.post(url, {"target": "planned"})
        self.assertRedirects(response, self.idea.detail_url, fetch_redirect_response=False)

    def test_move_keeps_the_card_on_the_idea_page(self):
        beta = Project.objects.create(name="Beta", folder="beta")
        url = reverse("core:idea_edit", args=[self.idea.pk]) + "?view=idea"
        response = self.client.post(
            url, {"project": beta.pk, "title": "Detailed", "goal": "", "context": "", "notes": ""},
            headers={"HX-Request": "true"},
        )
        self.assertContains(response, f'id="idea-{self.idea.pk}"')

    def test_activity_is_filtered_to_the_idea(self):
        for idea in (self.idea, self.other):
            self.client.post(reverse("core:idea_transition", args=[idea.pk]), {"target": "planned"})
        html = self.page().content.decode()
        self.assertEqual(html.count('class="activity-entry'), 1)
        self.assertIn(f'hx-get="{reverse("core:activity")}?idea={self.idea.pk}"', html)


class ActivityIdeaFilterTests(TestCase):
    def setUp(self):
        project = Project.objects.create(name="Alpha", folder="alpha")
        self.idea = Idea.objects.create(project=project, title="Watched")
        other = Idea.objects.create(project=project, title="Ignored")
        for idea in (self.idea, other):
            self.client.post(reverse("core:idea_transition", args=[idea.pk]), {"target": "planned"})

    def test_idea_filter(self):
        html = self.client.get(reverse("core:activity"), {"idea": self.idea.pk}).content.decode()
        self.assertIn("Watched", html)
        self.assertNotIn("Ignored", html)
        self.assertEqual(Event.objects.count(), 2)

    def test_unknown_idea_matches_nothing_and_keeps_its_filter_on_refresh(self):
        html = self.client.get(reverse("core:activity"), {"idea": "999999"}).content.decode()
        self.assertIn("Nothing matches that filter.", html)
        self.assertNotIn("Watched", html)
        self.assertIn("idea=999999", html)


class ProjectDetailPageTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Alpha", folder="alpha", status=Project.Status.DEFINED, description="About alpha."
        )
        self.other = Project.objects.create(name="Beta", folder="beta")
        self.open = Idea.objects.create(project=self.project, title="Open one")
        self.closed = Idea.objects.create(project=self.project, title="Done one", status=Idea.Status.IMPLEMENTED)
        Idea.objects.create(project=self.other, title="Beta's idea")

    def page(self, query=""):
        return self.client.get(reverse("core:project_detail", args=["alpha"]) + query)

    def test_page_is_addressed_by_folder(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("<h1>Alpha</h1>", html)
        self.assertIn("About alpha.", html)
        self.assertIn('class="badge badge-defined"', html)

    def test_pk_url_redirects_to_the_folder_url(self):
        response = self.client.get(f"/projects/{self.project.pk}/?closed=1")
        self.assertRedirects(response, "/projects/alpha/?closed=1", fetch_redirect_response=False)

    def test_numeric_folder_wins_over_a_pk(self):
        numeric = Project.objects.create(name="Numeric", folder=str(self.project.pk))
        response = self.client.get(f"/projects/{self.project.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"<h1>{numeric.name}</h1>")

    def test_unknown_project_is_404(self):
        self.assertEqual(self.client.get("/projects/nope/").status_code, 404)
        self.assertEqual(self.client.get("/projects/999999/").status_code, 404)

    def test_lists_only_its_open_ideas_until_show_closed(self):
        html = self.page().content.decode()
        self.assertIn("Open one", html)
        self.assertNotIn("Done one", html)
        self.assertNotIn("Beta's idea", html)
        self.assertIn("Done one", self.page("?closed=1").content.decode())

    def test_add_idea_slot_and_template_target_the_idea_list(self):
        html = self.page().content.decode()
        self.assertIn(f'id="new-idea-{self.project.pk}"', html)
        self.assertIn(f'id="project-ideas-{self.project.pk}"', html)
        self.assertIn('hx-target="#project-ideas-__PK__"', html)
        self.assertIn(reverse("core:idea_new", args=[999999999999]).replace("999999999999", "__PK__") + "?view=project", html)

    def test_adding_an_idea_re_renders_the_list(self):
        url = reverse("core:idea_new", args=[self.project.pk]) + "?view=project"
        response = self.client.post(
            url, {"title": "Fresh", "goal": "", "context": "", "notes": ""}, headers={"HX-Request": "true"}
        )
        html = response.content.decode()
        self.assertIn(f'id="project-ideas-{self.project.pk}"', html)
        self.assertIn(f'id="new-idea-{self.project.pk}"', html)
        self.assertIn("Fresh", html)
        self.assertNotIn(f'<section class="project', html)

    def test_js_off_add_returns_to_the_project_page(self):
        url = reverse("core:idea_new", args=[self.project.pk]) + "?view=project"
        response = self.client.post(url, {"title": "Fresh", "goal": "", "context": "", "notes": ""})
        self.assertRedirects(response, "/projects/alpha/", fetch_redirect_response=False)

    def test_transition_re_renders_the_header_and_keeps_a_closed_project(self):
        url = reverse("core:project_transition", args=[self.project.pk]) + "?view=project"
        response = self.client.post(url, {"target": "canceled"}, headers={"HX-Request": "true"})
        html = response.content.decode()
        self.assertIn(f'id="project-{self.project.pk}"', html)
        self.assertIn("project-detail", html)
        self.assertIn("badge-canceled", html)

    def test_edit_cancel_and_save_come_back_as_the_header(self):
        url = reverse("core:project_edit", args=[self.project.pk]) + "?view=project"
        form = self.client.get(url, headers={"HX-Request": "true"}).content.decode()
        self.assertIn(reverse("core:project_card", args=[self.project.pk]) + "?view=project", form)
        self.assertIn('href="/projects/alpha/"', form)
        response = self.client.post(
            url, {"name": "Alpha 2", "folder": "alpha", "description": "", "status": "defined"},
            headers={"HX-Request": "true"},
        )
        self.assertContains(response, "<h1>Alpha 2</h1>")

    def test_closed_idea_leaves_the_list(self):
        url = reverse("core:idea_transition", args=[self.open.pk]) + "?view=project"
        response = self.client.post(url, {"target": "abandoned"}, headers={"HX-Request": "true"})
        self.assertEqual(response.content.decode().strip(), "")

    def test_activity_is_filtered_to_the_project(self):
        self.client.post(reverse("core:idea_transition", args=[self.open.pk]), {"target": "planned"})
        beta_idea = Idea.objects.get(title="Beta's idea")
        self.client.post(reverse("core:idea_transition", args=[beta_idea.pk]), {"target": "planned"})
        html = self.page().content.decode()
        self.assertEqual(html.count('class="activity-entry'), 1)
        self.assertIn(f'hx-get="{reverse("core:activity")}?project=alpha"', html)

    def test_idea_page_breadcrumb_links_the_project_page(self):
        html = self.client.get(self.open.detail_url).content.decode()
        self.assertIn('href="/projects/alpha/"', html)


class ProjectsViewTests(TestCase):
    def setUp(self):
        self.busy = Project.objects.create(name="Busy", folder="busy", status=Project.Status.DEFINED)
        self.idle = Project.objects.create(name="Idle", folder="idle", status=Project.Status.IMPLEMENTED)
        self.gone = Project.objects.create(name="Gone", folder="gone", status=Project.Status.CANCELED)
        Idea.objects.create(project=self.busy, title="Busy idea", status=Idea.Status.VERIFY)
        Idea.objects.create(project=self.busy, title="Another", status=Idea.Status.NEW)

    def page(self, **params):
        return self.client.get(reverse("core:projects"), params).content.decode()

    def test_rows_link_to_project_pages_without_idea_cards(self):
        html = self.page()
        self.assertIn('href="/projects/busy/"', html)
        self.assertIn('href="/projects/idle/"', html)
        self.assertNotIn("Busy idea", html)
        self.assertNotIn('class="idea', html)
        self.assertIn("1 verify · 1 new", html)

    def test_grouped_by_open_ideas_closed_only_on_request(self):
        html = self.page()
        self.assertLess(html.index("With open ideas"), html.index("Busy"))
        self.assertLess(html.index("Busy"), html.index("Without open ideas"))
        self.assertLess(html.index("Without open ideas"), html.index("Idle"))
        self.assertNotIn("Gone", html)
        closed = self.page(closed="1")
        self.assertIn("Gone", closed)
        self.assertLess(closed.index("Idle"), closed.index("Closed"))

    def test_status_menu_only_off_implemented(self):
        html = self.page()
        self.assertIn('class="badge badge-defined"', html)
        self.assertNotIn("badge-implemented", html)
        self.assertIn(
            f'hx-post="{reverse("core:project_transition", args=[self.busy.pk])}?view=projects"', html
        )

    def test_transition_returns_the_row_and_closing_removes_it(self):
        url = reverse("core:project_transition", args=[self.busy.pk])
        response = self.client.post(url + "?view=projects", {"target": "implemented"}, headers={"HX-Request": "true"})
        self.assertContains(response, 'class="project-row')
        self.idle.status = Project.Status.IMPLEMENTED
        response = self.client.post(
            reverse("core:project_transition", args=[self.idle.pk]) + "?view=projects",
            {"target": "discontinued"}, headers={"HX-Request": "true"},
        )
        self.assertEqual(response.content.decode().strip(), "")

    def test_js_off_transition_returns_to_the_projects_view(self):
        url = reverse("core:project_transition", args=[self.busy.pk]) + "?view=projects&closed=1"
        response = self.client.post(url, {"target": "implemented"})
        self.assertRedirects(response, "/projects/?closed=1", fetch_redirect_response=False)

    def test_js_off_filter_hides_non_matching_rows_and_empty_groups(self):
        html = self.page(q="IDL")
        self.assertRegex(html, r'id="project-%d"[^>]*\s+hidden' % self.busy.pk)
        self.assertNotRegex(html, r'id="project-%d"[^>]*\s+hidden' % self.idle.pk)
        self.assertIn('<section class="project-group" hidden>', html)
        self.assertIn('class="empty no-match-message" hidden', html)

    def test_filter_matching_nothing_says_so(self):
        html = self.page(q="zzz")
        self.assertIn('<p class="empty no-match-message">', html)

    def test_query_count_does_not_grow_with_projects(self):
        # Projects, then their shown ideas -- no idea files for a row.
        with self.assertNumQueries(2):
            self.client.get(reverse("core:projects"))


class IdeasViewTests(TestCase):
    def setUp(self):
        self.alpha = Project.objects.create(name="Alpha App", folder="alpha")
        self.beta = Project.objects.create(name="Beta", folder="beta-tool")
        self.new = Idea.objects.create(project=self.alpha, title="Fresh thought")
        self.planned = Idea.objects.create(project=self.beta, title="Planned work", status=Idea.Status.PLANNED)
        self.verify = Idea.objects.create(project=self.alpha, title="Check me", status=Idea.Status.VERIFY)
        self.done = Idea.objects.create(project=self.beta, title="Shipped", status=Idea.Status.IMPLEMENTED)

    def page(self, **params):
        return self.client.get(reverse("core:home"), params).content.decode()

    def card_tag(self, html, idea):
        start = html.index(f'id="idea-{idea.pk}"')
        return html[start:html.index(">", start)]

    def test_sections_in_order_verify_planned_new(self):
        html = self.page()
        self.assertIn("<h1>Ideas</h1>", html)
        self.assertLess(html.index('id="ideas-verify"'), html.index('id="ideas-planned"'))
        self.assertLess(html.index('id="ideas-planned"'), html.index('id="ideas-new"'))
        self.assertLess(html.index(f'id="idea-{self.verify.pk}"'), html.index(f'id="idea-{self.planned.pk}"'))
        self.assertLess(html.index(f'id="idea-{self.planned.pk}"'), html.index(f'id="idea-{self.new.pk}"'))
        self.assertNotIn("Shipped", html)

    def test_newest_first_within_a_section(self):
        newer = Idea.objects.create(project=self.beta, title="Newer thought")
        html = self.page()
        self.assertLess(html.index(f'id="idea-{newer.pk}"'), html.index(f'id="idea-{self.new.pk}"'))

    def test_closed_adds_implemented_and_abandoned_sections(self):
        html = self.page(closed="1")
        self.assertIn('id="ideas-implemented"', html)
        self.assertIn('id="ideas-abandoned"', html)
        self.assertIn("Shipped", html)

    def test_cards_carry_a_project_chip_and_stay_lazy(self):
        html = self.page()
        self.assertIn('<a class="idea-project" href="/projects/alpha/">Alpha App</a>', html)
        self.assertIn('class="idea-lazy"', html)
        self.assertIn(f'href="/ideas/{self.new.pk}/"', html)

    def test_no_add_idea_here(self):
        self.assertNotIn("add-idea-link", self.page())

    def test_js_off_filter_matches_title_project_name_and_folder(self):
        for query, hit in (("THOUGHT", self.new), ("beta", self.planned), ("beta-tool", self.planned), ("alpha app", self.verify)):
            html = self.page(q=query)
            self.assertNotIn("hidden", self.card_tag(html, hit), query)
        html = self.page(q="beta-tool")
        self.assertIn("hidden", self.card_tag(html, self.new))
        self.assertIn('class="empty no-match-message" hidden', html)

    def test_filter_hides_a_section_without_matches(self):
        html = self.page(q="Planned work")
        section = html[html.index('data-status="verify"'):]
        self.assertTrue(section.split(">")[0].endswith(" hidden"))

    def test_filter_matching_nothing_says_so(self):
        self.assertIn('<p class="empty no-match-message">', self.page(q="zzz"))

    def test_transition_re_renders_the_card_with_its_chip(self):
        url = reverse("core:idea_transition", args=[self.new.pk])
        response = self.client.post(url, {"target": "planned"}, headers={"HX-Request": "true"})
        html = response.content.decode()
        self.assertIn(f'id="idea-{self.new.pk}"', html)
        self.assertIn('class="idea-project"', html)
        self.assertIn("data-filter=", html)

    def test_closing_removes_the_card(self):
        url = reverse("core:idea_transition", args=[self.verify.pk])
        response = self.client.post(url, {"target": "implemented"}, headers={"HX-Request": "true"})
        self.assertEqual(response.content.decode().strip(), "")

    def test_moved_idea_comes_back_with_its_new_chip(self):
        url = reverse("core:idea_edit", args=[self.new.pk])
        response = self.client.post(
            url, {"project": self.beta.pk, "title": "Fresh thought", "goal": "", "context": "", "notes": ""},
            headers={"HX-Request": "true"},
        )
        self.assertContains(response, '<a class="idea-project" href="/projects/beta-tool/">Beta</a>')

    def test_js_off_edit_returns_to_the_ideas_view(self):
        url = reverse("core:idea_edit", args=[self.new.pk]) + "?closed=1"
        response = self.client.post(
            url, {"project": self.alpha.pk, "title": "Renamed", "goal": "", "context": "", "notes": ""}
        )
        self.assertRedirects(response, "/?closed=1", fetch_redirect_response=False)

    def test_empty_board_points_at_the_projects(self):
        Idea.objects.all().delete()
        html = self.page()
        self.assertIn("No open ideas.", html)
        self.assertIn(f'href="{reverse("core:projects")}"', html)


class NavTests(TestCase):
    def setUp(self):
        project = Project.objects.create(name="Alpha", folder="alpha")
        self.idea = Idea.objects.create(project=project, title="Thing")

    def test_nav_names_every_view_and_marks_the_current_one(self):
        cases = {
            "/": "Ideas",
            self.idea.detail_url: "Ideas",
            "/projects/": "Projects",
            "/projects/alpha/": "Projects",
            reverse("core:activity"): "Activity",
            reverse("redact:index"): "Redact logs",
        }
        for url, current in cases.items():
            html = self.client.get(url).content.decode()
            nav = html[html.index('<nav aria-label="Main">'):html.index("</nav>")]
            for label in ("Ideas", "Projects", "Activity", "Redact logs", "Admin"):
                self.assertIn(f">{label}</a>", nav, url)
            self.assertEqual(nav.count('aria-current="page"'), 1, url)
            self.assertIn(f'aria-current="page">{current}</a>', nav, url)
