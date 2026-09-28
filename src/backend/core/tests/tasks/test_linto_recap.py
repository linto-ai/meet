"""Tests for the LinTO recap delivery helpers of core.tasks.linto."""

# pylint: disable=protected-access

import pytest

from core.tasks import linto as linto_task

pytestmark = pytest.mark.django_db

REPORT = "# Analytical Report - Test\n\n**Date:** 2026-09-24\n\n## Agenda\n\n1. Test"


class TestSummaryPreview:
    """The summary as the LLM answered it, made fit for the email."""

    @pytest.mark.parametrize(
        "content",
        [
            f"```markdown\n{REPORT}\n```",
            f"```\n{REPORT}\n```\n",
            f"  ```md\n{REPORT}\n```  ",
            f"```markdown\n{REPORT}",  # cut answer: no closing fence
        ],
    )
    def test_a_fence_wrapping_the_whole_summary_is_dropped(self, content):
        """A summary wrapped in a code fence renders as Markdown, not code."""
        preview = linto_task._extract_summary_preview({"content": content})
        assert preview == REPORT
        html = linto_task._render_summary_html(preview)
        assert "<h1>" in html
        assert "<pre>" not in html

    def test_a_plain_summary_is_kept(self):
        """Nothing to strip: the summary is returned as is."""
        assert linto_task._extract_summary_preview({"content": REPORT}) == REPORT

    def test_code_blocks_inside_the_summary_are_kept(self):
        """Only a fence around the WHOLE summary goes."""
        content = f"{REPORT}\n\n```\nsome code\n```"
        assert linto_task._extract_summary_preview({"content": content}) == content

    def test_a_dict_content_is_unwrapped_too(self):
        """The export content can come as ``{"text": ...}``."""
        content = {"text": f"```markdown\n{REPORT}\n```"}
        assert linto_task._extract_summary_preview({"content": content}) == REPORT
