"""Tests for core.tasks._base failure-notification handling."""

# pylint: disable=redefined-outer-name,protected-access

import types
from unittest import mock

import pytest

from core import factories, models
from core.tasks import _base

pytestmark = pytest.mark.django_db


def _einfo(tb="Traceback (most recent call last):\n  boom\n"):
    return types.SimpleNamespace(traceback=tb)


def _recording_with_owner(email="owner@example.com", state=None):
    rec = factories.RecordingFactory(status=models.RecordingStatusChoices.ACTIVE)
    owner = factories.UserFactory(email=email)
    factories.UserRecordingAccessFactory(
        recording=rec, user=owner, role=models.RoleChoices.OWNER
    )
    if state is not None:
        rec.linto_state = state
        rec.save(update_fields=["linto_state"])
    return rec, owner


class TestHandlePipelineFailure:
    def test_marks_failed_and_sends_admin_and_creator_emails(self, settings):
        settings.EMAIL_SUPPORT_EMAIL = "support@example.com"
        rec, owner = _recording_with_owner(
            state={
                "conversation_id": "conv-1",
                "steps": {"twake": True, "email": False},
                "attempts": 3,
            }
        )

        with mock.patch.object(_base, "EmailMultiAlternatives") as mail_cls:
            _base.handle_pipeline_failure(
                str(rec.id), RuntimeError("boom"), _einfo(), attempts=3
            )

        rec.refresh_from_db()
        assert rec.status == models.RecordingStatusChoices.NOTIFICATION_FAILED

        assert mail_cls.call_count == 2  # admin + creator
        recipients = [c.kwargs["to"] for c in mail_cls.call_args_list]
        assert ["support@example.com"] in recipients
        assert [owner.email] in recipients
        # both messages are actually sent
        assert mail_cls.return_value.send.call_count == 2

    def test_no_support_email_skips_admin_email(self, settings):
        settings.EMAIL_SUPPORT_EMAIL = None
        rec, owner = _recording_with_owner()

        with mock.patch.object(_base, "EmailMultiAlternatives") as mail_cls:
            _base.handle_pipeline_failure(str(rec.id), RuntimeError("x"), _einfo())

        rec.refresh_from_db()
        assert rec.status == models.RecordingStatusChoices.NOTIFICATION_FAILED
        # only the creator email goes out
        assert mail_cls.call_count == 1
        assert mail_cls.call_args.kwargs["to"] == [owner.email]

    def test_no_owner_skips_creator_email(self, settings):
        settings.EMAIL_SUPPORT_EMAIL = "support@example.com"
        rec = factories.RecordingFactory(status=models.RecordingStatusChoices.ACTIVE)

        with mock.patch.object(_base, "EmailMultiAlternatives") as mail_cls:
            _base.handle_pipeline_failure(str(rec.id), RuntimeError("x"), _einfo())

        rec.refresh_from_db()
        assert rec.status == models.RecordingStatusChoices.NOTIFICATION_FAILED
        # only the admin email goes out
        assert mail_cls.call_count == 1
        assert mail_cls.call_args.kwargs["to"] == ["support@example.com"]

    def test_missing_recording_does_not_crash_or_email(self, settings):
        settings.EMAIL_SUPPORT_EMAIL = "support@example.com"
        with mock.patch.object(_base, "EmailMultiAlternatives") as mail_cls:
            # Random UUID that does not exist — handler must swallow and return.
            _base.handle_pipeline_failure(
                "00000000-0000-0000-0000-000000000000",
                RuntimeError("x"),
                _einfo(),
            )
        mail_cls.assert_not_called()

    def test_admin_email_body_contains_diagnostics(self, settings):
        settings.EMAIL_SUPPORT_EMAIL = "support@example.com"
        rec, _owner = _recording_with_owner(
            state={
                "conversation_id": "conv-9",
                "steps": {"twake": True, "email": False},
                "attempts": 4,
            }
        )

        with mock.patch.object(_base, "EmailMultiAlternatives") as mail_cls:
            _base.handle_pipeline_failure(
                str(rec.id), RuntimeError("kaboom"), _einfo("TB-LINE"), attempts=4
            )

        admin_call = next(
            c
            for c in mail_cls.call_args_list
            if c.kwargs["to"] == ["support@example.com"]
        )
        body = admin_call.kwargs["body"]
        assert "conv-9" in body
        assert "kaboom" in body
        assert "TB-LINE" in body
        assert str(rec.id) in body


class TestStepSummary:
    def test_returns_failed_and_done_steps(self):
        failed, done = _base._step_summary(
            {"steps": {"twake": True, "email": False, "tag": True}}
        )
        assert failed == "email"
        assert set(done) == {"twake", "tag"}

    def test_empty_state(self):
        assert _base._step_summary({}) == ("unknown", [])

    def test_all_done(self):
        failed, done = _base._step_summary({"steps": {"twake": True, "email": True}})
        assert failed == "unknown"
        assert set(done) == {"twake", "email"}


class TestResolveOwner:
    def test_returns_owner_user(self):
        rec, owner = _recording_with_owner()
        assert _base._resolve_owner(rec) == owner

    def test_returns_none_without_owner(self):
        rec = factories.RecordingFactory()
        assert _base._resolve_owner(rec) is None


class TestNotificationTaskOnFailure:
    def test_routes_to_handler_with_recording_id_and_attempts(self):
        stub_self = types.SimpleNamespace(request=types.SimpleNamespace(retries=3))
        with mock.patch.object(_base, "handle_pipeline_failure") as handler:
            _base.NotificationTask.on_failure(
                stub_self, RuntimeError("x"), "task-id", ("rec-7",), {}, _einfo()
            )
        handler.assert_called_once()
        assert handler.call_args.args[0] == "rec-7"
        assert handler.call_args.kwargs.get("attempts") == 3

    def test_no_recording_id_is_noop(self):
        stub_self = types.SimpleNamespace(request=types.SimpleNamespace(retries=0))
        with mock.patch.object(_base, "handle_pipeline_failure") as handler:
            _base.NotificationTask.on_failure(
                stub_self, RuntimeError("x"), "task-id", (), {}, _einfo()
            )
        handler.assert_not_called()
