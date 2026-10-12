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


# ---------------------------------------------------------------------------
# Daily heartbeat
# ---------------------------------------------------------------------------


def _heartbeat_profile_file(tmp_path, heartbeat=None):
    base = yaml.safe_load(_minimal_profile())
    base["heartbeat"] = heartbeat or {
        "project": "Business",
        "content": "Check {date}: {headline}",
        "priority": 4,
        "alert_priority": 1,
        "stats_sql": "SELECT 7 AS records_total",
    }
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.dump(base))
    return p


def _run_with_heartbeat(
    tmp_path,
    *,
    claimed=True,
    trigger_result=None,
    trigger_error=None,
    stats_error=None,
    create_result=("task-1", None),
    cycle_note=None,
):
    """Run run_action with every external dependency mocked; return the mocks."""
    import action_dispatch
    from db import DbError

    def fake_query_sql(sql, *a, **k):
        if "records_total" in sql:
            if stats_error:
                raise DbError(stats_error)
            return [{"records_total": 7}]
        if trigger_error:
            raise DbError(trigger_error)
        return trigger_result or []

    with patch("action_dispatch.query_sql", side_effect=fake_query_sql), \
         patch("action_dispatch.claim_heartbeat", return_value=claimed) as claim, \
         patch("action_dispatch.release_heartbeat") as release, \
         patch("action_dispatch.TodoistClient"), \
         patch("action_dispatch.LLMClient"), \
         patch("action_dispatch._action_one") as action_one, \
         patch("action_dispatch._insert_action_run"), \
         patch("action_dispatch._create_todoist_task", return_value=create_result) as create:
        action_one.side_effect = lambda rec, *a: action_dispatch.ActionResult(rec["id"], "created", "t")
        results = action_dispatch.run_action(
            _heartbeat_profile_file(tmp_path), cycle_note=cycle_note
        )
    return results, claim, release, create


def test_load_profile_accepts_heartbeat(tmp_path):
    from action_dispatch import _load_profile

    assert "heartbeat" in _load_profile(_heartbeat_profile_file(tmp_path))


@pytest.mark.parametrize("missing", ["project", "content"])
def test_load_profile_heartbeat_requires_project_and_content(tmp_path, missing):
    from action_dispatch import _load_profile

    hb = {"project": "Business", "content": "x"}
    del hb[missing]
    with pytest.raises(ValueError, match=missing):
        _load_profile(_heartbeat_profile_file(tmp_path, hb))


def test_real_rights_offering_profile_loads():
    from pathlib import Path
    from action_dispatch import _load_profile

    profile = _load_profile(
        Path(__file__).parent.parent / "profiles" / "task_rights_offering.yaml"
    )
    assert profile["heartbeat"]["stats_sql"]


def test_heartbeat_sent_when_no_records_found(tmp_path):
    results, claim, _, create = _run_with_heartbeat(tmp_path)
    assert results == []
    claim.assert_called_once_with("test_profile")
    payload = create.call_args[0][0]
    assert "no new records" in payload["content"]
    assert payload["priority"] == 4
    assert "records_total: 7" in payload["description"]
    assert "**Status:** OK" in payload["description"]


def test_heartbeat_reports_created_tasks(tmp_path):
    _, _, _, create = _run_with_heartbeat(
        tmp_path, trigger_result=[{"id": 1}, {"id": 2}]
    )
    payload = create.call_args[0][0]
    assert "2 task(s) created" in payload["content"]
    assert payload["priority"] == 4


def test_heartbeat_sent_and_escalated_when_trigger_query_fails(tmp_path):
    results, _, _, create = _run_with_heartbeat(tmp_path, trigger_error="boom")
    assert results == []
    payload = create.call_args[0][0]
    assert "NEEDS ATTENTION" in payload["content"]
    assert payload["priority"] == 1
    assert "boom" in payload["description"]


def test_heartbeat_escalated_when_a_record_failed(tmp_path):
    import action_dispatch

    failed = action_dispatch.ActionResult(9, "failed", error="LLM error: x")
    with patch("action_dispatch.query_sql", side_effect=lambda sql, *a, **k: [{"records_total": 1}] if "records_total" in sql else [{"id": 9}]), \
         patch("action_dispatch.claim_heartbeat", return_value=True), \
         patch("action_dispatch.TodoistClient"), patch("action_dispatch.LLMClient"), \
         patch("action_dispatch._insert_action_run"), \
         patch("action_dispatch._action_one", return_value=failed), \
         patch("action_dispatch._create_todoist_task", return_value=("t", None)) as create:
        action_dispatch.run_action(_heartbeat_profile_file(tmp_path))
    payload = create.call_args[0][0]
    assert payload["priority"] == 1
    assert "#9 LLM error: x" in payload["description"]


def test_heartbeat_includes_cycle_note_and_escalates(tmp_path):
    _, _, _, create = _run_with_heartbeat(tmp_path, cycle_note="Gmail ingest failed this cycle")
    payload = create.call_args[0][0]
    assert payload["priority"] == 1
    assert "Gmail ingest failed this cycle" in payload["description"]


def test_heartbeat_stats_failure_is_reported_not_fatal(tmp_path):
    _, _, _, create = _run_with_heartbeat(tmp_path, stats_error="no such table")
    payload = create.call_args[0][0]
    assert payload["priority"] == 1
    assert "Feed stats query failed" in payload["description"]


def test_heartbeat_not_created_when_already_claimed_today(tmp_path):
    _, _, _, create = _run_with_heartbeat(tmp_path, claimed=False)
    create.assert_not_called()


def test_heartbeat_claim_released_when_task_creation_fails(tmp_path):
    _, _, release, _ = _run_with_heartbeat(tmp_path, create_result=(None, "Todoist down"))
    release.assert_called_once_with("test_profile")


def test_heartbeat_sent_when_processing_raises_and_exception_propagates(tmp_path):
    import action_dispatch

    with patch("action_dispatch.query_sql", side_effect=lambda sql, *a, **k: [{"records_total": 1}] if "records_total" in sql else [{"id": 1}]), \
         patch("action_dispatch.claim_heartbeat", return_value=True), \
         patch("action_dispatch.TodoistClient"), \
         patch("action_dispatch.LLMClient", side_effect=RuntimeError("no api key")), \
         patch("action_dispatch._create_todoist_task", return_value=("t", None)) as create:
        with pytest.raises(RuntimeError, match="no api key"):
            action_dispatch.run_action(_heartbeat_profile_file(tmp_path))
    payload = create.call_args[0][0]
    assert payload["priority"] == 1
    assert "no api key" in payload["description"]


def test_profile_without_heartbeat_never_claims(tmp_path):
    import action_dispatch

    p = tmp_path / "profile.yaml"
    p.write_text(_minimal_profile())
    with patch("action_dispatch.query_sql", return_value=[]), \
         patch("action_dispatch.claim_heartbeat") as claim:
        action_dispatch.run_action(p)
    claim.assert_not_called()


def test_orchestrator_runs_action_profiles_when_ingest_raises():
    import ingest_orchestrator

    with patch.object(ingest_orchestrator, "_load_orchestration_config",
                      return_value={"gmail_ingest": True, "post_ingest_actions": ["profiles/x.yaml"]}), \
         patch.object(ingest_orchestrator, "check_token_expiration", return_value=None), \
         patch.object(ingest_orchestrator, "run_ingest", side_effect=RuntimeError("gmail down")), \
         patch.object(ingest_orchestrator, "run_action", return_value=[]) as run_action:
        ingest_orchestrator.run_cycle()
    run_action.assert_called_once()
    assert "gmail down" in run_action.call_args.kwargs["cycle_note"]
