"""Tests for the LinTO recap delivery helpers of core.tasks.linto."""

# pylint: disable=protected-access

import contextlib
from unittest import mock

import pytest
from asgiref.sync import async_to_sync

from core import factories
from core.tasks import linto as linto_task

pytestmark = pytest.mark.django_db

REPORT = "# Analytical Report - Test\n\n**Date:** 2026-09-24\n\n## Agenda\n\n1. Test"


class _MemoryCheckpoint(linto_task._StepCheckpoint):
    """A checkpoint over a plain dict, as the Recording/Room stores behave."""

    def __init__(self, steps=None):
        self.steps = dict(steps or {})

        async def _mark(name, value):
            self.steps[name] = value

        super().__init__(get=self.steps.get, mark=_mark)


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


class TestTwakeLinkOnResume:
    """A task resumed after the Twake step still links the Drive folder."""

    def _deliver(self, checkpoint, settings):
        settings.CLOUDERY_URL = "http://cloudery.test"
        settings.CLOUDERY_TOKEN = "tok"
        owner = factories.UserFactory(sub="owner-sub")
        recording = factories.RecordingFactory()
        with contextlib.ExitStack() as stack:
            token = stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.get_drive_token",
                    mock.AsyncMock(return_value="drive-token"),
                )
            )
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.ensure_meeting_directory",
                    mock.AsyncMock(return_value="dir-1"),
                )
            )
            stack.enter_context(
                mock.patch("core.services.twake_drive.save_file", mock.AsyncMock())
            )
            link = async_to_sync(linto_task._deliver_to_twake)(
                conversation_id="conv-1",
                recipient_user=owner,
                transcript_text="hello",
                summary_preview=None,
                pdf_content=None,
                pub_filename="summary.pdf",
                pub_mime="application/pdf",
                meeting=recording,
                language=None,
                extra_files_provider=None,
                log_id="rec-1",
                checkpoint=checkpoint,
            )
        return link, token

    def test_the_link_is_checkpointed(self, settings):
        """The upload stores the folder link with the step."""
        checkpoint = _MemoryCheckpoint()
        link, _ = self._deliver(checkpoint, settings)
        assert link.endswith("/#/folder/dir-1")
        assert checkpoint.steps["twake"] == link

    def test_a_resume_returns_the_checkpointed_link(self, settings):
        """No second upload, and the email still gets the link."""
        stored = "https://owner-drive.example.com/#/folder/dir-1"
        link, token = self._deliver(_MemoryCheckpoint({"twake": stored}), settings)
        assert link == stored
        token.assert_not_awaited()

    def test_a_step_checkpointed_before_the_link_was_kept(self, settings):
        """An older state only knows the step is done: no link, no upload."""
        link, token = self._deliver(_MemoryCheckpoint({"twake": True}), settings)
        assert link is None
        token.assert_not_awaited()
