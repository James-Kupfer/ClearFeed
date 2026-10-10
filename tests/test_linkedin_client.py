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


# --- LinkedIn reply channel ---------------------------------------------------

_LR_PROFILE = {
    "name": "task_connection",
    "target": "todoist",
    "todoist": {"project": "P", "content": "c", "description": "", "priority": 3},
    "auto_reply": {
        "when_key": "action", "when_value": "Decline", "body": "No thanks.\n\nJames",
        "channel": "linkedin", "trash": True,
        "linkedin_reply": {"url_key": "review_url", "not_url_key": "accept_url"},
        "linkedin_ignore": {"url_key": "decline_url"},
    },
}
_LR_PR = {
    "action": "Decline",
    "review_url": "https://www.linkedin.com/review/1",
    "decline_url": "https://www.linkedin.com/ignore/1",
    "accept_url": "https://www.linkedin.com/accept/1",
}
_LR_REC = {"id": 8, "source_type": "gmail", "source_ref": "msg-8"}


def test_linkedin_channel_replies_then_ignores_then_trashes_in_order():
    from action_dispatch import _handle_auto_reply

    gmail, order = MagicMock(), []
    gmail.trash_message.side_effect = lambda *_: order.append("trash")
    with patch("linkedin_client.send_message", side_effect=lambda *a, **k: order.append("reply")) as send, \
         patch("linkedin_client.ignore_invitation", side_effect=lambda *a, **k: order.append("ignore")):
        result = _handle_auto_reply(_LR_REC, _LR_PROFILE, _LR_PR, {"gmail": gmail})

    assert order == ["reply", "ignore", "trash"]
    assert send.call_args.args == ("https://www.linkedin.com/review/1", "No thanks.\n\nJames")
    gmail.trash_message.assert_called_once_with("msg-8")
    assert result.status == "created" and result.target == "linkedin_reply" and result.error is None


def test_linkedin_channel_reply_failure_returns_none_and_touches_nothing():
    from action_dispatch import _handle_auto_reply

    gmail = MagicMock()
    with patch("linkedin_client.send_message", side_effect=RuntimeError("no box")), \
         patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(_LR_REC, _LR_PROFILE, _LR_PR, {"gmail": gmail})

    assert result is None
    ignore.assert_not_called()
    gmail.trash_message.assert_not_called()


@pytest.mark.parametrize(
    "pr",
    [
        {**_LR_PR, "review_url": ""},
        {**_LR_PR, "review_url": _LR_PR["accept_url"]},
    ],
)
def test_linkedin_channel_bad_url_returns_none_without_opening_browser(pr):
    from action_dispatch import _handle_auto_reply

    with patch("linkedin_client.send_message") as send:
        assert _handle_auto_reply(_LR_REC, _LR_PROFILE, pr, {"gmail": MagicMock()}) is None
    send.assert_not_called()


def test_linkedin_channel_post_send_failures_are_recorded_not_fatal():
    from action_dispatch import _handle_auto_reply

    gmail = MagicMock()
    gmail.trash_message.side_effect = RuntimeError("gmail down")
    with patch("linkedin_client.send_message"), \
         patch("linkedin_client.ignore_invitation", side_effect=RuntimeError("expired")):
        result = _handle_auto_reply(_LR_REC, _LR_PROFILE, _LR_PR, {"gmail": gmail})

    assert result.status == "created"
    assert "LinkedIn ignore failed: expired" in result.error
    assert "trash failed: gmail down" in result.error


def test_action_one_linkedin_reply_failure_falls_back_to_task():
    from action_dispatch import _action_one

    todoist = MagicMock()
    todoist.resolve_project_id.return_value = "proj"
    todoist.create_task.return_value = "task-3"
    llm = MagicMock()
    llm.call_json.return_value = _LR_PR
    profile = {**_LR_PROFILE, "prompt": "p {sender}"}
    with patch("linkedin_client.send_message", side_effect=RuntimeError("no box")):
        result = _action_one(
            {**_LR_REC, "sender": "x@y.com"}, profile, llm, {"todoist": todoist, "gmail": MagicMock()}
        )

    assert result.external_id == "task-3" and result.target is None


def test_load_profile_validates_channel(tmp_path):
    import yaml

    from action_dispatch import _load_profile

    def load(auto_reply):
        base = {
            "name": "t", "target": "todoist",
            "trigger": {"sql": "SELECT cr.id FROM ContentRecords cr WHERE {{dedup}}"},
            "todoist": {"project": "P", "content": "c", "description": "d"},
            "prompt": "p", "auto_reply": {"when_key": "a", "when_value": "b", "body": "x", **auto_reply},
        }
        p = tmp_path / "p.yaml"
        p.write_text(yaml.dump(base))
        return _load_profile(p)

    with pytest.raises(ValueError, match="channel"):
        load({"channel": "sms"})
    with pytest.raises(ValueError, match="url_key"):
        load({"channel": "linkedin"})
    assert load({"channel": "linkedin", "linkedin_reply": {"url_key": "review_url"}})


def test_shipped_profile_uses_linkedin_channel():
    from pathlib import Path

    from action_dispatch import _load_profile

    cfg = _load_profile(Path(__file__).parent.parent / "profiles" / "task_connection.yaml")["auto_reply"]
    assert cfg["channel"] == "linkedin"
    assert cfg["linkedin_reply"]["url_key"] == "review_url"
