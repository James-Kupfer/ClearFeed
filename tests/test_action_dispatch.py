"""Tests for action_dispatch and todoist_client."""

import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Profile loading helpers
# ---------------------------------------------------------------------------


def _minimal_profile(overrides: dict | None = None) -> str:
    base = {
        "kind": "action",
        "name": "test_profile",
        "target": "todoist",
        "model": "haiku",
        "trigger": {
            "sql": (
                "SELECT cr.id, cr.sender, cr.subject FROM ContentRecords cr "
                "WHERE cr.enrichment_status='complete' AND {{dedup}} "
                "ORDER BY cr.processed_at DESC"
            ),
        },
        "todoist": {
            "project": "Professional",
            "content": "Reply to {name}",
            "description": "{description}\nFrom: {sender}",
            "priority": 3,
        },
        "prompt": "Return JSON with keys 'name' and 'description'. Sender: {sender}",
    }
    if overrides:
        base.update(overrides)
    return yaml.dump(base)


def _minimal_aggregate_profile() -> str:
    base = {
        "kind": "action",
        "name": "task_investment",
        "target": "todoist",
        "mode": "aggregate",
        "model": "sonnet",
        "trigger": {
            "sql": (
                "SELECT cr.id, cr.sender, cr.subject FROM ContentRecords cr "
                "WHERE cr.enrichment_status='complete' AND {{dedup}} "
                "ORDER BY cr.received_at DESC"
            ),
            "prior_actions_hours": 74,
        },
        "todoist": {
            "project": "Investment",
            "content": "{action}",
            "description": "{description}",
            "priority": "{priority}",
        },
        "prompt": "Records: {records_json}\nPrior: {prior_actions_json}",
    }
    return yaml.dump(base)


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def test_load_profile_accepts_valid_sql_trigger(tmp_path):
    from action_dispatch import _load_profile

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile())
    profile = _load_profile(p)
    assert profile["name"] == "test_profile"


def test_load_profile_raises_on_missing_todoist(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    del base["todoist"]
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="todoist"):
        _load_profile(p)


def test_load_profile_raises_on_missing_trigger_sql(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    del base["trigger"]["sql"]
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="sql"):
        _load_profile(p)


def test_load_profile_raises_when_dedup_missing_from_sql(tmp_path):
    """trigger.sql must contain {{dedup}} token."""
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["trigger"][
        "sql"
    ] = "SELECT cr.id FROM ContentRecords cr ORDER BY cr.processed_at DESC"
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="dedup"):
        _load_profile(p)


def test_load_profile_rejects_inputs_key(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["inputs"] = ["sender", "subject"]
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="inputs"):
        _load_profile(p)


def test_load_profile_rejects_trigger_window_hours(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["trigger"]["window_hours"] = 168
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="window_hours"):
        _load_profile(p)


def test_load_profile_rejects_trigger_filter(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["trigger"]["filter"] = {"labels": ["Investment"]}
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="filter"):
        _load_profile(p)


def test_load_profile_raises_on_missing_file(tmp_path):
    from action_dispatch import _load_profile

    with pytest.raises(FileNotFoundError):
        _load_profile(tmp_path / "ghost.yaml")


def test_load_profile_raises_on_unknown_target(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["target"] = "carrier_pigeon"
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="Unsupported target"):
        _load_profile(p)


def test_load_profile_raises_on_wrong_kind(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["kind"] = "digest"
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="kind"):
        _load_profile(p)


def test_load_profile_raises_when_system_contains_placeholder(tmp_path):
    """`system` must be byte-stable for prompt caching to work — a stray
    {placeholder} would either silently break caching or (since `system` is
    never interpolated) leak the literal braces into the model's system prompt."""
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["system"] = "Hello {sender}, this must not be allowed."
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    with pytest.raises(ValueError, match="sender"):
        _load_profile(p)


def test_load_profile_accepts_placeholder_free_system(tmp_path):
    from action_dispatch import _load_profile

    base = yaml.safe_load(_minimal_profile())
    base["system"] = "Static instructions with no dynamic content."
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    profile = _load_profile(p)
    assert profile["system"] == "Static instructions with no dynamic content."


# ---------------------------------------------------------------------------
# Target registry
# ---------------------------------------------------------------------------


def test_todoist_in_target_registry():
    from action_dispatch import _TARGET_HANDLERS, _TARGET_VALIDATORS, _handle_todoist

    assert _TARGET_HANDLERS["todoist"] is _handle_todoist
    assert "todoist" in _TARGET_VALIDATORS


def test_playwright_handler_not_implemented():
    from action_dispatch import _handle_playwright

    with pytest.raises(NotImplementedError):
        _handle_playwright({"id": 1}, {"target": "playwright"}, {}, {})


# ---------------------------------------------------------------------------
# {{dedup}} substitution
# ---------------------------------------------------------------------------


def test_query_pending_records_substitutes_dedup_token():
    """{{dedup}} in trigger SQL must be replaced with the NOT EXISTS subquery."""
    from action_dispatch import _query_pending_records

    profile = {
        "name": "test_profile",
        "trigger": {
            "sql": (
                "SELECT cr.id, cr.sender FROM ContentRecords cr "
                "WHERE cr.enrichment_status='complete' AND {{dedup}} "
                "ORDER BY cr.processed_at DESC"
            ),
        },
    }

    captured_sql: list[str] = []

    class _FakeCursor:
        def execute(self, sql, *args):
            captured_sql.append(sql)
            self.description = [("id",), ("sender",)]

        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    with patch("action_dispatch.query_sql") as mock_qs:
        mock_qs.return_value = []
        _query_pending_records(profile)

    # query_sql is called with the substituted SQL
    called_sql = mock_qs.call_args[0][0]
    assert "{{dedup}}" not in called_sql
    assert "NOT EXISTS" in called_sql
    assert "ActionRuns" in called_sql
    assert "test_profile" in called_sql


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_render_template_substitutes_known_keys():
    from action_dispatch import _render_template

    result = _render_template(
        "Hello {name}, from {sender}", {"name": "Alice", "sender": "bob@x.com"}
    )
    assert result == "Hello Alice, from bob@x.com"


def test_render_template_empty_string_for_none_value():
    from action_dispatch import _render_template

    result = _render_template(
        "Link: {linked_article_url}", {"linked_article_url": None}
    )
    assert result == "Link: "


def test_render_template_warns_and_empties_unknown_key(caplog):
    from action_dispatch import _render_template
    import logging

    with caplog.at_level(logging.WARNING, logger="action_dispatch"):
        result = _render_template("{missing_key} text", {})
    assert result == " text"
    assert "missing_key" in caplog.text


def test_render_template_leaves_no_braces_in_output():
    from action_dispatch import _render_template

    result = _render_template("{a} and {b}", {"a": "1", "b": "2"})
    assert "{" not in result and "}" not in result


# ---------------------------------------------------------------------------
# Todoist payload building
# ---------------------------------------------------------------------------


def test_build_todoist_payload_prompt_priority_overrides_static():
    from action_dispatch import _build_todoist_payload

    td = {
        "project": "Pro",
        "content": "Do {name}",
        "description": "{description}",
        "priority": 2,
    }
    record = {"sender": "a@b.com"}
    prompt_result = {"name": "Alice", "description": "Brief.", "priority": 4}
    payload = _build_todoist_payload(td, record, prompt_result)
    assert payload["priority"] == 4


def test_build_todoist_payload_static_priority_when_prompt_omits_it():
    from action_dispatch import _build_todoist_payload

    td = {
        "project": "Pro",
        "content": "Do {name}",
        "description": "{description}",
        "priority": 3,
    }
    record = {"sender": "a@b.com"}
    prompt_result = {"name": "Alice", "description": "Brief."}
    payload = _build_todoist_payload(td, record, prompt_result)
    assert payload["priority"] == 3


def test_build_todoist_payload_priority_clamped_to_1_4():
    from action_dispatch import _build_todoist_payload

    td = {"project": "Pro", "content": "{name}", "description": "", "priority": 2}
    payload = _build_todoist_payload(td, {}, {"name": "X", "priority": 99})
    assert payload["priority"] == 4


def test_build_todoist_payload_prompt_overwrites_record_field():
    from action_dispatch import _build_todoist_payload

    td = {"project": "Pro", "content": "{sender}", "description": ""}
    record = {"sender": "original@x.com"}
    prompt_result = {"sender": "override@x.com"}
    payload = _build_todoist_payload(td, record, prompt_result)
    assert payload["content"] == "override@x.com"


# ---------------------------------------------------------------------------
# Aggregate mode: happy path
# ---------------------------------------------------------------------------


def test_run_aggregate_creates_one_task_per_action(tmp_path):
    """_run_aggregate: single LLM call â†’ N tasks, correct ActionResult statuses and action_content."""
    from action_dispatch import _run_aggregate

    records = [
        {
            "id": 1,
            "sender": "x@y.com",
            "subject": "Oil breakout",
            "executive_summary": "CNQ broke resistance.",
        },
        {
            "id": 2,
            "sender": "a@b.com",
            "subject": "Crypto rally",
            "executive_summary": "BTC above 70k.",
        },
        {
            "id": 3,
            "sender": "c@d.com",
            "subject": "Background info",
            "executive_summary": "General update.",
        },
    ]

    llm_response = {
        "actions": [
            {
                "type": "Equity",
                "sector": "Energy",
                "industry": "Oil & Gas E&P",
                "action": "Review CNQ trade before Thursday open",
                "description": "CNQ broke $42.50 resistance.",
                "priority": 3,
                "sources": "StreetAccount",
                "record_ids": [1],
            },
            {
                "type": "Crypto",
                "sector": "Bitcoin",
                "industry": "Bitcoin",
                "action": "Evaluate BTC position sizing",
                "description": "BTC above 70k on volume.",
                "priority": 2,
                "sources": "CryptoFeed",
                "record_ids": [2],
            },
        ]
    }

    base = yaml.safe_load(_minimal_aggregate_profile())
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))

    profile = base
    profile["name"] = "task_investment"

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = llm_response

    mock_todoist = MagicMock()
    mock_todoist.resolve_project_id.return_value = "proj-invest"
    mock_todoist.create_task.return_value = "task-001"

    with patch("action_dispatch.query_prior_actions", return_value=[]):
        results = _run_aggregate(profile, records, mock_llm, {"todoist": mock_todoist})

    # One LLM call
    assert mock_llm.call_json.call_count == 1

    # Two tasks created (one per action)
    assert mock_todoist.create_task.call_count == 2

    # Record 1 and 2 â†’ created; record 3 â†’ skipped (not in any action's record_ids)
    statuses = {r.record_id: r.status for r in results}
    assert statuses[1] == "created"
    assert statuses[2] == "created"
    assert statuses[3] == "skipped"

    # action_content stored on created records
    created = [r for r in results if r.status == "created"]
    for r in created:
        assert r.action_content is not None
        parsed = json.loads(r.action_content)
        assert "action" in parsed


def test_run_aggregate_passes_system_and_marks_it_non_cacheable(tmp_path):
    """Aggregate mode makes exactly one LLM call per profile run, so the next call
    sharing this system text is a full ingest cycle away — well past the cache
    TTL. cacheable must be False so cache_control isn't sent for nothing."""
    from action_dispatch import _run_aggregate

    records = [{"id": 1, "sender": "x@y.com", "subject": "Test", "executive_summary": "X."}]
    profile = yaml.safe_load(_minimal_aggregate_profile())
    profile["name"] = "task_investment"
    profile["system"] = "Static taxonomy and rules."

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {"actions": []}

    with patch("action_dispatch.query_prior_actions", return_value=[]):
        _run_aggregate(profile, records, mock_llm, {})

    call_kwargs = mock_llm.call_json.call_args[1]
    assert call_kwargs.get("system") == "Static taxonomy and rules."
    assert call_kwargs.get("cacheable") is False


def test_run_aggregate_uses_action_max_tokens(tmp_path):
    """Regression (2026-07-27): the aggregate LLM call must use config.ACTION_MAX_TOKENS,
    not the smaller classify/summarize LLM_MAX_TOKENS. Sonnet 5's thinking output could
    exhaust the smaller budget before emitting any JSON, silently zeroing out every
    task_investment run (created=0, no error surfaced above debug-level logs)."""
    from action_dispatch import _run_aggregate
    import config

    records = [{"id": 1, "sender": "x@y.com", "subject": "Test", "executive_summary": "X."}]
    profile = yaml.safe_load(_minimal_aggregate_profile())
    profile["name"] = "task_investment"

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {"actions": []}

    with patch("action_dispatch.query_prior_actions", return_value=[]):
        _run_aggregate(profile, records, mock_llm, {})

    call_kwargs = mock_llm.call_json.call_args[1]
    assert call_kwargs.get("max_tokens") == config.ACTION_MAX_TOKENS
    assert config.ACTION_MAX_TOKENS > config.LLM_MAX_TOKENS


def test_action_one_uses_action_max_tokens():
    """Regression (2026-07-27): per-record action calls must also use ACTION_MAX_TOKENS."""
    from action_dispatch import _action_one
    import config

    profile = yaml.safe_load(_minimal_profile())
    record = {"id": 1, "sender": "x@y.com", "subject": "Test"}

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {"name": "Test", "description": "Test."}

    _action_one(record, profile, mock_llm, {"todoist": MagicMock()})

    call_kwargs = mock_llm.call_json.call_args[1]
    assert call_kwargs.get("max_tokens") == config.ACTION_MAX_TOKENS


def test_action_one_passes_profile_system_prompt():
    """Per-record calls must forward profile['system'] as the system= kwarg so it
    can be prompt-cached across the records this profile processes in a run."""
    from action_dispatch import _action_one

    profile = yaml.safe_load(_minimal_profile())
    profile["system"] = "Static per-profile instructions."
    record = {"id": 1, "sender": "x@y.com", "subject": "Test"}

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {"name": "Test", "description": "Test."}

    _action_one(record, profile, mock_llm, {"todoist": MagicMock()})

    call_kwargs = mock_llm.call_json.call_args[1]
    assert call_kwargs.get("system") == "Static per-profile instructions."
    # Not explicitly set to False here — per_record calls repeat within a run, so
    # the default (cacheable=True) is the correct behavior.
    assert call_kwargs.get("cacheable") is not False


def test_action_one_defaults_to_empty_system_when_profile_has_none():
    from action_dispatch import _action_one

    profile = yaml.safe_load(_minimal_profile())
    record = {"id": 1, "sender": "x@y.com", "subject": "Test"}

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {"name": "Test", "description": "Test."}

    _action_one(record, profile, mock_llm, {"todoist": MagicMock()})

    call_kwargs = mock_llm.call_json.call_args[1]
    assert call_kwargs.get("system") == ""


def test_run_aggregate_llm_failure_returns_empty(tmp_path):
    """If LLM/JSON parse fails, _run_aggregate returns [] so the batch can retry."""
    from action_dispatch import _run_aggregate

    records = [{"id": 1, "sender": "x@y.com", "subject": "Test"}]
    profile = yaml.safe_load(_minimal_aggregate_profile())
    profile["name"] = "task_investment"

    mock_llm = MagicMock()
    mock_llm.call_json.side_effect = ValueError("LLM returned invalid JSON")

    with patch("action_dispatch.query_prior_actions", return_value=[]):
        results = _run_aggregate(profile, records, mock_llm, {})

    assert results == []


def test_run_aggregate_injects_prior_actions_json(tmp_path):
    """prior_actions_json placeholder must be populated from query_prior_actions."""
    from action_dispatch import _run_aggregate

    records = [
        {"id": 1, "sender": "x@y.com", "subject": "Test", "executive_summary": "X."}
    ]
    profile = yaml.safe_load(_minimal_aggregate_profile())
    profile["name"] = "task_investment"

    prior = ['{"action": "Prior action from yesterday"}']

    captured_prompts: list[str] = []

    mock_llm = MagicMock()

    def capture_call(_role, prompt, *args, **kwargs):
        captured_prompts.append(prompt)
        return {"actions": []}

    mock_llm.call_json.side_effect = capture_call

    with patch("action_dispatch.query_prior_actions", return_value=prior) as mock_qa:
        _run_aggregate(profile, records, mock_llm, {})

    mock_qa.assert_called_once_with("task_investment", 74)
    assert len(captured_prompts) == 1
    assert "Prior action from yesterday" in captured_prompts[0]


def test_run_aggregate_unknown_record_ids_warned_and_ignored(tmp_path, caplog):
    """Unknown record_ids in LLM response should be logged and ignored, not raise."""
    from action_dispatch import _run_aggregate
    import logging

    records = [{"id": 1, "sender": "x@y.com", "subject": "Test"}]
    profile = yaml.safe_load(_minimal_aggregate_profile())
    profile["name"] = "task_investment"

    llm_response = {
        "actions": [
            {
                "type": "Equity",
                "sector": "Energy",
                "industry": "Oil & Gas E&P",
                "action": "Do something",
                "description": "Details.",
                "priority": 2,
                "sources": "Test",
                "record_ids": [1, 999],  # 999 does not exist
            }
        ]
    }

    mock_llm = MagicMock()
    mock_llm.call_json.return_value = llm_response

    mock_todoist = MagicMock()
    mock_todoist.resolve_project_id.return_value = "proj-1"
    mock_todoist.create_task.return_value = "task-1"

    with patch("action_dispatch.query_prior_actions", return_value=[]):
        with caplog.at_level(logging.WARNING, logger="action_dispatch"):
            results = _run_aggregate(
                profile, records, mock_llm, {"todoist": mock_todoist}
            )

    assert any("999" in msg for msg in caplog.messages)
    statuses = {r.record_id: r.status for r in results}
    assert statuses[1] == "created"


# ---------------------------------------------------------------------------
# skip_if
# ---------------------------------------------------------------------------


def test_handle_todoist_skips_when_skip_if_truthy():
    from action_dispatch import _handle_todoist

    record = {"id": 42, "sender": "a@b.com"}
    profile = {
        "todoist": {
            "project": "Pro",
            "skip_if": "is_job_alert",
            "content": "{name}",
            "description": "",
            "priority": 3,
        }
    }
    prompt_result = {"is_job_alert": True, "name": "Alice"}
    result = _handle_todoist(record, profile, prompt_result, {})
    assert result.status == "skipped"
    assert result.record_id == 42
    assert result.external_id is None


def test_handle_todoist_proceeds_when_skip_if_false():
    from action_dispatch import _handle_todoist

    record = {"id": 7, "sender": "real@person.com"}
    profile = {
        "todoist": {
            "project": "Pro",
            "skip_if": "is_job_alert",
            "content": "{name}",
            "description": "",
            "priority": 3,
        }
    }
    prompt_result = {"is_job_alert": False, "name": "Bob"}

    mock_todoist = MagicMock()
    mock_todoist.resolve_project_id.return_value = "proj-1"
    mock_todoist.create_task.return_value = "task-99"

    result = _handle_todoist(record, profile, prompt_result, {"todoist": mock_todoist})
    assert result.status == "created"
    assert result.external_id == "task-99"


def test_handle_todoist_no_skip_if_key_proceeds():
    from action_dispatch import _handle_todoist

    record = {"id": 5, "sender": "x@y.com"}
    profile = {
        "todoist": {
            "project": "Pro",
            "content": "{name}",
            "description": "",
            "priority": 2,
        }
    }
    prompt_result = {"name": "Carol"}

    mock_todoist = MagicMock()
    mock_todoist.resolve_project_id.return_value = "proj-2"
    mock_todoist.create_task.return_value = "task-55"

    result = _handle_todoist(record, profile, prompt_result, {"todoist": mock_todoist})
    assert result.status == "created"


# ---------------------------------------------------------------------------
# auto_reply
# ---------------------------------------------------------------------------

_AUTO_REPLY_CFG = {
    "when_key": "action",
    "when_value": "Decline",
    "body": "Declined.\n\nSincerely,\nJames",
    "trash": True,
}


def _auto_reply_profile(**cfg_overrides) -> dict:
    return {
        "name": "task_connection",
        "target": "todoist",
        "todoist": {"project": "Pro", "content": "{name}", "description": "", "priority": 3},
        "auto_reply": {**_AUTO_REPLY_CFG, **cfg_overrides},
    }


def _gmail_record() -> dict:
    return {"id": 11, "source_type": "gmail", "source_ref": "msg-1", "sender": "x@y.com"}


def _mock_gmail(headers: dict | None = None) -> MagicMock:
    gmail = MagicMock()
    gmail.get_message_headers.return_value = {
        "thread_id": "thr-1",
        "headers": headers
        or {
            "from": "Jane Doe <jane@real.com>",
            "subject": "Jane wants to connect",
            "message-id": "<orig@mail>",
        },
    }
    gmail.send_reply.return_value = "sent-1"
    return gmail


def test_load_profile_accepts_auto_reply(tmp_path):
    from action_dispatch import _load_profile

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile({"auto_reply": _AUTO_REPLY_CFG}))
    assert _load_profile(p)["auto_reply"]["when_value"] == "Decline"


@pytest.mark.parametrize("missing", ["when_key", "when_value", "body"])
def test_load_profile_rejects_auto_reply_missing_field(tmp_path, missing):
    from action_dispatch import _load_profile

    cfg = {k: v for k, v in _AUTO_REPLY_CFG.items() if k != missing}
    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile({"auto_reply": cfg}))
    with pytest.raises(ValueError, match=missing):
        _load_profile(p)


def test_load_profile_rejects_blank_auto_reply_body(tmp_path):
    from action_dispatch import _load_profile

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile({"auto_reply": {**_AUTO_REPLY_CFG, "body": "  \n "}}))
    with pytest.raises(ValueError, match="blank"):
        _load_profile(p)


def test_load_profile_rejects_auto_reply_in_aggregate_mode(tmp_path):
    from action_dispatch import _load_profile

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile({"mode": "aggregate", "auto_reply": _AUTO_REPLY_CFG}))
    with pytest.raises(ValueError, match="per_record"):
        _load_profile(p)


def test_connection_profile_loads_with_auto_reply():
    """The shipped profile validates and carries the configured decline text."""
    from pathlib import Path

    from action_dispatch import _load_profile

    profile = _load_profile(Path(__file__).parent.parent / "profiles" / "task_connection.yaml")
    cfg = profile["auto_reply"]
    assert (cfg["when_key"], cfg["when_value"]) == ("action", "Decline")
    assert "I am not an open networker" in cfg["body"]
    assert "cr.source_ref" in profile["trigger"]["sql"]
    assert "invitations@linkedin.com" in cfg["skip_addresses"]


def test_auto_reply_matches_is_exact_but_case_and_whitespace_insensitive():
    from action_dispatch import _auto_reply_matches

    profile = _auto_reply_profile()
    assert _auto_reply_matches(profile, {"action": "Decline"})
    assert _auto_reply_matches(profile, {"action": " decline "})
    assert not _auto_reply_matches(profile, {"action": "Review"})
    assert not _auto_reply_matches(profile, {"action": "Declined"})
    assert not _auto_reply_matches(profile, {"is_job_alert": True})
    assert not _auto_reply_matches({"name": "x"}, {"action": "Decline"})


def test_action_one_decline_replies_instead_of_creating_task():
    from action_dispatch import _action_one

    gmail, todoist = _mock_gmail(), MagicMock()
    llm = MagicMock()
    llm.call_json.return_value = {"action": "Decline", "name": "Jane", "priority": 1}
    profile = _auto_reply_profile(prompt="p {sender}")
    profile["prompt"] = "p {sender}"

    result = _action_one(_gmail_record(), profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.status == "created"
    assert result.external_id == "sent-1"
    assert result.target == "gmail_reply"
    todoist.create_task.assert_not_called()
    gmail.send_reply.assert_called_once()
    gmail.trash_message.assert_called_once_with("msg-1")


def test_action_one_non_decline_still_creates_task():
    from action_dispatch import _action_one

    gmail, todoist = _mock_gmail(), MagicMock()
    todoist.resolve_project_id.return_value = "proj"
    todoist.create_task.return_value = "task-1"
    llm = MagicMock()
    llm.call_json.return_value = {"action": "Review", "name": "Jane"}
    profile = _auto_reply_profile()
    profile["prompt"] = "p {sender}"

    result = _action_one(_gmail_record(), profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.status == "created"
    assert result.external_id == "task-1"
    assert result.target is None
    gmail.send_reply.assert_not_called()
    gmail.trash_message.assert_not_called()


def test_handle_auto_reply_sends_threaded_reply_to_sender_then_trashes():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    result = _handle_auto_reply(
        _gmail_record(), _auto_reply_profile(), {"action": "Decline"}, {"gmail": gmail}
    )

    gmail.get_message_headers.assert_called_once_with("msg-1")
    gmail.send_reply.assert_called_once_with(
        to="jane@real.com",
        subject="Re: Jane wants to connect",
        body="Declined.\n\nSincerely,\nJames",
        thread_id="thr-1",
        in_reply_to="<orig@mail>",
        references=None,
    )
    gmail.trash_message.assert_called_once_with("msg-1")
    assert result.status == "created" and result.error is None


def test_handle_auto_reply_prefers_reply_to_and_does_not_double_re():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail(
        {
            "from": "a@x.com",
            "reply-to": "Jane <jane@real.com>",
            "subject": "RE: hello",
            "message-id": "<m@x>",
            "references": "<r1@x>",
        }
    )
    _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})

    kwargs = gmail.send_reply.call_args.kwargs
    assert kwargs["to"] == "jane@real.com"
    assert kwargs["subject"] == "RE: hello"
    assert kwargs["references"] == "<r1@x>"


def test_handle_auto_reply_strips_header_injection():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail(
        {
            "from": "a@x.com",
            "subject": "hi\r\nBcc: victim@evil.com",
            "message-id": "<m@x>\r\nX-Evil: 1",
        }
    )
    _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})

    kwargs = gmail.send_reply.call_args.kwargs
    assert "\n" not in kwargs["subject"] and "\r" not in kwargs["subject"]
    assert "\n" not in kwargs["in_reply_to"]


def test_handle_auto_reply_skips_trash_when_disabled():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    result = _handle_auto_reply(
        _gmail_record(), _auto_reply_profile(trash=False), {}, {"gmail": gmail}
    )
    assert result.status == "created"
    gmail.trash_message.assert_not_called()


def test_handle_auto_reply_send_failure_fails_without_trashing():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    gmail.send_reply.side_effect = RuntimeError("boom")
    result = _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})

    assert result.status == "failed"
    assert "boom" in result.error
    assert result.target == "gmail_reply"
    gmail.trash_message.assert_not_called()


def test_handle_auto_reply_trash_failure_after_send_stays_created():
    """The email is already out, so the record must not be marked failed/retried."""
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    gmail.trash_message.side_effect = RuntimeError("trash down")
    result = _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})

    assert result.status == "created"
    assert result.external_id == "sent-1"
    assert "trash failed" in result.error


def test_handle_auto_reply_no_usable_address_does_nothing_and_returns_none():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail({"from": "undisclosed-recipients", "subject": "x"})
    result = _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})

    assert result is None
    gmail.send_reply.assert_not_called()
    gmail.trash_message.assert_not_called()


_LINKEDIN_SKIP = ["*noreply*", "invitations@linkedin.com"]


@pytest.mark.parametrize(
    "sender",
    [
        "Matthew Conti <invitations@linkedin.com>",
        '"Jim (via LinkedIn)" <messages-noreply@linkedin.com>',
        "Invitations <INVITATIONS@LinkedIn.com>",
    ],
)
def test_handle_auto_reply_skips_linkedin_relay_senders_without_side_effects(sender):
    """Real senders from ActionRuns: a reply to these never reaches the person."""
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail({"from": sender, "subject": "I want to connect"})
    profile = _auto_reply_profile(skip_addresses=_LINKEDIN_SKIP)
    profile["auto_reply"]["linkedin_ignore"] = {"url_key": "decline_url"}
    with patch("linkedin_client.ignore_invitation") as ignore:
        result = _handle_auto_reply(
            _gmail_record(), profile, {"decline_url": "https://www.linkedin.com/x"}, {"gmail": gmail}
        )

    assert result is None
    gmail.send_reply.assert_not_called()
    gmail.trash_message.assert_not_called()
    ignore.assert_not_called()


def test_handle_auto_reply_uses_reply_to_when_from_is_skipped():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail(
        {"from": "Matt <invitations@linkedin.com>", "reply-to": "Matt <matt@real.com>", "subject": "x"}
    )
    result = _handle_auto_reply(
        _gmail_record(), _auto_reply_profile(skip_addresses=_LINKEDIN_SKIP), {}, {"gmail": gmail}
    )

    assert result.status == "created"
    assert gmail.send_reply.call_args.kwargs["to"] == "matt@real.com"


def test_action_one_falls_back_to_task_when_no_deliverable_address():
    from action_dispatch import _action_one

    gmail = _mock_gmail({"from": "Matt <invitations@linkedin.com>", "subject": "x"})
    todoist = MagicMock()
    todoist.resolve_project_id.return_value = "proj"
    todoist.create_task.return_value = "task-7"
    llm = MagicMock()
    llm.call_json.return_value = {"action": "Decline", "name": "Matt"}
    profile = _auto_reply_profile(skip_addresses=_LINKEDIN_SKIP)
    profile["prompt"] = "p {sender}"

    result = _action_one(_gmail_record(), profile, llm, {"gmail": gmail, "todoist": todoist})

    assert result.status == "created" and result.external_id == "task-7"
    assert result.target is None
    gmail.send_reply.assert_not_called()
    gmail.trash_message.assert_not_called()


def test_load_profile_rejects_non_list_skip_addresses(tmp_path):
    from action_dispatch import _load_profile

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile({"auto_reply": {**_AUTO_REPLY_CFG, "skip_addresses": "*noreply*"}}))
    with pytest.raises(ValueError, match="skip_addresses"):
        _load_profile(p)


@pytest.mark.parametrize(
    "record",
    [
        {"id": 1, "sender": "x@y.com"},
        {"id": 1, "source_type": "file", "source_ref": "/tmp/a.pdf"},
        {"id": 1, "source_type": "gmail", "source_ref": None},
    ],
)
def test_handle_auto_reply_requires_gmail_source(record):
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    result = _handle_auto_reply(record, _auto_reply_profile(), {}, {"gmail": gmail})
    assert result.status == "failed"
    gmail.send_reply.assert_not_called()


def test_handle_auto_reply_gmail_read_failure_fails_without_sending():
    from action_dispatch import _handle_auto_reply

    gmail = _mock_gmail()
    gmail.get_message_headers.side_effect = RuntimeError("404")
    result = _handle_auto_reply(_gmail_record(), _auto_reply_profile(), {}, {"gmail": gmail})
    assert result.status == "failed"
    gmail.send_reply.assert_not_called()


def test_run_action_records_gmail_reply_target(tmp_path):
    """ActionRuns.target reflects the auto-reply, not the profile's todoist target."""
    import action_dispatch

    profile_path = tmp_path / "p.yaml"
    profile_path.write_text(
        _minimal_profile({"auto_reply": _AUTO_REPLY_CFG, "name": "task_connection"})
    )
    llm = MagicMock()
    llm.call_json.return_value = {"action": "Decline"}
    gmail = _mock_gmail()

    with patch.object(action_dispatch, "_query_pending_records", return_value=[_gmail_record()]), \
         patch.object(action_dispatch, "LLMClient", return_value=llm), \
         patch.object(action_dispatch, "TodoistClient", return_value=MagicMock()), \
         patch("gmail_client.GmailClient", return_value=gmail), \
         patch.object(action_dispatch, "_insert_action_run") as insert:
        action_dispatch.run_action(profile_path)

    kwargs = insert.call_args.kwargs
    assert kwargs["target"] == "gmail_reply"
    assert kwargs["status"] == "created"
    assert kwargs["external_id"] == "sent-1"


# ---------------------------------------------------------------------------
# TodoistClient
# ---------------------------------------------------------------------------


def test_todoist_client_raises_on_empty_token():
    from todoist_client import TodoistClient, TodoistError

    with patch("security_config.TODOIST_API_TOKEN", ""):
        with pytest.raises(TodoistError, match="TODOIST_API_TOKEN"):
            TodoistClient()


def test_resolve_project_id_returns_existing(requests_mock):
    from todoist_client import TodoistClient

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.get(
        "https://api.todoist.com/api/v1/projects",
        json=[{"id": "123", "name": "Professional"}, {"id": "456", "name": "Other"}],
    )
    pid = client.resolve_project_id("Professional")
    assert pid == "123"


def test_resolve_project_id_autocreates_missing(requests_mock):
    from todoist_client import TodoistClient

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.get(
        "https://api.todoist.com/api/v1/projects",
        json=[{"id": "456", "name": "Other"}],
    )
    requests_mock.post(
        "https://api.todoist.com/api/v1/projects",
        json={"id": "789", "name": "NewProject"},
        status_code=200,
    )
    pid = client.resolve_project_id("NewProject")
    assert pid == "789"


def test_resolve_project_id_cached_on_second_call(requests_mock):
    from todoist_client import TodoistClient

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.get(
        "https://api.todoist.com/api/v1/projects",
        json=[{"id": "123", "name": "Professional"}],
    )
    client.resolve_project_id("Professional")
    client.resolve_project_id("Professional")
    assert requests_mock.call_count == 1


def test_create_task_returns_id(requests_mock):
    from todoist_client import TodoistClient

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.post(
        "https://api.todoist.com/api/v1/tasks",
        json={"id": "task-001", "content": "Do something"},
        status_code=200,
    )
    task_id = client.create_task(project_id="123", content="Do something", priority=3)
    assert task_id == "task-001"


def test_create_task_raises_on_http_error(requests_mock):
    from todoist_client import TodoistClient, TodoistError

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.post(
        "https://api.todoist.com/api/v1/tasks",
        status_code=400,
        text="Bad Request",
    )
    with pytest.raises(TodoistError):
        client.create_task(project_id="123", content="x")


# ---------------------------------------------------------------------------
# to_api_priority — unit tests
# ---------------------------------------------------------------------------


def test_to_api_priority_happy_path():
    from todoist_client import to_api_priority

    assert to_api_priority(1) == 4
    assert to_api_priority(2) == 3
    assert to_api_priority(3) == 2
    assert to_api_priority(4) == 1


def test_to_api_priority_none_returns_none():
    from todoist_client import to_api_priority

    assert to_api_priority(None) is None


def test_to_api_priority_empty_string_returns_none():
    from todoist_client import to_api_priority

    assert to_api_priority("") is None


def test_to_api_priority_string_coercion():
    from todoist_client import to_api_priority

    assert to_api_priority("3") == 2


def test_to_api_priority_non_numeric_string_returns_none_and_warns(caplog):
    from todoist_client import to_api_priority
    import logging

    with caplog.at_level(logging.WARNING, logger="todoist_client"):
        result = to_api_priority("high")
    assert result is None
    assert "high" in caplog.text


def test_to_api_priority_below_range_clamps_and_warns(caplog):
    from todoist_client import to_api_priority
    import logging

    with caplog.at_level(logging.WARNING, logger="todoist_client"):
        result = to_api_priority(0)
    assert result == 4  # clamped to 1, then 5-1=4
    assert caplog.text  # warning was emitted


def test_to_api_priority_above_range_clamps_and_warns(caplog):
    from todoist_client import to_api_priority
    import logging

    with caplog.at_level(logging.WARNING, logger="todoist_client"):
        result = to_api_priority(5)
    assert result == 1  # clamped to 4, then 5-4=1
    assert caplog.text  # warning was emitted


def test_create_task_omits_priority_when_none(requests_mock):
    """When to_api_priority returns None, the priority key must not appear in the request body."""
    from todoist_client import TodoistClient
    import json as _json

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.post(
        "https://api.todoist.com/api/v1/tasks",
        json={"id": "task-002", "content": "No priority"},
        status_code=200,
    )
    client.create_task(project_id="123", content="No priority", priority=None)

    sent_body = _json.loads(requests_mock.last_request.body)
    assert "priority" not in sent_body


def test_create_task_sends_inverted_priority(requests_mock):
    """Human priority 1 must reach the API as 4 (most urgent in REST scale)."""
    from todoist_client import TodoistClient
    import json as _json

    with patch("security_config.TODOIST_API_TOKEN", "tok"):
        client = TodoistClient()

    requests_mock.post(
        "https://api.todoist.com/api/v1/tasks",
        json={"id": "task-003", "content": "Urgent"},
        status_code=200,
    )
    client.create_task(project_id="123", content="Urgent", priority=1)

    sent_body = _json.loads(requests_mock.last_request.body)
    assert sent_body["priority"] == 4
