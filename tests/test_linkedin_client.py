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

_CFG = {
    "when_key": "action",
    "when_value": "Decline",
    "trash": True,
    "linkedin_ignore": {"url_key": "review_url", "click_text": "Ignore", "not_url_key": "accept_url"},
}


def _profile(**overrides):
    return {
        "name": "task_connection",
        "target": "todoist",
        "todoist": {"project": "P", "content": "c", "description": "", "priority": 3},
        "auto_decline": {**_CFG, **overrides},
    }


_PR = {
    "action": "Decline",
    "review_url": "https://www.linkedin.com/in/someone/",
    "accept_url": "https://www.linkedin.com/accept/1",
}
_REC = {"id": 8, "source_type": "gmail", "source_ref": "msg-8"}


def test_auto_decline_matches_is_exact_but_case_and_whitespace_insensitive():
    from action_dispatch import _auto_decline_matches

    p = _profile()
    assert _auto_decline_matches(p, {"action": "Decline"})
    assert _auto_decline_matches(p, {"action": " decline "})
    assert not _auto_decline_matches(p, {"action": "Review"})
    assert not _auto_decline_matches(p, {"action": "Declined"})
    assert not _auto_decline_matches(p, {"is_job_alert": True})
    assert not _auto_decline_matches({"name": "x"}, {"action": "Decline"})


def test_decline_ignores_on_linkedin_then_trashes_and_never_sends_anything():
    from action_dispatch import _handle_auto_decline

    gmail, order = MagicMock(), []
    gmail.trash_message.side_effect = lambda *_: order.append("trash")
    with patch("linkedin_client.ignore_invitation", side_effect=lambda *a, **k: order.append("ignore")) as ig:
        result = _handle_auto_decline(_REC, _profile(), _PR, {"gmail": gmail})

    assert order == ["ignore", "trash"]
    ig.assert_called_once_with(
        "https://www.linkedin.com/in/someone/", click_text="Ignore", click_delay_seconds=None
    )
    gmail.trash_message.assert_called_once_with("msg-8")
    assert not gmail.send_reply.called and not gmail.send_message.called
    assert result.status == "created" and result.target == "linkedin_ignore" and result.error is None


def test_ignore_failure_returns_none_and_does_not_trash():
    from action_dispatch import _handle_auto_decline

    gmail = MagicMock()
    with patch("linkedin_client.ignore_invitation", side_effect=RuntimeError("button gone?")):
        result = _handle_auto_decline(_REC, _profile(), _PR, {"gmail": gmail})

    assert result is None
    gmail.trash_message.assert_not_called()


@pytest.mark.parametrize(
    "pr",
    [
        {**_PR, "review_url": ""},
        {**_PR, "review_url": _PR["accept_url"]},
    ],
)
def test_missing_or_accept_url_returns_none_without_opening_browser(pr):
    from action_dispatch import _handle_auto_decline

    gmail = MagicMock()
    with patch("linkedin_client.ignore_invitation") as ig:
        assert _handle_auto_decline(_REC, _profile(), pr, {"gmail": gmail}) is None
    ig.assert_not_called()
    gmail.trash_message.assert_not_called()


def test_trash_failure_after_ignore_is_recorded_and_stays_created():
    from action_dispatch import _handle_auto_decline

    gmail = MagicMock()
    gmail.trash_message.side_effect = RuntimeError("gmail down")
    with patch("linkedin_client.ignore_invitation"):
        result = _handle_auto_decline(_REC, _profile(), _PR, {"gmail": gmail})

    assert result.status == "created"
    assert "trash failed: gmail down" in result.error


def test_trash_disabled_skips_gmail():
    from action_dispatch import _handle_auto_decline

    gmail = MagicMock()
    with patch("linkedin_client.ignore_invitation"):
        result = _handle_auto_decline(_REC, _profile(trash=False), _PR, {"gmail": gmail})
    assert result.status == "created"
    gmail.trash_message.assert_not_called()


def test_record_without_gmail_source_is_recorded_not_fatal():
    from action_dispatch import _handle_auto_decline

    with patch("linkedin_client.ignore_invitation"):
        result = _handle_auto_decline({"id": 1}, _profile(), _PR, {"gmail": MagicMock()})
    assert result.status == "created" and "source_ref" in result.error


def test_action_one_decline_uses_ignore_not_task():
    from action_dispatch import _action_one

    gmail, todoist, llm = MagicMock(), MagicMock(), MagicMock()
    llm.call_json.return_value = _PR
    profile = {**_profile(), "prompt": "p {sender}"}
    with patch("linkedin_client.ignore_invitation"):
        result = _action_one({**_REC, "sender": "x@y.com"}, profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.target == "linkedin_ignore"
    todoist.create_task.assert_not_called()


def test_action_one_ignore_failure_falls_back_to_task():
    from action_dispatch import _action_one

    gmail, todoist, llm = MagicMock(), MagicMock(), MagicMock()
    todoist.resolve_project_id.return_value = "proj"
    todoist.create_task.return_value = "task-3"
    llm.call_json.return_value = _PR
    profile = {**_profile(), "prompt": "p {sender}"}
    with patch("linkedin_client.ignore_invitation", side_effect=RuntimeError("x")):
        result = _action_one({**_REC, "sender": "x@y.com"}, profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.external_id == "task-3" and result.target is None
    gmail.trash_message.assert_not_called()


def test_action_one_non_decline_still_creates_task():
    from action_dispatch import _action_one

    gmail, todoist, llm = MagicMock(), MagicMock(), MagicMock()
    todoist.resolve_project_id.return_value = "proj"
    todoist.create_task.return_value = "task-4"
    llm.call_json.return_value = {**_PR, "action": "Review"}
    profile = {**_profile(), "prompt": "p {sender}"}
    with patch("linkedin_client.ignore_invitation") as ig:
        result = _action_one({**_REC, "sender": "x@y.com"}, profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.external_id == "task-4"
    ig.assert_not_called()


def _load(tmp_path, auto_decline):
    import yaml

    from action_dispatch import _load_profile

    base = {
        "name": "t", "target": "todoist",
        "trigger": {"sql": "SELECT cr.id FROM ContentRecords cr WHERE {{dedup}}"},
        "todoist": {"project": "P", "content": "c", "description": "d"},
        "prompt": "p", "auto_decline": auto_decline,
    }
    p = tmp_path / "p.yaml"
    p.write_text(yaml.dump(base))
    return _load_profile(p)


def test_load_profile_accepts_auto_decline(tmp_path):
    assert _load(tmp_path, _CFG)["auto_decline"]["when_value"] == "Decline"


@pytest.mark.parametrize(
    "cfg,match",
    [
        ({k: v for k, v in _CFG.items() if k != "when_key"}, "when_key"),
        ({k: v for k, v in _CFG.items() if k != "linkedin_ignore"}, "linkedin_ignore"),
        ({**_CFG, "linkedin_ignore": {"url_key": "review_url"}}, "click_text"),
        ({**_CFG, "linkedin_ignore": {"click_text": "Ignore"}}, "url_key"),
    ],
)
def test_load_profile_rejects_bad_auto_decline(tmp_path, cfg, match):
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, cfg)


def test_load_profile_rejects_auto_decline_in_aggregate_mode(tmp_path):
    import yaml

    from action_dispatch import _load_profile

    base = {
        "name": "t", "target": "todoist", "mode": "aggregate",
        "trigger": {"sql": "SELECT cr.id FROM ContentRecords cr WHERE {{dedup}}"},
        "todoist": {"project": "P", "content": "c", "description": "d"},
        "prompt": "p", "auto_decline": _CFG,
    }
    p = tmp_path / "p.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="per_record"):
        _load_profile(p)


def test_shipped_profile_ignores_via_profile_page_button():
    from pathlib import Path

    from action_dispatch import _load_profile

    profile = _load_profile(Path(__file__).parent.parent / "profiles" / "task_connection.yaml")
    cfg = profile["auto_decline"]
    assert (cfg["when_key"], cfg["when_value"]) == ("action", "Decline")
    assert cfg["linkedin_ignore"] == {
        "url_key": "review_url", "click_text": "Ignore", "not_url_key": "accept_url",
        "delay_seconds": [5, 25],
    }
    assert "cr.source_ref" in profile["trigger"]["sql"]
    assert "body" not in cfg


def test_run_action_records_linkedin_ignore_target(tmp_path):
    import yaml

    import action_dispatch

    profile_path = tmp_path / "p.yaml"
    profile_path.write_text(yaml.dump({
        "name": "task_connection", "target": "todoist",
        "trigger": {"sql": "SELECT cr.id FROM ContentRecords cr WHERE {{dedup}}"},
        "todoist": {"project": "P", "content": "c", "description": "d"},
        "prompt": "p {sender}", "auto_decline": _CFG,
    }))
    llm = MagicMock()
    llm.call_json.return_value = _PR
    with patch.object(action_dispatch, "_query_pending_records", return_value=[{**_REC, "sender": "x"}]),          patch.object(action_dispatch, "LLMClient", return_value=llm),          patch.object(action_dispatch, "TodoistClient", return_value=MagicMock()),          patch("gmail_client.GmailClient", return_value=MagicMock()),          patch("linkedin_client.ignore_invitation"),          patch.object(action_dispatch, "_insert_action_run") as insert:
        action_dispatch.run_action(profile_path)

    assert insert.call_args.kwargs["target"] == "linkedin_ignore"
    assert insert.call_args.kwargs["status"] == "created"


# --- click delay ---------------------------------------------------------------


def test_pick_delay_none_or_empty_is_zero():
    from linkedin_client import pick_delay

    assert pick_delay(None) == 0.0
    assert pick_delay([]) == 0.0


def test_pick_delay_stays_within_range_and_varies():
    from linkedin_client import pick_delay

    vals = [pick_delay([5, 25]) for _ in range(500)]
    assert all(5 <= v <= 25 for v in vals)
    assert len({round(v, 3) for v in vals}) > 100  # genuinely random, not constant
    assert min(vals) < 8 and max(vals) > 22        # spans the range


def test_delay_seconds_is_passed_from_profile_to_ignore_invitation():
    from action_dispatch import _handle_auto_decline

    cfg = {**_CFG["linkedin_ignore"], "delay_seconds": [5, 25]}
    with patch("linkedin_client.ignore_invitation") as ig:
        _handle_auto_decline(_REC, _profile(linkedin_ignore=cfg), _PR, {"gmail": MagicMock()})
    assert ig.call_args.kwargs["click_delay_seconds"] == [5, 25]


@pytest.mark.parametrize("bad", [[5], [25, 5], [-1, 5], "5-25", [5, "x"], [5, 25, 30], 10])
def test_load_profile_rejects_bad_delay_seconds(tmp_path, bad):
    cfg = {**_CFG, "linkedin_ignore": {**_CFG["linkedin_ignore"], "delay_seconds": bad}}
    with pytest.raises(ValueError, match="delay_seconds"):
        _load(tmp_path, cfg)


def test_load_profile_accepts_delay_seconds(tmp_path):
    cfg = {**_CFG, "linkedin_ignore": {**_CFG["linkedin_ignore"], "delay_seconds": [5, 25]}}
    assert _load(tmp_path, cfg)["auto_decline"]["linkedin_ignore"]["delay_seconds"] == [5, 25]


def test_shipped_profile_delays_5_to_25_seconds():
    from pathlib import Path

    from action_dispatch import _load_profile

    li = _load_profile(Path(__file__).parent.parent / "profiles" / "task_connection.yaml")[
        "auto_decline"]["linkedin_ignore"]
    assert li["delay_seconds"] == [5, 25]
