"""Tests for linkedin_client URL validation and the ignore wiring (no browser launched)."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/comm/mynetwork/ignore?x=1",
        "https://linkedin.com/x",
        "https://WWW.LinkedIn.com/x",
    ],
)
def test_validate_url_accepts_linkedin(url):
    from linkedin_client import validate_url

    assert validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://www.linkedin.com/x",
        "https://evil.com/x",
        "https://linkedin.com.evil.com/x",
        "https://notlinkedin.com/x",
        "javascript:alert(1)",
        "file:///etc/passwd",
    ],
)
def test_validate_url_rejects_everything_else(url):
    from linkedin_client import LinkedInError, validate_url

    with pytest.raises(LinkedInError):
        validate_url(url)


def test_ignore_invitation_does_not_launch_browser_for_bad_url():
    import linkedin_client

    with patch.object(linkedin_client, "_visit") as visit:
        with pytest.raises(linkedin_client.LinkedInError):
            linkedin_client.ignore_invitation("https://evil.com/x")
    visit.assert_not_called()


def test_ignore_invitation_passes_click_text_through():
    import linkedin_client

    with patch.object(linkedin_client, "_visit") as visit:
        linkedin_client.ignore_invitation("https://www.linkedin.com/x", click_text="Ignore")
    args = visit.call_args.args
    assert args[0] == "https://www.linkedin.com/x" and args[1] == "Ignore"


# --- wiring in action_dispatch ---------------------------------------------

_LI = {"url_key": "decline_url", "click_text": "Ignore", "not_url_key": "accept_url"}


def _profile(li=_LI):
    return {
        "name": "task_connection",
        "target": "todoist",
        "todoist": {"project": "P", "content": "c", "description": "", "priority": 3},
        "auto_reply": {
            "when_key": "action", "when_value": "Decline", "body": "no", "trash": True,
            "linkedin_ignore": li,
        },
    }


def _gmail():
    g = MagicMock()
    g.get_message_headers.return_value = {
        "thread_id": "t", "headers": {"from": "a@b.com", "subject": "s", "message-id": "<m>"}
    }
    g.send_reply.return_value = "sent-1"
    return g


_REC = {"id": 3, "source_type": "gmail", "source_ref": "msg-1"}
_PR = {"action": "Decline", "decline_url": "https://www.linkedin.com/ignore/1",
       "accept_url": "https://www.linkedin.com/accept/1"}


def test_decline_opens_ignore_link_after_reply_and_trash():
    from action_dispatch import _handle_auto_reply

    gmail = _gmail()
    with patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(_REC, _profile(), _PR, {"gmail": gmail})

    ignore.assert_called_once_with("https://www.linkedin.com/ignore/1", click_text="Ignore")
    assert result.status == "created" and result.error is None


def test_linkedin_failure_after_reply_stays_created_with_error():
    from action_dispatch import _handle_auto_reply

    with patch("linkedin_client.ignore_invitation", side_effect=RuntimeError("expired")):
        result = _handle_auto_reply(_REC, _profile(), _PR, {"gmail": _gmail()})

    assert result.status == "created"
    assert "LinkedIn ignore failed: expired" in result.error


def test_linkedin_not_attempted_when_send_fails():
    from action_dispatch import _handle_auto_reply

    gmail = _gmail()
    gmail.send_reply.side_effect = RuntimeError("boom")
    with patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(_REC, _profile(), _PR, {"gmail": gmail})

    assert result.status == "failed"
    ignore.assert_not_called()


def test_linkedin_refuses_when_decline_url_equals_accept_url():
    from action_dispatch import _handle_auto_reply

    pr = {**_PR, "decline_url": _PR["accept_url"]}
    with patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(_REC, _profile(), pr, {"gmail": _gmail()})

    ignore.assert_not_called()
    assert result.status == "created" and "refused" in result.error


def test_linkedin_skipped_when_no_url():
    from action_dispatch import _handle_auto_reply

    pr = {"action": "Decline", "decline_url": ""}
    with patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(_REC, _profile(), pr, {"gmail": _gmail()})

    ignore.assert_not_called()
    assert "no decline_url" in result.error


def test_load_profile_rejects_linkedin_ignore_without_url_key(tmp_path):
    import yaml

    from action_dispatch import _load_profile

    base = {
        "name": "t", "target": "todoist",
        "trigger": {"sql": "SELECT cr.id FROM ContentRecords cr WHERE {{dedup}}"},
        "todoist": {"project": "P", "content": "c", "description": "d"},
        "prompt": "p",
        "auto_reply": {"when_key": "a", "when_value": "b", "body": "x", "linkedin_ignore": {}},
    }
    p = tmp_path / "p.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="url_key"):
        _load_profile(p)
