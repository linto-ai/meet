"""Tests for core.services.twake_drive.resolve_instance."""

import pytest

from core import factories
from core.services import twake_drive

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def domain(settings):
    """The domain the plain subs live under, and no dev override."""
    settings.TWAKE_INSTANCE_DOMAIN = "twake.test"
    settings.TWAKE_DEV_INSTANCE_OVERRIDE = None


def test_the_dev_override_wins(settings):
    """The dev override beats a stored instance."""
    settings.TWAKE_DEV_INSTANCE_OVERRIDE = "dev.twake.test"
    user = factories.UserFactory(sub="bob", twake_instance="bob.twake.test")

    assert twake_drive.resolve_instance(user) == "dev.twake.test"


def test_the_stored_instance_comes_before_the_sub():
    """An email sub with a stored instance uploads there."""
    user = factories.UserFactory(
        sub="bob@example.com", twake_instance="bob-1.example.com"
    )

    assert twake_drive.resolve_instance(user) == "bob-1.example.com"


def test_a_plain_sub_names_the_instance():
    """Without a stored instance, a plain sub is the instance name."""
    user = factories.UserFactory(sub="bob", twake_instance=None)

    assert twake_drive.resolve_instance(user) == "bob.twake.test"


@pytest.mark.parametrize("sub", ["bob@example.com", "bob.smith", "jean_paul", None])
def test_no_instance_when_the_sub_is_not_a_plain_id(sub, caplog):
    """An email, a dotted sub or one that makes no hostname gives no instance."""
    user = factories.UserFactory(sub=sub, twake_instance=None)

    with caplog.at_level("WARNING", logger=twake_drive.logger.name):
        assert twake_drive.resolve_instance(user) is None

    assert any(
        "No Twake instance known for user" in r.getMessage() for r in caplog.records
    )
