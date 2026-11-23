"""Test configuration: a fictional owner (the code reads every identity value from the environment).

None of these is a real person, account or chat. America/Edmonton has the same offsets and DST
rules as the fixtures' original zone, so the time arithmetic in the tests is unchanged."""
import os

for _k, _v in {
    "OMNI_OWNER_TELEGRAM_CHAT": "1000000001",
    "OMNI_OWNER_NAME": "Alex Morgan",
    "OMNI_OWNER_EMAIL": "owner@example.com",
    "OMNI_AGENT_EMAIL": "agent@example.com",
    "OMNI_TIMEZONE": "America/Edmonton",
}.items():
    os.environ.setdefault(_k, _v)

import pytest

#: Tests that exercise code of the original agent that this repository does not ship. Each name
#: says what is missing; README.md "What is not here yet" lists the same parts.
NOT_SHIPPED = {
    "test_omni_buzz_agent_runs_dont_ask": "scripts/buzz_agents.py (the Buzz chat-agent launcher)",
    "test_executor_posts_exactly_the_granted_text": "scripts/omni_tweet_send.py (tweet sender)",
    "test_executor_refuses_without_grant_and_never_calls_the_provider": "scripts/omni_tweet_send.py (tweet sender)",
    "test_legacy_twitter_post_cannot_post": "scripts/twitter_post.py (legacy tweet poster)",
    "test_no_permission_bypass_and_no_general_bash": "scripts/agent_claude.py (Claude Code sub-agent launcher)",
    "test_followup_is_proposed_never_sent_and_never_to_retired_address": "scripts/meeting_followup.py (meeting follow-up mailer)",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        why = NOT_SHIPPED.get(item.name)
        if why:
            item.add_marker(pytest.mark.skip(reason="not shipped: " + why))
