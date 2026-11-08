import re
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import IdeaCreateForm, IdeaEditForm, IdeaForm, ProjectForm
from .models import (
    Artifact,
    CLOSED_IDEA_STATUSES,
    Idea,
    OPEN_IDEA_STATUSES,
    Project,
    SANDBOXED_HTML_CSP,
    Screenshot,
    _loosen_markdown,
)
from .transitions import IDEA_TRANSITIONS, PROJECT_TRANSITIONS


LAZY_STAND_IN = re.compile(r'<div class="idea-lazy" hx-get="([^"]+)".*?</div>', re.S)


def expand_lazy(client, html):
    """Swap every lazy stand-in (_idea_lazy.html) for what htmx fetches into it once
    it is visible, nested ones included -- the page as the user sees it with every
    card expanded and every Prompt <details> opened (#269)."""
    while match := LAZY_STAND_IN.search(html):
        fetched = client.get(match.group(1).replace("&amp;", "&")).content.decode()
        html = html[: match.start()] + fetched + html[match.end() :]
    return html


class ProjectOrderingTests(TestCase):
    def test_projects_sorted_by_most_recent_activity(self):
        first = Project.objects.create(name="First Proj", folder="first")
        second = Project.objects.create(name="Second Proj", folder="second")
        Project.objects.create(name="Third Proj", folder="third")

        # Touch "first" last so it becomes the most recently updated.
        second.save()
        first.save()

        names = list(Project.objects.values_list("name", flat=True))
        self.assertEqual(names, ["First Proj", "Second Proj", "Third Proj"])

    def test_a_new_idea_bumps_its_project(self):
        first = Project.objects.create(name="First Proj", folder="first")
        Project.objects.create(name="Second Proj", folder="second")

        # "Second" is the more recently touched project, but an idea under
        # "First" is newer still, so "First" sorts ahead of it.
        Idea.objects.create(project=first, title="Fresh idea")

        names = list(Project.objects.values_list("name", flat=True))
        self.assertEqual(names, ["First Proj", "Second Proj"])

    def test_touching_an_existing_idea_bumps_its_project(self):
        first = Project.objects.create(name="First Proj", folder="first")
        idea = Idea.objects.create(project=first, title="Idea")
        Project.objects.create(name="Second Proj", folder="second")

        idea.status = Idea.Status.PLANNED
        idea.save()

        names = list(Project.objects.values_list("name", flat=True))
        self.assertEqual(names, ["First Proj", "Second Proj"])

    def test_project_without_ideas_still_sorts_by_its_own_activity(self):
        # The idea timestamp is NULL for a project with no ideas; it has to fall
        # back to the project's own updated_at rather than sorting as NULL. The
        # idea here is closed, so it doesn't trigger the open-idea priority and
        # this stays a pure test of the recency fallback.
        with_idea = Project.objects.create(name="With Idea", folder="with")
        Idea.objects.create(
            project=with_idea, title="Idea", status=Idea.Status.IMPLEMENTED
        )
        empty = Project.objects.create(name="Empty", folder="empty")

        self.assertEqual(
            list(Project.objects.values_list("name", flat=True)),
            ["Empty", "With Idea"],
        )

        empty.save()
        with_idea.save()
        self.assertEqual(
            list(Project.objects.values_list("name", flat=True)),
            ["With Idea", "Empty"],
        )


class IdeaOrderingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_ideas_sorted_newest_first(self):
        Idea.objects.create(project=self.project, title="Older", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Newer", status=Idea.Status.NEW)

        titles = list(Idea.objects.values_list("title", flat=True))
        self.assertEqual(titles, ["Newer", "Older"])

    def test_status_does_not_group_the_ideas(self):
        # Recency is the only key: a planned idea written before a new one still
        # sorts behind it, and closed ideas are not pushed to the end either.
        Idea.objects.create(project=self.project, title="Planned", status=Idea.Status.PLANNED)
        Idea.objects.create(project=self.project, title="Abandoned", status=Idea.Status.ABANDONED)
        Idea.objects.create(project=self.project, title="New", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Implemented", status=Idea.Status.IMPLEMENTED)

        titles = list(Idea.objects.values_list("title", flat=True))
        self.assertEqual(titles, ["Implemented", "New", "Abandoned", "Planned"])

    def test_touching_an_idea_moves_it_to_the_top(self):
        older = Idea.objects.create(project=self.project, title="Older", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Newer", status=Idea.Status.NEW)

        older.save()

        titles = list(Idea.objects.values_list("title", flat=True))
        self.assertEqual(titles, ["Older", "Newer"])

    def test_a_transition_moves_an_idea_to_the_top(self):
        idea = Idea.objects.create(project=self.project, title="Older", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Newer", status=Idea.Status.NEW)

        idea.status = Idea.Status.PLANNED
        idea.save()

        titles = list(Idea.objects.values_list("title", flat=True))
        self.assertEqual(titles, ["Older", "Newer"])

    def test_open_idea_statuses_excludes_closed_ideas(self):
        Idea.objects.create(project=self.project, title="New", status=Idea.Status.NEW)
        Idea.objects.create(project=self.project, title="Planned", status=Idea.Status.PLANNED)
        Idea.objects.create(project=self.project, title="Implemented", status=Idea.Status.IMPLEMENTED)
        Idea.objects.create(project=self.project, title="Abandoned", status=Idea.Status.ABANDONED)

        open_titles = set(
            Idea.objects.filter(status__in=OPEN_IDEA_STATUSES).values_list("title", flat=True)
        )
        self.assertEqual(open_titles, {"New", "Planned"})


PROMPT_PREAMBLE = (
    "### Project\n"
    "`cd /workspace/proj` first and carry out everything below in that "
    "project, using it as the working directory for all paths and commands. "
    "Before planning or implementing anything below, read `/workspace/CLAUDE.md` "
    "and `/workspace/proj/CLAUDE.md` (its authority for this project) if "
    "you have not already read them this session."
)


class IdeaPromptBuildingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_build_prompt_includes_all_filled_sections_in_order(self):
        idea = Idea.objects.create(
            project=self.project,
            title="My Title",
            goal="My Goal",
            context="My Context",
            notes="My Notes",
        )
        expected = (
            f"{PROMPT_PREAMBLE}\n\n"
            "### Title\nMy Title\n\n"
            "### Goal\nMy Goal\n\n"
            "### Context\nMy Context\n\n"
            "### Notes\nMy Notes"
        )
        self.assertEqual(idea.build_prompt(), expected)

    def test_build_prompt_omits_empty_optional_sections(self):
        idea = Idea.objects.create(
            project=self.project,
            title="My Title",
            goal="My Goal",
        )
        self.assertEqual(
            idea.build_prompt(),
            f"{PROMPT_PREAMBLE}\n\n### Title\nMy Title\n\n### Goal\nMy Goal",
        )

    def test_build_prompt_starts_by_scoping_the_work_to_the_project_directory(self):
        idea = Idea.objects.create(project=self.project, title="My Title", goal="My Goal")
        self.assertTrue(idea.build_prompt().startswith("### Project\n"))
        self.assertIn("`cd /workspace/proj`", idea.build_prompt())

    def test_build_prompt_names_the_projects_own_folder(self):
        other = Project.objects.create(name="Other", folder="other-folder")
        idea = Idea.objects.create(project=other, title="My Title")
        self.assertIn("`cd /workspace/other-folder`", idea.build_prompt())

    def test_title_only_idea_still_gets_preamble_and_title(self):
        idea = Idea.objects.create(project=self.project, title="My Title")
        self.assertEqual(idea.build_prompt(), f"{PROMPT_PREAMBLE}\n\n### Title\nMy Title")

    def test_overview_renders_the_prompt_block_for_a_title_only_idea(self):
        Idea.objects.create(project=self.project, title="Bare Idea")
        html = expand_lazy(self.client, self.client.get(reverse("core:home")).content.decode())
        self.assertIn("`cd /workspace/proj`", html)
        self.assertIn("### Title\nBare Idea", html)

    def test_build_prompt_appends_plan_section_after_notes(self):
        idea = Idea.objects.create(
            project=self.project,
            title="My Title",
            notes="My Notes",
            plan="1. Do the thing\n2. Then the other thing",
        )
        self.assertEqual(
            idea.build_prompt(),
            f"{PROMPT_PREAMBLE}\n\n"
            "### Title\nMy Title\n\n"
            "### Notes\nMy Notes\n\n"
            "### Implementation plan\n1. Do the thing\n2. Then the other thing\n\n"
            "Follow this plan; deviate only if it turns out to be wrong, and say so.",
        )

    def test_build_prompt_has_no_plan_section_when_plan_is_blank(self):
        idea = Idea.objects.create(project=self.project, title="My Title", plan="   ")
        self.assertNotIn("Implementation plan", idea.build_prompt())
        self.assertNotIn("Follow this plan", idea.build_prompt())

    def test_build_prompt_appends_review_section_and_instruction(self):
        idea = Idea.objects.create(
            project=self.project,
            title="My Title",
            review="- The filter drops the first line.",
        )
        prompt = idea.build_prompt()
        self.assertIn("### Review findings\n- The filter drops the first line.", prompt)
        self.assertIn(
            "Address these review findings; say what changed in the report.", prompt
        )

    def test_build_prompt_has_no_review_section_when_review_is_blank(self):
        idea = Idea.objects.create(project=self.project, title="My Title", review="   ")
        self.assertNotIn("Review findings", idea.build_prompt())
        self.assertNotIn("Address these review findings", idea.build_prompt())

    def test_prompt_sections_are_exactly_type_goal_context_notes_plan_review(self):
        self.assertEqual(
            [heading for heading, field in Idea.PROMPT_SECTIONS],
            ["Type", "Goal", "Context", "Notes", "Implementation plan", "Review findings"],
        )

    def test_build_prompt_includes_type_section_right_after_title(self):
        idea = Idea.objects.create(
            project=self.project, title="My Title", kind=Idea.Kind.FIX, goal="My Goal"
        )
        self.assertEqual(
            idea.build_prompt(),
            f"{PROMPT_PREAMBLE}\n\n### Title\nMy Title\n\n### Type\nFix\n\n### Goal\nMy Goal",
        )

    def test_build_prompt_omits_type_section_for_other_kind(self):
        # "other" is the default, and its prompt must be byte-for-byte what an idea
        # produced before the kind field existed.
        idea = Idea.objects.create(
            project=self.project, title="My Title", kind=Idea.Kind.OTHER, goal="My Goal"
        )
        self.assertEqual(
            idea.build_prompt(),
            f"{PROMPT_PREAMBLE}\n\n### Title\nMy Title\n\n### Goal\nMy Goal",
        )
        self.assertNotIn("### Type", idea.build_prompt())


class IdeaKindChipRenderingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_typed_idea_renders_a_kind_chip(self):
        Idea.objects.create(project=self.project, title="Typed", kind=Idea.Kind.FIX)
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn('class="idea-kind idea-kind-fix"', html)
        self.assertIn(">Fix<", html)

    def test_other_kind_idea_renders_no_chip(self):
        Idea.objects.create(project=self.project, title="Untyped", kind=Idea.Kind.OTHER)
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertNotIn("idea-kind", html)

    def test_add_idea_form_offers_an_unticked_verification_box(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        content = self.client.get(url, headers={"hx-request": "true"}).content.decode()
        self.assertIn('name="needs_verification"', content)
        box = re.search(r'<input[^>]*name="needs_verification"[^>]*>', content).group(0)
        self.assertNotIn("checked", box)

    def test_only_an_idea_that_needs_verification_shows_the_chip(self):
        Idea.objects.create(project=self.project, title="Checked", needs_verification=True)
        Idea.objects.create(project=self.project, title="Unchecked")
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertEqual(html.count('class="idea-verify-chip"'), 1)

    def test_add_idea_form_offers_a_kind_select(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        content = response.content.decode()
        self.assertIn('name="kind"', content)


class IdeaPlanRenderingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_plan_html_renders_markdown_structure(self):
        idea = Idea.objects.create(
            project=self.project,
            title="T",
            plan="## Steps\n\n1. First\n2. Second\n",
        )
        html = idea.plan_html
        self.assertIn("<h2>Steps</h2>", html)
        self.assertIn("<li>First</li>", html)

    def test_plan_html_renders_fenced_code_blocks(self):
        idea = Idea.objects.create(
            project=self.project,
            title="T",
            plan="```python\nx = 1\n```",
        )
        self.assertIn("<pre><code", idea.plan_html)
        self.assertIn("x = 1", idea.plan_html)

    def test_plan_html_is_empty_when_plan_is_blank(self):
        idea = Idea.objects.create(project=self.project, title="T")
        self.assertEqual(idea.plan_html, "")

    def test_prompt_html_renders_markdown_structure(self):
        idea = Idea.objects.create(project=self.project, title="T", goal="Do the thing.")
        html = idea.prompt_html
        self.assertIn("<h3>Goal</h3>", html)
        self.assertIn("Do the thing.", html)

    def test_prompt_html_renders_fenced_code_blocks_from_a_field(self):
        idea = Idea.objects.create(
            project=self.project, title="T", notes="```python\nx = 1\n```"
        )
        self.assertIn("<pre><code", idea.prompt_html)
        self.assertIn("x = 1", idea.prompt_html)

    def test_prompt_html_is_never_blank_even_with_no_optional_fields(self):
        # Unlike plan_html, the prompt always has a Project and Title section, so it
        # never falls back to the empty string.
        idea = Idea.objects.create(project=self.project, title="T")
        self.assertNotEqual(idea.prompt_html, "")
        self.assertIn("### Title\nT", idea.build_prompt())


class LoosenMarkdownTests(TestCase):
    def test_list_after_paragraph_gets_a_blank_line(self):
        self.assertEqual(
            _loosen_markdown("Fixed three:\n- a\n- b"), "Fixed three:\n\n- a\n- b"
        )

    def test_existing_blank_line_is_untouched(self):
        text = "Fixed three:\n\n- a\n- b"
        self.assertEqual(_loosen_markdown(text), text)

    def test_consecutive_items_are_untouched(self):
        text = "- a\n- b\n- c"
        self.assertEqual(_loosen_markdown(text), text)

    def test_code_fence_contents_are_not_touched(self):
        text = "Example:\n```\nsome text\n- not a list\n```"
        self.assertEqual(_loosen_markdown(text), text)

    # Python-Markdown needs no space after "#", so a line opening with an idea
    # reference ("#177 confirmed ...") became an <h1> (seen on #184's plan, idea #199).
    def test_leading_idea_reference_is_escaped(self):
        self.assertEqual(_loosen_markdown("#177 done"), "\\#177 done")
        self.assertEqual(_loosen_markdown("- #177 done"), "- \\#177 done")

    def test_real_headings_are_untouched(self):
        text = "# Title\n## Sub\n### 3 things"
        self.assertEqual(_loosen_markdown(text), text)

    def test_idea_reference_in_code_fence_is_untouched(self):
        text = "```\n#177\n```"
        self.assertEqual(_loosen_markdown(text), text)


class IdeaReportLooseRenderingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_single_newline_becomes_br(self):
        idea = Idea.objects.create(
            project=self.project, title="T", report="Line one.\nLine two."
        )
        self.assertIn("Line one.<br", idea.report_html)

    def test_list_after_paragraph_renders_as_a_list(self):
        idea = Idea.objects.create(
            project=self.project, title="T", report="Fixed three:\n- a\n- b"
        )
        html = idea.report_html
        self.assertIn("<ul>", html)
        self.assertIn("<li>a</li>", html)

    # Plans are Claude-written too, in the same shape (idea #199: #184's plan opened
    # with "**Findings**" directly followed by "- ..." items, all run into one <p>).
    def test_plan_list_after_paragraph_renders_as_a_list(self):
        idea = Idea.objects.create(
            project=self.project, title="T", plan="**Findings**\n- a\n- b"
        )
        html = idea.plan_html
        self.assertIn("<ul>", html)
        self.assertIn("<li>a</li>", html)

    def test_plan_single_newline_becomes_br(self):
        idea = Idea.objects.create(
            project=self.project, title="T", plan="Line one.\nLine two."
        )
        self.assertIn("Line one.<br", idea.plan_html)

    def test_plan_list_item_opening_with_an_idea_reference_is_not_a_heading(self):
        idea = Idea.objects.create(
            project=self.project, title="T", plan="- #177 confirmed"
        )
        self.assertIn("<li>#177 confirmed</li>", idea.plan_html)

    def test_prompt_renders_a_list_after_paragraph_in_a_field(self):
        idea = Idea.objects.create(
            project=self.project, title="T", plan="**Findings**\n- a\n- b"
        )
        self.assertIn("<li>a</li>", idea.prompt_html)


class IdeaHtmlSanitizationTests(TestCase):
    """A stray unclosed tag mentioned in prose (e.g. "the <script> block runs...")
    must not turn into a real unclosed tag in the served page -- that swallows every
    sibling project on the overview page into script/RAWTEXT content. See idea #66."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_report_html_neutralizes_an_unclosed_script_tag(self):
        idea = Idea.objects.create(
            project=self.project, title="T", report="Before <script> After"
        )
        html = idea.report_html
        self.assertNotIn("<script", html)
        self.assertIn("Before", html)
        self.assertIn("After", html)

    def test_report_html_strips_an_event_handler_attribute(self):
        idea = Idea.objects.create(
            project=self.project,
            title="T",
            report='<img src="x" onerror="alert(1)">',
        )
        html = idea.report_html
        # <img> itself is allowed -- Markdown's own ![]() syntax needs it -- but the
        # handler attribute must never survive as something the browser would run.
        self.assertIn("<img", html)
        self.assertNotIn("onerror", html)

    def test_plan_html_neutralizes_an_unclosed_script_tag(self):
        idea = Idea.objects.create(
            project=self.project, title="T", plan="Before <script> After"
        )
        html = idea.plan_html
        self.assertNotIn("<script", html)
        self.assertIn("Before", html)
        self.assertIn("After", html)

    def test_prompt_html_neutralizes_an_unclosed_script_tag(self):
        idea = Idea.objects.create(
            project=self.project, title="T", notes="Before <script> After"
        )
        html = idea.prompt_html
        self.assertNotIn("<script", html)
        self.assertIn("Before", html)
        self.assertIn("After", html)


class IdeaMarkdownFormattingStillRendersTests(TestCase):
    """Neutralizing raw HTML (`_render_markdown` in core/models.py) must not touch
    Markdown's own syntax -- these pin down the constructs a report/plan actually
    uses that the sanitizing pass could plausibly break."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def _report_html(self, report):
        return Idea.objects.create(project=self.project, title="T", report=report).report_html

    def test_blockquote_still_renders(self):
        # ">" is also nh3/markdown-adjacent to raw-HTML escaping; a naive "escape
        # every special character" fix would turn this into a literal "&gt;" line.
        self.assertIn("<blockquote>", self._report_html("> quoted text"))

    def test_bold_and_italic_still_render(self):
        html = self._report_html("**bold** and *italic*")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)

    def test_inline_code_with_angle_bracket_still_renders_unescaped_once(self):
        # A double-escape bug here would show the reader a literal "&lt;" instead
        # of "<".
        html = self._report_html("Use `x < y` here")
        self.assertIn("<code>x &lt; y</code>", html)

    def test_fenced_code_block_with_angle_bracket_still_renders_unescaped_once(self):
        html = self._report_html("```python\nx = 1 < 2\n```")
        self.assertIn("<pre><code", html)
        self.assertIn("x = 1 &lt; 2", html)

    def test_table_still_renders(self):
        html = self._report_html("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", html)
        self.assertIn("<th>a</th>", html)
        self.assertIn("<td>1</td>", html)

    def test_link_still_renders_with_href(self):
        html = self._report_html("[text](http://example.com/a?b=1&c=2)")
        self.assertIn('href="http://example.com/a?b=1&amp;c=2"', html)
        self.assertIn(">text<", html)

    def test_markdown_image_syntax_still_renders_as_a_real_img_tag(self):
        # Unlike a raw <img> typed by hand, this comes from Markdown's own ![]()
        # syntax, so it must survive the tag allow-list.
        html = self._report_html("![alt text](http://example.com/pic.png)")
        self.assertIn("<img", html)
        self.assertIn('src="http://example.com/pic.png"', html)
        self.assertIn('alt="alt text"', html)

    def test_nested_list_still_renders(self):
        html = self._report_html("- a\n- b\n  - c")
        self.assertEqual(html.count("<li>"), 3)


class OverviewPageTests(TestCase):
    def setUp(self):
        self.idea_project = Project.objects.create(
            name="ProjA-idea", folder="proja", status=Project.Status.IDEA
        )
        self.defined_project = Project.objects.create(
            name="ProjB-defined", folder="projb", status=Project.Status.DEFINED
        )
        # Project with zero open ideas
        Project.objects.create(name="ProjC-empty", folder="projc", status=Project.Status.IMPLEMENTED)

        Idea.objects.create(
            project=self.idea_project,
            title="New Idea Title",
            status=Idea.Status.NEW,
            goal="Do the thing with Claude Code.",
        )
        Idea.objects.create(
            project=self.idea_project,
            title="Implemented Idea Title",
            status=Idea.Status.IMPLEMENTED,
        )

    def test_overview_page_returns_200(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_all_projects_appear_including_one_with_no_open_ideas(self):
        response = self.client.get(reverse("core:projects"))
        content = response.content.decode()
        self.assertIn("ProjA-idea", content)
        self.assertIn("ProjB-defined", content)
        self.assertIn("ProjC-empty", content)

    def test_closed_idea_hidden_open_idea_and_prompt_shown(self):
        response = self.client.get("/")
        content = expand_lazy(self.client, response.content.decode())
        self.assertNotIn("Implemented Idea Title", content)
        self.assertIn("New Idea Title", content)
        self.assertIn("### Goal", content)
        self.assertIn("Do the thing with Claude Code.", content)

    def test_projects_ordered_by_most_recent_activity(self):
        # setUp creates ProjA, ProjB, ProjC and then two ideas under ProjA. An
        # idea counts as activity on its project, so ProjA is the most recently
        # active one despite being created first; ProjC and ProjB follow in
        # reverse creation order.
        response = self.client.get(reverse("core:projects"))
        content = response.content.decode()
        self.assertLess(content.index("ProjA-idea"), content.index("ProjC-empty"))
        self.assertLess(content.index("ProjC-empty"), content.index("ProjB-defined"))

    def test_project_with_open_idea_sorts_before_more_recently_active_one_without(self):
        # ProjD's open idea is old; ProjE has no open idea (just a closed one) but
        # was touched far more recently. Pure recency ordering would put ProjE
        # first -- open-idea status must win regardless of recency.
        stale = self.idea_project.updated_at - timedelta(days=30)
        proj_open = Project.objects.create(
            name="ProjD-stale-open", folder="projd", status=Project.Status.IDEA
        )
        idea = Idea.objects.create(
            project=proj_open,
            title="Stale Open Idea",
            status=Idea.Status.NEW,
            goal="Old but still open.",
        )
        Project._base_manager.filter(pk=proj_open.pk).update(updated_at=stale)
        Idea._base_manager.filter(pk=idea.pk).update(updated_at=stale)

        proj_closed = Project.objects.create(
            name="ProjE-fresh-closed",
            folder="proje",
            status=Project.Status.IMPLEMENTED,
        )
        Idea.objects.create(
            project=proj_closed,
            title="Fresh Closed Idea",
            status=Idea.Status.IMPLEMENTED,
        )

        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertLess(content.index("ProjD-stale-open"), content.index("ProjE-fresh-closed"))

    def test_status_badge_shown_for_open_projects(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn('class="badge badge-idea"', content)
        self.assertIn('class="badge badge-defined"', content)

    def test_implemented_project_has_no_status_control(self):
        # "Implemented" carries no information and its only transition,
        # Discontinue, is rare enough to do in the admin, so the card shows
        # neither a badge nor a menu.
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertNotIn("badge-implemented", content)
        self.assertNotIn("Discontinue", content)

    def test_open_project_badge_keeps_its_visible_label(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn('class="badge badge-idea">Idea', content)
        
    def test_project_folder_not_shown_on_card(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertNotIn('class="folder"', content)

    def test_implemented_idea_shows_badge_and_reopen_menu(self):
        content = self.client.get("/?closed=1").content.decode()
        self.assertIn("Implemented Idea Title", content)
        self.assertIn('class="badge badge-new"', content)
        self.assertIn('class="badge badge-implemented"', content)
        self.assertIn("Reopen", content)

    def test_ideas_view_query_count(self):
        # One for the ideas with their projects (select_related), one for their
        # screenshots and one for their artifacts (prefetch_related, batched across
        # every idea) -- a verify idea's card shows its screenshots, and every
        # idea's card shows its artifacts, so both are fetched up front, not per card.
        Idea.objects.create(project=self.defined_project, title="Second", status=Idea.Status.VERIFY)
        with self.assertNumQueries(3):
            self.client.get("/")

    def test_admin_edit_links_present(self):
        idea = Idea.objects.get(title="New Idea Title")
        content = self.client.get("/").content.decode()
        self.assertIn(f"/admin/core/idea/{idea.pk}/change/", content)
        content = self.client.get(reverse("core:project_detail", args=["proja"])).content.decode()
        self.assertIn(f"/admin/core/project/{self.idea_project.pk}/change/", content)


class IdeaCardPlanTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_card_shows_collapsed_rendered_plan_when_a_plan_exists(self):
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            plan="## Steps\n\n1. First\n2. Second\n",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-plan"', content)
        self.assertIn("<h2>Steps</h2>", content)
        self.assertIn("<li>First</li>", content)

    def test_card_offers_one_toggle_defaulting_to_the_rendered_panel(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="## Steps\n")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'id="view-raw-plan-{idea.pk}"', content)
        self.assertIn("Rendered", content)
        self.assertIn("Raw", content)
        toggle_input = content.split(f'id="view-raw-plan-{idea.pk}"')[1].split(">")[0]
        self.assertNotIn("checked", toggle_input)

    def _plan_block(self, content):
        """Just the <details class="idea-plan"> fragment.

        Scoping matters: the prompt <pre> below it contains the same Markdown source,
        so an unscoped assertion passes whether or not a raw tab exists at all.
        """
        return content.split('<details class="idea-plan">')[1].split("</details>")[0]

    def test_raw_tab_carries_the_escaped_markdown_source(self):
        Idea.objects.create(project=self.project, title="T", plan="## Steps\n\n- a < b\n")
        block = self._plan_block(expand_lazy(self.client, self.client.get("/").content.decode()))
        self.assertIn("## Steps", block)
        self.assertIn("a &lt; b", block)

    def test_rendered_tab_shows_html_the_raw_tab_shows_source(self):
        Idea.objects.create(project=self.project, title="T", plan="## Steps\n")
        block = self._plan_block(expand_lazy(self.client, self.client.get("/").content.decode()))
        self.assertIn("<h2>Steps</h2>", block)
        self.assertIn("## Steps", block)

    def test_toggle_ids_are_per_idea_so_two_cards_do_not_share_state(self):
        first = Idea.objects.create(project=self.project, title="A", plan="one")
        second = Idea.objects.create(project=self.project, title="B", plan="two")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'id="view-raw-plan-{first.pk}"', content)
        self.assertIn(f'id="view-raw-plan-{second.pk}"', content)

    def test_plan_block_is_absent_when_the_idea_has_no_plan(self):
        Idea.objects.create(project=self.project, title="Unplanned Idea")
        content = self.client.get("/").content.decode()
        self.assertNotIn("idea-plan", content)


class PlanIsLongTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_short_plan_is_not_long(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="1. Do the thing")
        self.assertFalse(idea.plan_is_long)

    def test_plan_over_the_line_threshold_is_long(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="line\n" * 19)
        self.assertTrue(idea.plan_is_long)

    def test_plan_over_the_character_threshold_is_long(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="x" * 1201)
        self.assertTrue(idea.plan_is_long)

    def test_blank_plan_is_not_long(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="   ")
        self.assertFalse(idea.plan_is_long)


class PlannedIdeaCardTests(TestCase):
    """A planned idea with a plan swaps which block is open: the plan leads, and the
    prompt moves behind a closed <details>. Every other idea keeps today's markup."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_planned_idea_shows_the_plan_open_with_no_tabs_or_raw_panel(self):
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="## Steps\n\n1. First\n2. Second\n",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertNotIn('<details class="idea-plan">', content)
        self.assertNotIn("idea-plan-raw", content)
        self.assertNotIn("plan-view-", content)
        self.assertIn('class="idea-plan-block"', content)
        self.assertIn("<h2>Steps</h2>", content)
        self.assertIn("<li>First</li>", content)

    def test_planned_idea_wraps_the_prompt_in_a_closed_details(self):
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="1. Do it",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('<details class="idea-prompt">', content)
        self.assertIn("<summary>Prompt</summary>", content)
        self.assertIn('class="idea-content-block"', content)
        self.assertIn("### Title\nPlanned Idea", content)

    def test_short_planned_plan_has_no_clamp_markup(self):
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="1. Do it",
        )
        content = self.client.get("/").content.decode()
        self.assertNotIn("idea-plan-clamped", content)
        self.assertNotIn("Show more", content)

    def test_long_planned_plan_is_clamped_with_a_show_more_control(self):
        idea = Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="line\n" * 19,
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-plan-clamped"', content)
        self.assertIn(f'id="plan-more-{idea.pk}"', content)
        self.assertIn("Show more", content)
        self.assertIn("Show less", content)

    def test_new_idea_with_a_plan_keeps_todays_tabs_and_open_prompt(self):
        Idea.objects.create(
            project=self.project,
            title="New Idea",
            status=Idea.Status.NEW,
            plan="## Steps\n\n1. First\n",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('<details class="idea-plan">', content)
        self.assertIn(f'id="view-raw-plan-{Idea.objects.get(title="New Idea").pk}"', content)
        self.assertNotIn('<details class="idea-prompt">', content)
        self.assertNotIn('class="idea-plan-block"', content)
        self.assertIn("### Title\nNew Idea", content)

    def test_planned_idea_with_no_plan_is_unaffected(self):
        Idea.objects.create(
            project=self.project,
            title="Planned No Plan",
            status=Idea.Status.PLANNED,
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertNotIn('class="idea-plan-block"', content)
        self.assertNotIn('<details class="idea-prompt">', content)
        self.assertIn('class="idea-content-block"', content)


class ViewToggleTests(TestCase):
    """Both blocks on a card (plan and prompt) end up with the same shape: a rendered
    panel, a raw panel, and one checkbox toggle -- no more paired radio tabs."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_old_radio_tab_markup_is_gone(self):
        Idea.objects.create(project=self.project, title="T", plan="## Steps\n")
        content = self.client.get("/").content.decode()
        self.assertNotIn("plan-view-", content)
        self.assertNotIn("plan-tab", content)

    def test_prompt_block_has_a_toggle_with_rendered_and_raw_panels(self):
        idea = Idea.objects.create(project=self.project, title="T")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'id="view-raw-prompt-{idea.pk}"', content)
        self.assertIn('class="idea-prompt-body"', content)
        self.assertIn('class="idea-content"', content)

    def test_plan_block_has_a_toggle_with_rendered_and_raw_panels(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="## Steps\n")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'id="view-raw-plan-{idea.pk}"', content)
        self.assertIn('class="idea-plan-body"', content)
        self.assertIn('class="idea-plan-raw"', content)

    def test_raw_prompt_pre_is_always_present_for_copy(self):
        # The copy handler reads .idea-content's textContent, which works even while
        # CSS hides the element, so the raw <pre> must always be in the DOM.
        idea = Idea.objects.create(project=self.project, title="T")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'<pre class="idea-content">', content)
        self.assertIn(idea.build_prompt(), content)

    def test_prompt_toggle_ids_are_per_idea(self):
        first = Idea.objects.create(project=self.project, title="A")
        second = Idea.objects.create(project=self.project, title="B")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn(f'id="view-raw-prompt-{first.pk}"', content)
        self.assertIn(f'id="view-raw-prompt-{second.pk}"', content)

    def test_planned_idea_prompt_has_a_toggle_but_the_plan_does_not(self):
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="## Steps\n\n1. First\n",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        prompt_block = content.split('<details class="idea-prompt">')[1]
        self.assertIn("view-raw-toggle", prompt_block)
        plan_block = content.split('class="idea-plan-block"')[1].split(
            '<details class="idea-prompt">'
        )[0]
        self.assertNotIn("view-raw-toggle", plan_block)

    def test_planned_idea_prompt_head_drops_the_redundant_inner_label(self):
        # "Prompt" already reads as the <summary>; the inner .idea-content-label span
        # is only needed in the non-planned branch, where there is no summary.
        Idea.objects.create(
            project=self.project,
            title="Planned Idea",
            status=Idea.Status.PLANNED,
            plan="1. Do it",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        prompt_block = content.split('<details class="idea-prompt">')[1].split("</details>")[0]
        self.assertNotIn("idea-content-label", prompt_block)
        self.assertIn("copy-btn", prompt_block)
        self.assertIn("view-raw-toggle", prompt_block)

    def test_non_planned_idea_prompt_keeps_its_label(self):
        Idea.objects.create(project=self.project, title="T")
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-content-label">Prompt</span>', content)


class CollapsedIdeaCardTests(TestCase):
    """Every idea card collapses to its header row behind one toggle. New and closed
    ideas start collapsed; planned and verify ideas start expanded, since they carry
    something actionable (idea #287)."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_new_idea_card_is_collapsible(self):
        idea = Idea.objects.create(project=self.project, title="New Idea", status=Idea.Status.NEW)
        content = self.client.get("/").content.decode()
        self.assertIn("idea-collapsible", content)
        self.assertIn(f'id="idea-expand-{idea.pk}"', content)
        self.assertIn('class="idea-body"', content)

    def test_closed_idea_card_is_collapsible(self):
        idea = Idea.objects.create(
            project=self.project, title="Done", status=Idea.Status.IMPLEMENTED
        )
        content = self.client.get("/?closed=1").content.decode()
        self.assertIn("idea-collapsible", content)
        self.assertIn(f'id="idea-expand-{idea.pk}"', content)

    def test_planned_idea_card_is_collapsible_and_starts_expanded(self):
        idea = Idea.objects.create(
            project=self.project,
            title="Planned",
            status=Idea.Status.PLANNED,
            plan="1. Do it",
        )
        content = self.client.get("/").content.decode()
        self.assertIn("idea-collapsible", content)
        self.assertIn(f'id="idea-expand-{idea.pk}" class="idea-expand-toggle" checked>', content)

    def test_verify_idea_card_is_collapsible_and_starts_expanded(self):
        idea = Idea.objects.create(project=self.project, title="Verifying", status=Idea.Status.VERIFY)
        content = self.client.get("/").content.decode()
        self.assertIn("idea-collapsible", content)
        self.assertIn(f'id="idea-expand-{idea.pk}" class="idea-expand-toggle" checked>', content)

    def test_new_and_closed_idea_cards_start_collapsed(self):
        new = Idea.objects.create(project=self.project, title="New", status=Idea.Status.NEW)
        done = Idea.objects.create(
            project=self.project, title="Done", status=Idea.Status.IMPLEMENTED
        )
        content = self.client.get("/?closed=1").content.decode()
        for idea in (new, done):
            self.assertIn(f'id="idea-expand-{idea.pk}" class="idea-expand-toggle">', content)

    def test_planned_idea_body_still_leads_with_its_plan(self):
        Idea.objects.create(
            project=self.project, title="Planned", status=Idea.Status.PLANNED, plan="1. Do it"
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-plan-block"', content)

    def test_collapsed_card_still_contains_its_full_content_for_expansion(self):
        Idea.objects.create(
            project=self.project,
            title="New Idea",
            status=Idea.Status.NEW,
            goal="Do the thing.",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn("Do the thing.", content)

    def test_only_the_number_copy_button_carries_data_copy_text(self):
        # The header "<Project>: <Title>" Copy button is gone in every status (#287):
        # the #id button is the reference that gets copied.
        for status in (Idea.Status.NEW, Idea.Status.PLANNED, Idea.Status.VERIFY):
            Idea.objects.create(project=self.project, title=f"T {status}", status=status)
        content = self.client.get("/").content.decode()
        self.assertEqual(content.count("data-copy-text"), 3)
        self.assertNotIn(f'data-copy-text="{self.project.name}:', content)
        for idea in Idea.objects.all():
            self.assertIn(f'data-copy-text="#{idea.pk}"', content)

    def test_title_sits_inside_the_clickable_expand_label(self):
        # The caret alone is a tiny click target; the title text has to toggle the
        # same checkbox, so it must be nested inside .idea-expand-label, not just a
        # sibling of it.
        idea = Idea.objects.create(project=self.project, title="New Idea", status=Idea.Status.NEW)
        content = self.client.get("/").content.decode()
        label = content.split('class="idea-expand-label"')[1].split("</label>")[0]
        self.assertIn(f"<strong>{idea.title}</strong>", label)


class TransitionTablesTests(TestCase):
    def test_idea_transition_keys_and_targets_are_valid_statuses(self):
        valid_statuses = set(Idea.Status.values)
        for key, transitions in IDEA_TRANSITIONS.items():
            self.assertIn(key, valid_statuses)
            for transition in transitions:
                self.assertIn(transition.target, valid_statuses)

    def test_project_transition_keys_and_targets_are_valid_statuses(self):
        valid_statuses = set(Project.Status.values)
        for key, transitions in PROJECT_TRANSITIONS.items():
            self.assertIn(key, valid_statuses)
            for transition in transitions:
                self.assertIn(transition.target, valid_statuses)

    def test_closed_idea_statuses_offer_only_reopen(self):
        for status in CLOSED_IDEA_STATUSES:
            transitions = IDEA_TRANSITIONS[status]
            self.assertEqual(len(transitions), 1)
            self.assertEqual(transitions[0].label, "Reopen")
            self.assertEqual(transitions[0].target, Idea.Status.PLANNED)

    def test_discontinued_and_canceled_projects_offer_no_transitions(self):
        self.assertEqual(PROJECT_TRANSITIONS[Project.Status.DISCONTINUED], [])
        self.assertEqual(PROJECT_TRANSITIONS[Project.Status.CANCELED], [])


class ModelTransitionMethodsTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_idea_allowed_transitions_for_new(self):
        idea = Idea.objects.create(project=self.project, title="Idea", status=Idea.Status.NEW)
        targets = [t.target for t in idea.allowed_transitions()]
        self.assertEqual(targets, [Idea.Status.PLANNED, Idea.Status.ABANDONED])

    def test_idea_can_transition_to(self):
        idea = Idea.objects.create(project=self.project, title="Idea", status=Idea.Status.NEW)
        self.assertTrue(idea.can_transition_to(Idea.Status.PLANNED))
        self.assertFalse(idea.can_transition_to(Idea.Status.IMPLEMENTED))

    def test_project_allowed_transitions_for_idea_status(self):
        project = Project.objects.create(name="P2", folder="p2", status=Project.Status.IDEA)
        targets = [t.target for t in project.allowed_transitions()]
        self.assertEqual(targets, [Project.Status.DEFINED, Project.Status.CANCELED])

    def test_project_can_transition_to(self):
        project = Project.objects.create(name="P3", folder="p3", status=Project.Status.IDEA)
        self.assertTrue(project.can_transition_to(Project.Status.DEFINED))
        self.assertFalse(project.can_transition_to(Project.Status.IMPLEMENTED))

    def test_discontinued_project_has_no_allowed_transitions(self):
        project = Project.objects.create(
            name="P4", folder="p4", status=Project.Status.DISCONTINUED
        )
        self.assertEqual(project.allowed_transitions(), [])
        self.assertFalse(project.can_transition_to(Project.Status.IDEA))


class IdeaIsClosedTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_is_closed_true_for_implemented_and_abandoned(self):
        implemented = Idea.objects.create(
            project=self.project, title="A", status=Idea.Status.IMPLEMENTED
        )
        abandoned = Idea.objects.create(
            project=self.project, title="B", status=Idea.Status.ABANDONED
        )
        self.assertTrue(implemented.is_closed)
        self.assertTrue(abandoned.is_closed)

    def test_is_closed_false_for_new_and_planned(self):
        new = Idea.objects.create(project=self.project, title="C", status=Idea.Status.NEW)
        planned = Idea.objects.create(project=self.project, title="D", status=Idea.Status.PLANNED)
        self.assertFalse(new.is_closed)
        self.assertFalse(planned.is_closed)


class IdeaFormTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_rejects_blank_title(self):
        form = IdeaForm(data={"title": "", "goal": "", "context": "", "notes": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_accepts_blank_optional_fields(self):
        form = IdeaForm(data={"title": "My Idea", "goal": "", "context": "", "notes": ""})
        self.assertTrue(form.is_valid())

    def test_saves_the_plan_field(self):
        form = IdeaForm(data={"title": "My Idea", "plan": "1. Step one"})
        self.assertTrue(form.is_valid(), form.errors)
        idea = form.save(commit=False)
        self.assertEqual(idea.plan, "1. Step one")


class IdeaEditFormTests(TestCase):
    def test_has_a_project_select_without_an_empty_choice(self):
        Project.objects.create(name="Proj", folder="proj")
        form = IdeaEditForm()
        self.assertIn("project", form.fields)
        self.assertFalse(form.fields["project"].empty_label)
        self.assertEqual(list(form.fields)[0], "project")

    def test_moves_the_idea(self):
        source = Project.objects.create(name="Source", folder="source")
        target = Project.objects.create(name="Target", folder="target")
        idea = Idea.objects.create(project=source, title="Idea")
        form = IdeaEditForm(data={"project": target.pk, "title": "Idea"}, instance=idea)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().project_id, target.pk)


class IdeaCreateFormTests(TestCase):
    def test_has_no_plan_field(self):
        form = IdeaCreateForm()
        self.assertNotIn("plan", form.fields)

    def test_has_no_project_field(self):
        self.assertNotIn("project", IdeaCreateForm().fields)
        self.assertNotIn("project", IdeaForm().fields)

    def test_ignores_a_posted_plan(self):
        form = IdeaCreateForm(data={"title": "My Idea", "plan": "1. Step one"})
        self.assertTrue(form.is_valid(), form.errors)
        idea = form.save(commit=False)
        self.assertEqual(idea.plan, "")


class ProjectFormTests(TestCase):
    def test_round_trips_name_folder_description(self):
        form = ProjectForm(data={
            "name": "My Project",
            "folder": "my-project",
            "description": "Some description",
        })
        self.assertTrue(form.is_valid())
        project = form.save()
        project.refresh_from_db()
        self.assertEqual(project.name, "My Project")
        self.assertEqual(project.folder, "my-project")
        self.assertEqual(project.description, "Some description")


class IdeaTransitionViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="Idea", status=Idea.Status.NEW)

    def test_valid_transition_returns_200_and_card(self):
        url = reverse("core:idea_transition", args=[self.idea.pk])
        response = self.client.post(url, {"target": "planned"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.status, "planned")
        self.assertIn(f'id="idea-{self.idea.pk}"', response.content.decode())

    def test_forbidden_transition_returns_400_and_does_not_save(self):
        url = reverse("core:idea_transition", args=[self.idea.pk])
        response = self.client.post(url, {"target": "implemented"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 400)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.status, "new")

    def test_planned_idea_without_verification_offers_done(self):
        self.idea.status = Idea.Status.PLANNED
        self.idea.save()
        url = reverse("core:idea_transition", args=[self.idea.pk]) + "?closed=1"
        response = self.client.post(url, {"target": "verify"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.status, "implemented")

    def test_planned_card_without_verification_shows_done(self):
        self.idea.status = Idea.Status.PLANNED
        self.idea.save()
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn('value="implemented"', html)
        self.assertIn(">Done<", html)
        self.assertNotIn("Ready to verify", html)

    def test_get_on_transition_url_is_405(self):
        url = reverse("core:idea_transition", args=[self.idea.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_htmx_transition_to_closed_without_closed_param_returns_empty_body(self):
        self.idea.status = Idea.Status.VERIFY
        self.idea.save()
        url = reverse("core:idea_transition", args=[self.idea.pk])
        response = self.client.post(url, {"target": "implemented"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "")

    def test_htmx_transition_to_closed_with_closed_param_returns_card(self):
        self.idea.status = Idea.Status.VERIFY
        self.idea.save()
        url = reverse("core:idea_transition", args=[self.idea.pk]) + "?closed=1"
        response = self.client.post(url, {"target": "implemented"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="idea-{self.idea.pk}"', response.content.decode())

    def test_non_htmx_post_redirects_to_home(self):
        url = reverse("core:idea_transition", args=[self.idea.pk])
        response = self.client.post(url, {"target": "planned"})
        self.assertEqual(response.status_code, 302)


class IdeaEditViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="Idea", goal="Old goal")

    def test_get_htmx_returns_form_partial(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("<form", response.content.decode())

    def test_get_non_htmx_returns_full_standalone_page(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.content.decode().lower())

    def test_valid_post_saves_and_response_contains_rebuilt_prompt(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "title": "Idea", "goal": "New goal text", "context": "", "notes": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("New goal text", content)
        self.assertIn("### Goal", content)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.goal, "New goal text")

    def test_invalid_post_returns_422_and_does_not_save(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "title": "", "goal": "", "context": "", "notes": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 422)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.goal, "Old goal")

    def test_non_htmx_valid_post_redirects_to_home(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "title": "Idea", "goal": "New goal", "context": "", "notes": "",
        })
        self.assertEqual(response.status_code, 302)

    def test_edit_form_ticks_and_unticks_needs_verification(self):
        url = reverse("core:idea_edit", args=[self.idea.pk])
        data = {"title": "Idea", "goal": "Old goal", "context": "", "notes": ""}
        self.client.post(url, {**data, "needs_verification": "on"}, headers={"hx-request": "true"})
        self.idea.refresh_from_db()
        self.assertTrue(self.idea.needs_verification)
        self.client.post(url, data, headers={"hx-request": "true"})
        self.idea.refresh_from_db()
        self.assertFalse(self.idea.needs_verification)

    def test_form_offers_a_project_select(self):
        other = Project.objects.create(name="Other", folder="other")
        url = reverse("core:idea_edit", args=[self.idea.pk])
        content = self.client.get(url, headers={"hx-request": "true"}).content.decode()
        self.assertIn('name="project"', content)
        self.assertIn(f'<option value="{self.project.pk}" selected', content)
        self.assertIn(f'<option value="{other.pk}"', content)

    def test_htmx_move_on_a_list_re_renders_the_card_in_place(self):
        # On the Ideas view the card stays where it is, its project chip updated.
        other = Project.objects.create(name="Other", folder="other")
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "project": other.pk, "title": "Idea", "goal": "Old goal", "context": "", "notes": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'id="idea-{self.idea.pk}"', content)
        self.assertNotIn("hx-swap-oob", content)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, other.pk)

    def test_htmx_edit_without_move_swaps_only_the_row(self):
        Project.objects.create(name="Other", folder="other")
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "project": self.project.pk, "title": "Idea", "goal": "Old goal", "context": "", "notes": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'id="idea-{self.idea.pk}"', content)
        self.assertNotIn("hx-swap-oob", content)

    def test_htmx_move_on_a_project_page_removes_the_row(self):
        other = Project.objects.create(
            name="Other", folder="other", status=Project.Status.CANCELED
        )
        url = reverse("core:idea_edit", args=[self.idea.pk]) + "?view=project"
        response = self.client.post(url, {
            "project": other.pk, "title": "Idea", "goal": "Old goal", "context": "", "notes": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, other.pk)

    def test_non_htmx_move_redirects_and_lists_the_idea_under_the_new_project(self):
        other = Project.objects.create(name="Other", folder="other")
        url = reverse("core:idea_edit", args=[self.idea.pk])
        response = self.client.post(url, {
            "project": other.pk, "title": "Idea", "goal": "Old goal", "context": "", "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.project_id, other.pk)


class IdeaNewViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def _payload(self, **overrides):
        data = {
            "title": "Fresh Idea", "goal": "", "context": "", "notes": "",
        }
        data.update(overrides)
        return data

    def test_get_htmx_returns_blank_form_partial(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("<form", content)
        self.assertIn(f'id="new-idea-{self.project.pk}"', content)

    def test_blank_form_autofocuses_the_title(self):
        # htmx focuses [autofocus] in swapped-in content, so opening the form is
        # visible even when it lands below the fold.
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertIn("autofocus", response.content.decode())

    def test_get_non_htmx_returns_full_standalone_page(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.content.decode().lower())

    def test_valid_post_creates_idea_attached_to_project(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.post(
            url, self._payload(goal="Ship it"), headers={"hx-request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        idea = Idea.objects.get(title="Fresh Idea")
        self.assertEqual(idea.project, self.project)
        self.assertEqual(idea.status, Idea.Status.NEW)
        content = expand_lazy(self.client, response.content.decode())
        # The project page's whole idea list comes back, with the new idea in it.
        self.assertIn(f'id="project-ideas-{self.project.pk}"', content)
        self.assertIn("Fresh Idea", content)
        self.assertIn("Ship it", content)

    def test_htmx_response_contains_a_fresh_add_slot(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.post(url, self._payload(), headers={"hx-request": "true"})
        self.assertIn(f'id="new-idea-{self.project.pk}"', response.content.decode())

    def test_invalid_post_returns_422_and_creates_nothing(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.post(
            url, self._payload(title=""), headers={"hx-request": "true"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Idea.objects.count(), 0)

    def test_invalid_post_is_retargeted_at_the_form_slot(self):
        # The form posts at the idea list (a save re-renders the whole list), so
        # the form-only error response must be redirected to its own slot.
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.post(
            url, self._payload(title=""), headers={"hx-request": "true"}
        )
        self.assertEqual(response["HX-Retarget"], f"#new-idea-{self.project.pk}")
        self.assertEqual(response["HX-Reswap"], "outerHTML")

    def test_non_htmx_valid_post_redirects_home(self):
        url = reverse("core:idea_new", args=[self.project.pk])
        response = self.client.post(url, self._payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Idea.objects.count(), 1)

    def test_show_closed_is_preserved_through_the_form(self):
        url = reverse("core:idea_new", args=[self.project.pk]) + "?closed=1"
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertIn("closed=1", response.content.decode())

    def test_unknown_project_is_404(self):
        response = self.client.get(reverse("core:idea_new", args=[9999]))
        self.assertEqual(response.status_code, 404)


class IdeaCardViewTests(TestCase):
    def test_returns_read_only_card(self):
        project = Project.objects.create(name="Proj", folder="proj")
        idea = Idea.objects.create(project=project, title="Idea")
        url = reverse("core:idea_card", args=[idea.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="idea-{idea.pk}"', response.content.decode())
        self.assertNotIn("edit-form", response.content.decode())


class IdeaVerifyCardRenderingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def test_verify_idea_is_visible_without_closed_1(self):
        Idea.objects.create(
            project=self.project,
            title="Ready to check",
            status=Idea.Status.VERIFY,
            report="Implemented as planned. Nothing to report.",
        )
        content = self.client.get("/").content.decode()
        self.assertIn("Ready to check", content)

    def test_verify_idea_leads_with_the_rendered_report(self):
        Idea.objects.create(
            project=self.project,
            title="Ready to check",
            status=Idea.Status.VERIFY,
            report="## Notes\n\nAll good.",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-report-block"', content)
        self.assertIn("<h2>Notes</h2>", content)

    def test_verify_idea_shows_screenshot_thumbnails(self):
        idea = Idea.objects.create(
            project=self.project, title="Ready to check", status=Idea.Status.VERIFY
        )
        Screenshot.objects.create(
            idea=idea,
            image=SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png"),
            caption="Before",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-screenshots"', content)
        self.assertIn("Before", content)
        self.assertIn("shot", content)

    def test_verify_idea_badge_is_distinct_from_planned_and_implemented(self):
        Idea.objects.create(project=self.project, title="I", status=Idea.Status.VERIFY)
        content = self.client.get("/").content.decode()
        self.assertIn('class="badge badge-verify"', content)

    def test_report_on_a_non_verify_idea_sits_in_a_closed_details(self):
        Idea.objects.create(
            project=self.project,
            title="Back to planned",
            status=Idea.Status.PLANNED,
            plan="1. Do it",
            report="Earlier attempt notes.",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('<details class="idea-report">', content)
        self.assertIn("Earlier attempt notes.", content)

    def test_screenshots_stay_reachable_after_an_idea_leaves_verify(self):
        # Verifying an idea, or reopening it, must not strand its evidence: the strip
        # moves into the same closed <details> as the report, delete buttons and all.
        idea = Idea.objects.create(
            project=self.project, title="Back to planned", status=Idea.Status.PLANNED
        )
        Screenshot.objects.create(
            idea=idea,
            image=SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png"),
            caption="Before",
        )
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('<details class="idea-report">', content)
        self.assertIn('class="idea-screenshots"', content)
        self.assertIn("Before", content)

    def test_report_absent_when_idea_has_no_report(self):
        Idea.objects.create(project=self.project, title="No report", status=Idea.Status.NEW)
        content = self.client.get("/").content.decode()
        self.assertNotIn("idea-report", content)


class ScreenshotDeleteViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(
            project=self.project, title="Idea", status=Idea.Status.VERIFY
        )
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.screenshot = Screenshot.objects.create(
            idea=self.idea,
            image=SimpleUploadedFile("shot.png", b"pngbytes", content_type="image/png"),
        )

    def test_plain_post_deletes_and_redirects(self):
        url = reverse("core:screenshot_delete", args=[self.screenshot.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Screenshot.objects.filter(pk=self.screenshot.pk).exists())

    def test_htmx_post_deletes_and_returns_the_idea_card(self):
        url = reverse("core:screenshot_delete", args=[self.screenshot.pk])
        response = self.client.post(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Screenshot.objects.filter(pk=self.screenshot.pk).exists())
        self.assertIn(f'id="idea-{self.idea.pk}"', response.content.decode())
        self.assertNotIn('class="idea-screenshots"', response.content.decode())

    def test_get_is_405(self):
        url = reverse("core:screenshot_delete", args=[self.screenshot.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_missing_screenshot_is_404(self):
        url = reverse("core:screenshot_delete", args=[999999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)


class ArtifactWebViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(
            project=self.project, title="Idea", status=Idea.Status.NEW
        )
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def test_new_idea_card_shows_the_upload_form(self):
        content = expand_lazy(self.client, self.client.get("/").content.decode())
        self.assertIn('class="idea-artifact-upload-form"', content)

    def test_plain_post_uploads_and_redirects(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        url = reverse("core:artifact_upload", args=[self.idea.pk])
        response = self.client.post(url, {"file": upload, "caption": "Spec"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Artifact.objects.filter(idea=self.idea).count(), 1)

    def test_htmx_post_uploads_and_returns_the_idea_card(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        url = reverse("core:artifact_upload", args=[self.idea.pk])
        response = self.client.post(url, {"file": upload}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'id="idea-{self.idea.pk}"', content)
        self.assertIn("notes.md", content)

    def test_htmx_post_with_bad_extension_is_422_with_error(self):
        upload = SimpleUploadedFile("virus.exe", b"whatever", content_type="application/octet-stream")
        url = reverse("core:artifact_upload", args=[self.idea.pk])
        response = self.client.post(url, {"file": upload}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 422)
        self.assertIn('class="idea-artifact-error"', response.content.decode())

    def test_missing_idea_is_404(self):
        upload = SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown")
        url = reverse("core:artifact_upload", args=[999999])
        response = self.client.post(url, {"file": upload})
        self.assertEqual(response.status_code, 404)

    def test_get_upload_is_405(self):
        url = reverse("core:artifact_upload", args=[self.idea.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_delete_removes_the_artifact(self):
        artifact = Artifact.objects.create(
            idea=self.idea,
            file=SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown"),
        )
        url = reverse("core:artifact_delete", args=[artifact.pk])
        response = self.client.post(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Artifact.objects.filter(pk=artifact.pk).exists())

    def test_missing_artifact_delete_is_404(self):
        url = reverse("core:artifact_delete", args=[999999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_download_text_artifact_is_inline(self):
        artifact = Artifact.objects.create(
            idea=self.idea,
            content_type="text/markdown",
            file=SimpleUploadedFile("notes.md", b"# Notes", content_type="text/markdown"),
        )
        url = reverse("core:artifact_download", args=[artifact.pk]) + "?raw"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertEqual(b"".join(response.streaming_content), b"# Notes")

    def test_download_binary_artifact_is_an_attachment(self):
        artifact = Artifact.objects.create(
            idea=self.idea,
            content_type="application/vnd.android.package-archive",
            file=SimpleUploadedFile("app.apk", b"fake-apk-bytes", content_type="application/octet-stream"),
        )
        url = reverse("core:artifact_download", args=[artifact.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("app.apk", response["Content-Disposition"])

    def test_missing_artifact_download_is_404(self):
        url = reverse("core:artifact_download", args=[999999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)



class SandboxedHtmlArtifactTests(TestCase):
    """An uploaded .html artifact must never run on claude-manager's own origin: both
    routes that serve it -- /media/ and the card's download link -- carry the sandbox
    CSP, and every media response carries nosniff so no other type is sniffed as HTML."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.idea = Idea.objects.create(project=self.project, title="Idea", status=Idea.Status.NEW)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

    def _upload(self, name, body=b"<script>fetch('/api/ideas/')</script>"):
        response = self.client.post(
            reverse("core:artifact_upload", args=[self.idea.pk]),
            {"file": SimpleUploadedFile(name, body, content_type="text/html")},
        )
        self.assertEqual(response.status_code, 302)
        return Artifact.objects.get(idea=self.idea, file__endswith=name)

    def _assert_sandboxed(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Security-Policy"], SANDBOXED_HTML_CSP)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_html_and_htm_are_accepted(self):
        self._upload("report.html")
        self._upload("old.htm")
        self.assertEqual(Artifact.objects.filter(idea=self.idea).count(), 2)

    def test_csp_starts_with_sandbox_and_blocks_external_loads(self):
        self.assertTrue(SANDBOXED_HTML_CSP.startswith("sandbox allow-scripts;"))
        self.assertNotIn("allow-same-origin", SANDBOXED_HTML_CSP)
        self.assertIn("default-src 'none'", SANDBOXED_HTML_CSP)

    def test_media_url_is_sandboxed(self):
        artifact = self._upload("report.html")
        self._assert_sandboxed(self.client.get(artifact.file.url))

    def test_uppercase_extension_is_sandboxed_too(self):
        artifact = self._upload("REPORT.HTML")
        self._assert_sandboxed(self.client.get(artifact.file.url))
        self._assert_sandboxed(self.client.get(reverse("core:artifact_download", args=[artifact.pk])))

    def test_download_link_opens_inline_and_sandboxed(self):
        artifact = self._upload("report.html")
        response = self.client.get(reverse("core:artifact_download", args=[artifact.pk]))
        self._assert_sandboxed(response)
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertTrue(response["Content-Type"].startswith("text/html"))

    def test_other_media_gets_nosniff_but_no_csp(self):
        upload = SimpleUploadedFile("notes.txt", b"# Notes", content_type="text/plain")
        self.client.post(reverse("core:artifact_upload", args=[self.idea.pk]), {"file": upload})
        artifact = Artifact.objects.get(idea=self.idea)
        for url in (artifact.file.url, reverse("core:artifact_download", args=[artifact.pk])):
            response = self.client.get(url)
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertNotIn("Content-Security-Policy", response)

    def test_html_is_not_readable_as_text(self):
        self.assertFalse(self._upload("report.html").is_text)

class ProjectTransitionViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj", status=Project.Status.IDEA)

    def test_valid_transition_returns_200_and_card(self):
        url = reverse("core:project_transition", args=[self.project.pk])
        response = self.client.post(url, {"target": "defined"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "defined")
        self.assertIn(f'id="project-{self.project.pk}"', response.content.decode())

    def test_forbidden_transition_returns_400_and_does_not_save(self):
        url = reverse("core:project_transition", args=[self.project.pk])
        response = self.client.post(url, {"target": "implemented"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "idea")

    def test_get_is_405(self):
        url = reverse("core:project_transition", args=[self.project.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_non_htmx_post_redirects(self):
        url = reverse("core:project_transition", args=[self.project.pk])
        response = self.client.post(url, {"target": "defined"})
        self.assertEqual(response.status_code, 302)


class ProjectEditViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_valid_post_saves_and_returns_card(self):
        url = reverse("core:project_edit", args=[self.project.pk])
        response = self.client.post(url, {
            "name": "New Name", "folder": "proj", "description": "desc",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "New Name")

    def test_invalid_post_returns_422_and_does_not_save(self):
        url = reverse("core:project_edit", args=[self.project.pk])
        response = self.client.post(url, {
            "name": "", "folder": "", "description": "",
        }, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 422)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Proj")

    def test_get_non_htmx_returns_full_page(self):
        url = reverse("core:project_edit", args=[self.project.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.content.decode().lower())


class ProjectCardViewTests(TestCase):
    def test_returns_read_only_card(self):
        project = Project.objects.create(name="Proj", folder="proj")
        url = reverse("core:project_card", args=[project.pk])
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="project-{project.pk}"', response.content.decode())
        self.assertNotIn("edit-form", response.content.decode())


class ClosedProjectVisibilityTests(TestCase):
    """A discontinued or canceled project is closed like a closed idea: hidden
    from the Projects view by default, shown under ?closed=1. Implemented stays
    visible either way -- it is a project's normal end state, not a closed one."""

    def setUp(self):
        self.open_project = Project.objects.create(name="Open Proj", folder="open-proj")
        self.implemented_project = Project.objects.create(
            name="Implemented Proj", folder="impl-proj", status=Project.Status.IMPLEMENTED
        )
        self.discontinued_project = Project.objects.create(
            name="Discontinued Proj", folder="disc-proj", status=Project.Status.DISCONTINUED
        )
        self.canceled_project = Project.objects.create(
            name="Canceled Proj", folder="canc-proj", status=Project.Status.CANCELED
        )

    def test_discontinued_and_canceled_hidden_by_default(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn("Open Proj", content)
        self.assertIn("Implemented Proj", content)
        self.assertNotIn("Discontinued Proj", content)
        self.assertNotIn("Canceled Proj", content)

    def test_discontinued_and_canceled_shown_with_closed_param(self):
        content = self.client.get(reverse("core:projects") + "?closed=1").content.decode()
        self.assertIn("Open Proj", content)
        self.assertIn("Implemented Proj", content)
        self.assertIn("Discontinued Proj", content)
        self.assertIn("Canceled Proj", content)

    def test_htmx_transition_to_discontinued_without_closed_param_returns_empty_body(self):
        url = reverse("core:project_transition", args=[self.implemented_project.pk])
        response = self.client.post(
            url, {"target": "discontinued"}, headers={"hx-request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "")

    def test_htmx_transition_to_discontinued_with_closed_param_returns_card(self):
        url = reverse("core:project_transition", args=[self.implemented_project.pk]) + "?closed=1"
        response = self.client.post(
            url, {"target": "discontinued"}, headers={"hx-request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="project-{self.implemented_project.pk}"', response.content.decode())


class HomePageInteractiveTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")
        self.new_idea = Idea.objects.create(
            project=self.project, title="New Idea", status=Idea.Status.NEW
        )
        self.closed_idea = Idea.objects.create(
            project=self.project, title="Closed Idea", status=Idea.Status.IMPLEMENTED
        )

    def test_closed_idea_hidden_by_default_and_shown_with_query_param(self):
        response = self.client.get("/")
        self.assertNotIn("Closed Idea", response.content.decode())
        response = self.client.get("/?closed=1")
        self.assertIn("Closed Idea", response.content.decode())

    def test_closed_toggle_link_rendered_once_for_the_page(self):
        content = self.client.get("/").content.decode()
        self.assertEqual(content.count('class="closed-toggle"'), 1)
        self.assertIn("Show closed", content)

        closed_content = self.client.get("/?closed=1").content.decode()
        self.assertEqual(closed_content.count('class="closed-toggle"'), 1)
        self.assertIn("Hide closed", closed_content)

    def test_new_idea_shows_plan_and_abandon_but_not_reopen(self):
        response = self.client.get("/")
        content = response.content.decode()
        self.assertIn("Plan", content)
        self.assertIn("Abandon", content)
        self.assertNotIn("Reopen", content)

    def test_add_idea_link_opens_the_form_without_a_request(self):
        # No hx-get any more: opening the form clones the <template> below via
        # a click handler instead of fetching it from the server.
        content = self.client.get(reverse("core:project_detail", args=["proj"])).content.decode()
        marker = "add-idea-link"
        self.assertIn(f'class="admin-link add-idea-link" data-project-pk="{self.project.pk}"', content)
        tag_start = content.rfind("<a ", 0, content.index(marker))
        tag_end = content.index(">", tag_start)
        self.assertNotIn("hx-get", content[tag_start:tag_end])
        self.assertIn('<template id="new-idea-form">', content)

    def test_add_idea_link_and_swap_slot_rendered_per_project(self):
        content = self.client.get(reverse("core:project_detail", args=["proj"])).content.decode()
        self.assertIn(reverse("core:idea_new", args=[self.project.pk]), content)
        self.assertIn("Add idea", content)
        self.assertEqual(content.count(f'id="new-idea-{self.project.pk}"'), 1)

    def test_project_with_no_ideas_still_has_an_add_slot(self):
        empty = Project.objects.create(name="Empty", folder="empty")
        content = self.client.get(reverse("core:project_detail", args=["empty"])).content.decode()
        self.assertIn(f'id="new-idea-{empty.pk}"', content)

    def test_no_template_comment_leaks_into_the_rendered_page(self):
        # Django's {# #} is single-line only: a multiline one is not a comment at
        # all and renders verbatim. Multiline needs {% comment %}.
        content = self.client.get("/").content.decode()
        self.assertNotIn("{#", content)
        self.assertNotIn("#}", content)

    def test_add_slot_precedes_the_ideas_it_shares_a_list_with(self):
        # The form opens into the slot. At the end of the list it lands off-screen
        # and the click looks like a no-op, so the slot has to come first.
        content = self.client.get(reverse("core:project_detail", args=["proj"])).content.decode()
        self.assertLess(
            content.index(f'id="new-idea-{self.project.pk}"'),
            content.index(f'id="idea-{self.new_idea.pk}"'),
        )


class StatusMenuTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_idea_status_menu_lists_one_entry_per_allowed_transition(self):
        Idea.objects.create(project=self.project, title="Fresh Idea", status=Idea.Status.NEW)
        response = self.client.get("/")
        content = response.content.decode()
        self.assertIn('<details class="status-menu">', content)
        for transition in IDEA_TRANSITIONS[Idea.Status.NEW]:
            self.assertIn(transition.label, content)
            self.assertIn(f'value="{transition.target}"', content)

    def test_implemented_idea_badge_keeps_its_visible_label(self):
        # Only implemented projects drop the badge: an implemented idea is hidden
        # by default, so when it is on screen its status is worth reading.
        Idea.objects.create(project=self.project, title="Done Idea", status=Idea.Status.IMPLEMENTED)
        content = self.client.get("/?closed=1").content.decode()
        self.assertIn('class="badge badge-implemented">Implemented', content)

    def test_badge_rendered_for_implemented_idea(self):
        # The old guard hid the badge for "implemented" ideas; the badge is
        # now the menu control, so it must always render.
        Idea.objects.create(project=self.project, title="Done Idea", status=Idea.Status.IMPLEMENTED)
        content = self.client.get("/?closed=1").content.decode()
        self.assertIn('class="badge badge-implemented"', content)

    def test_project_with_no_transitions_renders_plain_badge_no_menu(self):
        # A discontinued project is closed like a closed idea, so its card is
        # only rendered under ?closed=1 -- see ClosedProjectVisibilityTests.
        discontinued = Project.objects.create(
            name="Discontinued Proj", folder="disc", status=Project.Status.DISCONTINUED
        )
        url = reverse("core:project_card", args=[discontinued.pk]) + "?closed=1"
        content = self.client.get(url, headers={"hx-request": "true"}).content.decode()
        self.assertIn('<span class="badge badge-discontinued">Discontinued</span>', content)
        self.assertNotIn("status-menu", content)
        self.assertNotIn("<details", content)

    def test_canceled_project_renders_plain_badge_no_menu(self):
        canceled = Project.objects.create(
            name="Canceled Proj", folder="canc", status=Project.Status.CANCELED
        )
        url = reverse("core:project_card", args=[canceled.pk]) + "?closed=1"
        content = self.client.get(url, headers={"hx-request": "true"}).content.decode()
        self.assertIn('<span class="badge badge-canceled">Canceled</span>', content)
        self.assertNotIn("status-menu", content)

    def test_menu_action_preserves_closed_query_string(self):
        idea = Idea.objects.create(project=self.project, title="Fresh Idea", status=Idea.Status.NEW)
        url = reverse("core:idea_transition", args=[idea.pk])

        plain = self.client.get("/").content.decode()
        self.assertIn(f'action="{url}"', plain)
        self.assertNotIn(f'action="{url}?closed=1"', plain)

        closed = self.client.get("/?closed=1").content.decode()
        self.assertIn(f'action="{url}?closed=1"', closed)
        self.assertIn(f'hx-post="{url}?closed=1"', closed)

    def test_htmx_transition_response_card_contains_status_menu(self):
        idea = Idea.objects.create(project=self.project, title="Idea", status=Idea.Status.NEW)
        url = reverse("core:idea_transition", args=[idea.pk])
        response = self.client.post(url, {"target": "planned"}, headers={"hx-request": "true"})
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="idea-{idea.pk}"', content)
        self.assertIn('<details class="status-menu">', content)
        self.assertIn('class="badge badge-planned"', content)


class PWATests(TestCase):
    def test_service_worker_served_correctly(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertIn("addEventListener", response.content.decode())

    def test_service_worker_precaches_offline_page(self):
        response = self.client.get("/sw.js")
        offline_url = reverse("core:offline")
        self.assertIn(offline_url, response.content.decode())

    def test_service_worker_precaches_htmx(self):
        response = self.client.get("/sw.js")
        self.assertIn("vendor/htmx.min.js", response.content.decode())

    def test_service_worker_caches_static_when_not_debug(self):
        # Production hashes static filenames, so cache-first is safe there.
        with override_settings(DEBUG=False):
            content = self.client.get("/sw.js").content.decode()
        self.assertIn("/static/css/style.css", content)
        self.assertIn('startsWith("/static/")', content)

    def test_service_worker_leaves_static_uncached_in_debug(self):
        # Dev serves unhashed filenames; caching them pins stale CSS/JS.
        with override_settings(DEBUG=True):
            content = self.client.get("/sw.js").content.decode()
        self.assertNotIn("/static/css/style.css", content)
        self.assertNotIn("vendor/htmx.min.js", content)
        self.assertNotIn('startsWith("/static/")', content)
        # The offline fallback is still precached in dev.
        self.assertIn(reverse("core:offline"), content)

    def test_offline_page(self):
        response = self.client.get(reverse("core:offline"))
        self.assertEqual(response.status_code, 200)

    def test_home_page_links_manifest_and_service_worker(self):
        response = self.client.get(reverse("core:home"))
        content = response.content.decode()
        self.assertIn("manifest.webmanifest", content)
        self.assertIn("/sw.js", content)

    def test_manifest_is_valid(self):
        import json

        from django.conf import settings

        manifest_path = settings.BASE_DIR / "static" / "manifest.webmanifest"
        with open(manifest_path) as f:
            manifest = json.load(f)

        self.assertIn("name", manifest)
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")

        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)


class ProjectFilterTests(TestCase):
    """The filter box on the Projects view (#384)."""

    def setUp(self):
        self.weather = Project.objects.create(name="Weather", folder="weather")
        self.manager = Project.objects.create(name="Claude Idea Manager", folder="claude-manager")
        Idea.objects.create(
            project=self.weather, title="Closed Weather Idea", status=Idea.Status.IMPLEMENTED
        )

    def _project_tag(self, content, project):
        # The card is always rendered now (the filter hides it with the plain
        # `hidden` attribute instead of leaving it out), so "does this project
        # match" means "is its row tag missing `hidden`", not "is its
        # name in the page at all".
        start = content.index(f'id="project-{project.pk}"')
        end = content.index(">", start)
        return content[start:end]

    def test_no_query_shows_every_project(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertNotIn("hidden", self._project_tag(content, self.weather))
        self.assertNotIn("hidden", self._project_tag(content, self.manager))

    def test_filter_matches_the_name(self):
        content = self.client.get(reverse("core:projects"), {"q": "Idea Man"}).content.decode()
        self.assertNotIn("hidden", self._project_tag(content, self.manager))
        self.assertIn("hidden", self._project_tag(content, self.weather))

    def test_filter_matches_the_folder(self):
        # The folder never appears on the card, but it is the name used in
        # prompts and paths, so it has to be searchable.
        content = self.client.get(reverse("core:projects"), {"q": "claude-manager"}).content.decode()
        self.assertNotIn("hidden", self._project_tag(content, self.manager))
        self.assertIn("hidden", self._project_tag(content, self.weather))

    def test_filter_is_case_insensitive(self):
        content = self.client.get(reverse("core:projects"), {"q": "wEaThEr"}).content.decode()
        self.assertNotIn("hidden", self._project_tag(content, self.weather))
        self.assertIn("hidden", self._project_tag(content, self.manager))

    def test_whitespace_only_query_is_not_a_filter(self):
        content = self.client.get(reverse("core:projects"), {"q": "   "}).content.decode()
        self.assertIn("Weather", content)
        self.assertIn("Claude Idea Manager", content)

    def test_no_match_says_so_instead_of_offering_the_admin(self):
        content = self.client.get(reverse("core:projects"), {"q": "nothing-matches-this"}).content.decode()
        self.assertIn("No projects match", content)
        self.assertNotIn("No projects yet", content)

    def test_filter_box_carries_the_current_query_back(self):
        content = self.client.get(reverse("core:projects"), {"q": "weather"}).content.decode()
        self.assertIn('name="q" value="weather"', content)

    def test_htmx_partial_carries_the_closed_toggle_with_the_query(self):
        # The toggle is swapped along with the cards; left outside the swap it
        # would keep pointing at the unfiltered page after a live filter.
        content = self.client.get(reverse("core:projects"), {"q": "weather"}, HTTP_HX_REQUEST="true").content.decode()
        self.assertIn("Show closed", content)
        self.assertIn("q=weather", content)

    def test_hx_request_header_makes_no_difference(self):
        # There is no HX-Request branch in `projects`: filtering happens
        # client-side, so an htmx request still gets the full page, never a
        # partial-only response.
        content = self.client.get(reverse("core:projects"), {"q": "weather"}, HTTP_HX_REQUEST="true").content.decode()
        self.assertIn("<h1>Projects</h1>", content)
        self.assertIn("project-filter", content)
        self.assertIn(f'id="project-{self.weather.pk}"', content)

    def test_clear_link_renders_with_no_query(self):
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn('class="filter-clear"', content)
        self.assertIn(f'href="{reverse("core:projects")}"', content)

    def test_clear_link_renders_with_a_set_query(self):
        content = self.client.get(reverse("core:projects"), {"q": "weather"}).content.decode()
        self.assertIn('class="filter-clear"', content)
        self.assertIn(f'href="{reverse("core:projects")}"', content)

    def test_clear_link_drops_the_query_but_keeps_closed(self):
        content = self.client.get(reverse("core:projects"), {"q": "weather", "closed": "1"}).content.decode()
        clear_start = content.index('class="filter-clear"')
        clear_end = content.index(">", clear_start)
        clear_tag = content[clear_start:clear_end]
        self.assertIn(f'href="{reverse("core:projects")}?closed=1"', clear_tag)
        self.assertNotIn("q=weather", clear_tag)

    def test_filter_and_closed_combine(self):
        content = self.client.get(reverse("core:projects"), {"q": "weather", "closed": "1"}).content.decode()
        self.assertNotIn("hidden", self._project_tag(content, self.weather))
        self.assertIn("hidden", self._project_tag(content, self.manager))
        # The toggle back out of ?closed=1 must not drop the filter.
        self.assertIn("q=weather", content)
        # ...and the JS-off submit must not drop the toggle.
        self.assertIn('<input type="hidden" name="closed" value="1">', content)


class IdeaEditFormWithoutProjectTests(TestCase):
    def test_a_post_without_project_keeps_the_current_one(self):
        source = Project.objects.create(name="Source", folder="source")
        Project.objects.create(name="Other", folder="other")
        idea = Idea.objects.create(project=source, title="Idea")
        form = IdeaEditForm(data={"title": "Renamed"}, instance=idea)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().project_id, source.pk)


class ProjectRowCountsTests(TestCase):
    """A project's row on the Projects view counts its open ideas per status, the
    ones waiting on the user first (#384; the folded overview card did before)."""

    def setUp(self):
        self.busy = Project.objects.create(name="Busy", folder="busy")
        for status, n in (
            (Idea.Status.NEW, 3),
            (Idea.Status.PLANNED, 2),
            (Idea.Status.VERIFY, 1),
            (Idea.Status.IMPLEMENTED, 4),
            (Idea.Status.ABANDONED, 1),
        ):
            for i in range(n):
                Idea.objects.create(project=self.busy, title=f"{status} {i}", status=status)

    def test_open_idea_counts_ordered_verify_planned_new(self):
        self.assertEqual(
            self.busy.open_idea_counts, [("verify", 1), ("planned", 2), ("new", 3)]
        )
        content = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn("1 verify · 2 planned · 3 new", content)

    def test_counts_ignore_closed_ideas_under_show_closed(self):
        content = self.client.get(reverse("core:projects") + "?closed=1").content.decode()
        self.assertIn("1 verify · 2 planned · 3 new", content)
        self.assertNotIn("implemented ·", content)

    def test_htmx_row_rerender_keeps_counts(self):
        url = reverse("core:project_card", args=[self.busy.pk]) + "?view=projects"
        content = self.client.get(url, headers={"hx-request": "true"}).content.decode()
        self.assertIn('class="project-row', content)
        self.assertIn("1 verify · 2 planned · 3 new", content)


class LazyIdeaBodyTests(TestCase):
    """The overview ships only each card's header row; bodies and prompts are fetched
    when they come into view, which took the real page from 2-5 MB down (#269)."""

    def setUp(self):
        self.project = Project.objects.create(name="Proj", folder="proj")

    def test_overview_ships_no_plan_report_or_prompt(self):
        Idea.objects.create(project=self.project, title="New", plan="NEW-PLAN-TEXT")
        Idea.objects.create(
            project=self.project, title="Planned", status=Idea.Status.PLANNED, plan="PLANNED-PLAN-TEXT"
        )
        Idea.objects.create(
            project=self.project, title="Verify", status=Idea.Status.VERIFY, report="REPORT-TEXT"
        )
        content = self.client.get("/").content.decode()
        for text in ("NEW-PLAN-TEXT", "PLANNED-PLAN-TEXT", "REPORT-TEXT", 'class="idea-content"'):
            self.assertNotIn(text, content)
        self.assertEqual(content.count('class="idea-lazy"'), 3)
        for title in ("New", "Planned", "Verify"):
            self.assertIn(f"<strong>{title}</strong>", content)

    def test_stand_in_loads_once_when_visible(self):
        idea = Idea.objects.create(project=self.project, title="T")
        content = self.client.get("/").content.decode()
        self.assertIn(f'hx-get="{reverse("core:idea_body", args=[idea.pk])}"', content)
        self.assertIn('hx-trigger="intersect once"', content)

    def test_stand_in_keeps_the_closed_flag(self):
        idea = Idea.objects.create(project=self.project, title="T", status=Idea.Status.ABANDONED)
        content = self.client.get("/?closed=1").content.decode()
        self.assertIn(f'hx-get="{reverse("core:idea_body", args=[idea.pk])}?closed=1"', content)

    def test_body_of_a_new_idea_is_the_default_body_with_the_prompt_inline(self):
        idea = Idea.objects.create(project=self.project, title="T", plan="## Steps\n")
        content = self.client.get(reverse("core:idea_body", args=[idea.pk])).content.decode()
        self.assertIn('<details class="idea-plan">', content)
        self.assertIn('class="idea-content-label">Prompt</span>', content)
        self.assertIn(idea.build_prompt(), content)

    def test_body_of_a_planned_idea_leads_with_the_plan_and_defers_the_prompt(self):
        idea = Idea.objects.create(
            project=self.project, title="T", status=Idea.Status.PLANNED, plan="1. Do it"
        )
        content = self.client.get(reverse("core:idea_body", args=[idea.pk])).content.decode()
        self.assertIn('class="idea-plan-block"', content)
        prompt = content.split('<details class="idea-prompt">')[1].split("</details>")[0]
        self.assertIn(f'hx-get="{reverse("core:idea_prompt", args=[idea.pk])}"', prompt)
        self.assertNotIn('class="idea-content"', prompt)

    def test_prompt_endpoint_serves_the_unlabelled_prompt_block(self):
        idea = Idea.objects.create(project=self.project, title="T", goal="G")
        content = self.client.get(reverse("core:idea_prompt", args=[idea.pk])).content.decode()
        self.assertIn('<pre class="idea-content">', content)
        self.assertIn("copy-btn", content)
        self.assertNotIn("idea-content-label", content)

    def test_htmx_swapped_card_renders_its_body_inline(self):
        # A transition re-renders the card the user is looking at -- no second fetch.
        idea = Idea.objects.create(project=self.project, title="T", plan="PLAN-TEXT")
        response = self.client.post(
            reverse("core:idea_transition", args=[idea.pk]),
            {"target": Idea.Status.PLANNED},
            headers={"hx-request": "true"},
        )
        content = response.content.decode()
        self.assertIn("PLAN-TEXT", content)
        self.assertNotIn(reverse("core:idea_body", args=[idea.pk]), content)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class BackgroundUpdatesTests(TestCase):
    """GET /changes/ -- what static/js/board.js polls to refresh cards in place
    (#322), scoped per page (#384)."""

    def setUp(self):
        self.project = Project.objects.create(name="Polled", folder="polled", status=Project.Status.DEFINED)
        self.other = Project.objects.create(name="Other", folder="other", status=Project.Status.IMPLEMENTED)
        self.gone = Project.objects.create(name="Gone", folder="gone", status=Project.Status.CANCELED)
        self.open_idea = Idea.objects.create(project=self.project, title="Open one")
        self.closed_idea = Idea.objects.create(
            project=self.project, title="Closed one", status=Idea.Status.ABANDONED
        )

    def changes(self, query="", **headers):
        return self.client.get(reverse("core:changes") + query, headers=headers)

    def test_ideas_view_scope_lists_what_the_ideas_view_shows(self):
        data = self.changes().json()
        self.assertEqual([row[0] for row in data["ideas"]], [self.open_idea.pk])
        self.assertEqual(data["ideas"][0][1], self.project.pk)
        self.assertEqual(data["ideas"][0][2], "new")
        self.assertEqual(data["projects"], {})

    def test_closed_lists_closed_ideas_and_projects_too(self):
        data = self.changes("?closed=1").json()
        self.assertEqual({row[0] for row in data["ideas"]}, {self.open_idea.pk, self.closed_idea.pk})
        data = self.changes("?view=projects&closed=1").json()
        self.assertIn(str(self.gone.pk), data["projects"])

    def test_projects_view_scope_lists_row_signatures_only(self):
        data = self.changes("?view=projects").json()
        self.assertEqual(data["ideas"], [])
        self.assertEqual(set(data["projects"]), {str(self.project.pk), str(self.other.pk)})

    def test_project_scope_lists_its_ideas_and_its_header(self):
        Idea.objects.create(project=self.other, title="Elsewhere")
        data = self.changes(f"?project={self.project.pk}").json()
        self.assertEqual([row[0] for row in data["ideas"]], [self.open_idea.pk])
        self.assertEqual(set(data["projects"]), {str(self.project.pk)})
        # A closed project's page still polls its own header.
        data = self.changes(f"?project={self.gone.pk}").json()
        self.assertEqual(set(data["projects"]), {str(self.gone.pk)})

    def test_idea_scope_keeps_a_closed_idea(self):
        data = self.changes(f"?idea={self.closed_idea.pk}").json()
        self.assertEqual([row[0] for row in data["ideas"]], [self.closed_idea.pk])
        self.assertEqual(data["projects"], {})

    def test_unknown_scope_is_404_or_empty(self):
        self.assertEqual(self.changes("?project=999999").status_code, 404)
        self.assertEqual(self.changes("?idea=999999").json()["ideas"], [])

    def test_versions_and_signatures_match_the_rendered_cards(self):
        data = self.changes().json()
        page = self.client.get("/").content.decode()
        self.assertIn(f'id="idea-{self.open_idea.pk}" data-version="{data["ideas"][0][3]}"', page)
        projects = self.changes("?view=projects").json()["projects"]
        page = self.client.get(reverse("core:projects")).content.decode()
        self.assertIn(f'data-sig="{projects[str(self.project.pk)]}"', page)
        head = self.changes(f"?project={self.project.pk}").json()["projects"]
        page = self.client.get(reverse("core:project_detail", args=["polled"])).content.decode()
        self.assertIn(f'data-sig="{head[str(self.project.pk)]}"', page)
        card = self.client.get(reverse("core:idea_card", args=[self.open_idea.pk])).content.decode()
        self.assertIn(f'data-version="{data["ideas"][0][3]}"', card)

    def test_edit_form_carries_the_version_it_was_opened_at(self):
        version = self.changes().json()["ideas"][0][3]
        form = self.client.get(
            reverse("core:idea_edit", args=[self.open_idea.pk]), headers={"HX-Request": "true"}
        ).content.decode()
        self.assertIn(f'id="idea-{self.open_idea.pk}" data-version="{version}"', form)

    def test_editing_an_idea_changes_its_version(self):
        before = self.changes().json()["ideas"][0][3]
        self.open_idea.goal = "Changed by an agent"
        self.open_idea.save()
        self.assertNotEqual(self.changes().json()["ideas"][0][3], before)

    def test_attaching_an_artifact_changes_the_version(self):
        # An attachment does not touch Idea.updated_at, but the card shows it.
        before = self.changes().json()["ideas"][0][3]
        Artifact.objects.create(idea=self.open_idea, file=SimpleUploadedFile("a.log", b"x"), caption="")
        self.assertNotEqual(self.changes().json()["ideas"][0][3], before)

    def test_a_transition_changes_the_project_signature(self):
        before = self.changes("?view=projects").json()["projects"]
        self.open_idea.status = Idea.Status.PLANNED
        self.open_idea.save()
        after = self.changes("?view=projects").json()["projects"]
        self.assertNotEqual(after[str(self.project.pk)], before[str(self.project.pk)])
        self.assertEqual(after[str(self.other.pk)], before[str(self.other.pk)])

    def test_unchanged_board_answers_304(self):
        first = self.changes()
        self.assertEqual(first.status_code, 200)
        etag = first["ETag"]
        self.assertEqual(self.changes(**{"If-None-Match": etag}).status_code, 304)
        self.open_idea.title = "Retitled"
        self.open_idea.save()
        self.assertEqual(self.changes(**{"If-None-Match": etag}).status_code, 200)

    def test_every_page_polls_its_own_scope(self):
        pages = {
            reverse("core:home"): reverse("core:changes"),
            reverse("core:home") + "?closed=1": reverse("core:changes") + "?closed=1",
            reverse("core:projects"): reverse("core:changes") + "?view=projects",
            reverse("core:project_detail", args=["polled"]): reverse("core:changes") + f"?project={self.project.pk}",
            self.open_idea.detail_url: reverse("core:changes") + f"?idea={self.open_idea.pk}",
        }
        for url, changes_url in pages.items():
            page = self.client.get(url).content.decode()
            self.assertIn(f'data-changes-url="{changes_url.replace("&", "&amp;")}"', page, url)
            self.assertIn("js/board.js", page, url)

    def test_card_urls_on_the_board_carry_the_page(self):
        page = self.client.get(reverse("core:project_detail", args=["polled"])).content.decode()
        card = reverse("core:idea_card", args=[999999999999]).replace("999999999999", "__PK__")
        self.assertIn(f'data-idea-card-url="{card}?view=project"', page)
        self.assertIn('data-insert="slot"', page)
        self.assertIn('data-insert="status"', self.client.get("/").content.decode())


class MarkdownArtifactRenderTests(TestCase):
    """A .md under /media/ is served as a sanitized HTML page; ?raw gives the source."""

    def test_media_md_is_rendered_and_raw_is_not(self):
        import os, tempfile
        from django.test import override_settings
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(f"{root}/artifacts/idea-1")
            with open(f"{root}/artifacts/idea-1/a.md", "w") as f:
                f.write("# Title\n\n- one\n- two\n\n<script>alert(1)</script>\n")
            with override_settings(MEDIA_ROOT=root):
                r = self.client.get("/media/artifacts/idea-1/a.md")
                self.assertEqual(r.status_code, 200)
                self.assertIn("text/html", r["Content-Type"])
                body = r.content.decode()
                self.assertIn("<h1>Title</h1>", body)
                self.assertIn("<li>one</li>", body)
                self.assertNotIn("<script>", body)
                self.assertIn("sandbox", r["Content-Security-Policy"])
                raw = self.client.get("/media/artifacts/idea-1/a.md?raw")
                self.assertNotIn("text/html", raw["Content-Type"])
