"""Test the LiveKit join token Meet mints for the native LinTO visio bot."""

import jwt
import pytest

from core.utils import generate_bot_join_token

pytestmark = pytest.mark.django_db


def test_generate_bot_join_token_grants(settings):
    """The bot token is room-scoped, hidden, subscribe-only, data-publishing and,
    crucially, an AGENT-kind participant (the SFU only relays transcription
    packets from agents)."""
    settings.LINTO_NATIVE_TOKEN_TTL = 1234

    token = generate_bot_join_token("room-1", "chan-9")

    claims = jwt.decode(
        token,
        settings.LIVEKIT_CONFIGURATION["api_secret"],
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["sub"] == "linto-visio-bot-room-1-chan-9"
    assert claims["name"] == "LinTO"
    assert claims["exp"] - claims["nbf"] == 1234
    video = claims["video"]
    assert video["room"] == "room-1"
    assert video["roomJoin"] is True
    assert video["canSubscribe"] is True
    assert video["canPublish"] is False
    assert video["canPublishData"] is True
    assert video["hidden"] is True
    assert video["agent"] is True
