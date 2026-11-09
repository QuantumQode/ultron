import json
from dataclasses import asdict

import pytest

from ultron.config import Settings
from ultron.features import profile
from ultron.features.profile import PlanSettings
from ultron.ops import activity

OWNER = 111
GROUP = -100500


# Planning settings most tests run with: an owner who set a 35-minute routine, a sport rule
# and a wind-down note (the defaults have none of these). Tests of the defaults use
# profile.DEFAULTS.
CONFIGURED = PlanSettings(routine_minutes=35, sport_gap=90, wind_down_note="journal or meditate")


def configure(state, settings: PlanSettings = CONFIGURED) -> None:
    """Store `settings` as the owner's, without the Board line profile.save adds."""
    state.set(profile.KEY, json.dumps(asdict(settings)))


def make_settings(**overrides) -> Settings:
    values = dict(telegram_token="t", telegram_owner_id=OWNER, anthropic_api_key="k")
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def group_settings() -> Settings:
    return make_settings(
        telegram_group_id=GROUP,
        telegram_topic_chat=2,
        telegram_topic_reflect=3,
        telegram_topic_briefs=4,
    )


@pytest.fixture(autouse=True)
def _no_activity_sink():
    """A test that attaches the activity log mustn't leave it attached for the next one."""
    yield
    activity.detach()
