"""Tests for the LinTO recap delivery helpers of core.tasks.linto."""

# pylint: disable=protected-access,no-member

import contextlib
from datetime import datetime, timezone
from unittest import mock

from django.core import mail

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

    def test_a_short_summary_is_not_truncated(self):
        """Under the limit the preview is the whole summary."""
        assert linto_task._truncate_summary(REPORT) == REPORT

    def test_a_long_summary_is_cut_on_a_line(self):
        """Whole lines up to the limit, then an ellipsis."""
        lines = [f"- point {i} " + "x" * 40 for i in range(100)]
        preview = linto_task._truncate_summary("\n".join(lines), limit=500)
        body, ellipsis = preview.rsplit("\n\n", 1)
        assert ellipsis == "…"
        assert len(body) <= 500
        assert all(line in lines for line in body.splitlines())

    def test_a_single_long_line_is_cut_on_a_word(self):
        """A first line over the limit is cut between two words."""
        preview = linto_task._truncate_summary("word " * 200, limit=52)
        assert preview == " ".join(["word"] * 10) + "\n\n…"


class TestRecapEmail:
    """The recap email puts the Drive link before a bounded summary preview."""

    def _send(self, **kwargs):
        user = factories.UserFactory(email="owner@example.com", language="en-us")
        params = {
            "recipient_users": [user],
            "room_name": "okr-mcwr-mqk",
            "meeting_dt": datetime(2026, 9, 24, 14, 32, tzinfo=timezone.utc),
            "summary_preview": REPORT,
            "twake_drive_link": "https://owner-drive.example.com/#/folder/dir-1",
            "pdf_content": None,
            "pub_filename": "summary.pdf",
            "pub_mime": "application/pdf",
            "log_id": "rec-1",
            "checkpoint": _MemoryCheckpoint(),
        }
        params.update(kwargs)
        async_to_sync(linto_task._send_recap_emails)(**params)
        assert len(mail.outbox) == 1
        return mail.outbox[0]

    def test_the_drive_link_comes_before_the_summary(self):
        """A long summary can no longer push the link out of sight."""
        message = self._send()
        html = message.alternatives[0][0]
        link = html.index("https://owner-drive.example.com/#/folder/dir-1")
        assert link < html.index("Analytical Report")
        assert message.body.index("owner-drive") < message.body.index("Analytical")

    def test_the_html_preview_is_bounded(self):
        """The email shows the start of a long report, not all of it."""
        report = REPORT + "".join(f"\n\n## Topic {i}\n\nDetail {i}" for i in range(400))
        message = self._send(summary_preview=report)
        html = message.alternatives[0][0]
        assert "Topic 1<" in html
        assert "Topic 399" not in html
        assert "…" in html

    def test_no_summary_no_preview_heading(self):
        """Without a summary the email does not announce a preview."""
        message = self._send(summary_preview=None)
        assert "Summary preview" not in message.alternatives[0][0]
        assert "Summary preview" not in message.body


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
