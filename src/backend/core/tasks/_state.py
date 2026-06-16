"""Shared idempotent-checkpoint state helpers for recording-pipeline tasks.

The recording-notification tasks (LinTO transcription, screen-recording-to-Twake)
checkpoint their progress in ``recording.linto_state`` so that a retry resumes
where the previous attempt left off instead of re-running completed work.

The state shape is generic and step-name driven::

    {"conversation_id": None, "steps": {"twake": True, ...}, "attempts": 0}

``steps`` starts EMPTY: a step name is only added (set to ``True``) once the
step has completed, so ``step_done`` is simply "is this name present and true".
No step ordering is encoded here.
"""

from asgiref.sync import sync_to_async


def ensure_state(recording):
    """Return a normalized ``linto_state`` on the recording (filling defaults).

    Mutates ``recording.linto_state`` in place (and returns it) so callers can
    read/write through the returned dict.
    """
    state = recording.linto_state or {}
    state.setdefault("conversation_id", None)
    state.setdefault("steps", {})
    state.setdefault("attempts", 0)
    recording.linto_state = state
    return state


@sync_to_async
def save_state(recording):
    """Persist the ``linto_state`` field only."""
    recording.save(update_fields=["linto_state", "updated_at"])


def step_done(recording, name) -> bool:
    """Return whether the named step has been checkpointed as done."""
    return bool((recording.linto_state.get("steps") or {}).get(name))


async def mark_done(recording, name):
    """Mark the named step as done and persist the state."""
    recording.linto_state.setdefault("steps", {})[name] = True
    await save_state(recording)


def get_conversation_id(recording):
    """Return the checkpointed conversation id, if any."""
    return (recording.linto_state or {}).get("conversation_id")


def set_conversation_id(recording, conversation_id):
    """Store the conversation id in the state (does not persist)."""
    ensure_state(recording)["conversation_id"] = conversation_id


__all__ = (
    "ensure_state",
    "save_state",
    "step_done",
    "mark_done",
    "get_conversation_id",
    "set_conversation_id",
)
