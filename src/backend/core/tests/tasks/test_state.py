"""Tests for core.tasks._state idempotent checkpoint helpers."""

# pylint: disable=redefined-outer-name

import pytest
from asgiref.sync import async_to_sync

from core import factories
from core.tasks import _state

pytestmark = pytest.mark.django_db


class TestEnsureState:
    def test_fills_defaults_when_none(self):
        rec = factories.RecordingFactory()
        rec.linto_state = None
        state = _state.ensure_state(rec)
        assert state == {"conversation_id": None, "steps": {}, "attempts": 0}
        # returned dict is the same object stored on the recording
        assert rec.linto_state is state

    def test_preserves_existing_values(self):
        rec = factories.RecordingFactory()
        rec.linto_state = {
            "conversation_id": "conv-1",
            "steps": {"twake": True},
            "attempts": 2,
        }
        state = _state.ensure_state(rec)
        assert state["conversation_id"] == "conv-1"
        assert state["steps"] == {"twake": True}
        assert state["attempts"] == 2

    def test_partial_state_fills_only_missing_keys(self):
        rec = factories.RecordingFactory()
        rec.linto_state = {"attempts": 5}
        state = _state.ensure_state(rec)
        assert state["attempts"] == 5
        assert state["conversation_id"] is None
        assert state["steps"] == {}


class TestStepDone:
    def test_false_when_step_absent(self):
        rec = factories.RecordingFactory()
        _state.ensure_state(rec)
        assert _state.step_done(rec, "twake") is False

    def test_true_after_mark_done(self):
        rec = factories.RecordingFactory()
        _state.ensure_state(rec)
        async_to_sync(_state.mark_done)(rec, "twake")
        assert _state.step_done(rec, "twake") is True

    def test_other_steps_remain_undone(self):
        rec = factories.RecordingFactory()
        _state.ensure_state(rec)
        async_to_sync(_state.mark_done)(rec, "twake")
        assert _state.step_done(rec, "email") is False


class TestMarkDonePersistence:
    def test_mark_done_persists_to_db(self):
        rec = factories.RecordingFactory()
        _state.ensure_state(rec)
        async_to_sync(_state.mark_done)(rec, "transcription")

        reloaded = type(rec).objects.get(id=rec.id)
        assert reloaded.linto_state["steps"]["transcription"] is True

    def test_save_state_persists_conversation_id(self):
        rec = factories.RecordingFactory()
        _state.ensure_state(rec)
        _state.set_conversation_id(rec, "conv-xyz")
        async_to_sync(_state.save_state)(rec)

        reloaded = type(rec).objects.get(id=rec.id)
        assert reloaded.linto_state["conversation_id"] == "conv-xyz"


class TestConversationId:
    def test_get_returns_none_by_default(self):
        rec = factories.RecordingFactory()
        rec.linto_state = {}
        assert _state.get_conversation_id(rec) is None

    def test_set_then_get_roundtrip(self):
        rec = factories.RecordingFactory()
        _state.set_conversation_id(rec, "conv-42")
        assert _state.get_conversation_id(rec) == "conv-42"
