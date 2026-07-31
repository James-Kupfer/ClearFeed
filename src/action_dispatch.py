"""Stage 4: Action dispatch for ClearFeed — post-classification task creation.

Two modes:
  per_record (default) — one LLM call + one task per matching record.
  aggregate            — one LLM call over all records → N tasks (one per
                         Type/Sector/Industry group in the LLM response JSON).

Usage:
    clearfeed.bat action profiles\task_connection.yaml
    python -m action_dispatch profiles\task_connection.yaml

YAML profile schema:
    kind: action             # pipeline discriminator (validated; optional)
    name: task_connection
    target: todoist          # selects the per-record handler (see _TARGET_HANDLERS)
    mode: per_record         # per_record | aggregate (default: per_record)
    model: sonnet            # optional; overrides config.LLM_ROUTING["action"]

    trigger:
      prior_actions_hours: 74   # aggregate only: inject last N hours of action_content
      sql: |
        SELECT cr.id, cr.sender, cr.subject, cr.executive_summary,
               cr.body_text, cr.linked_article_url
        FROM ContentRecords cr
        WHERE cr.enrichment_status = 'complete'
          AND cr.processed_at >= (now() AT TIME ZONE 'America/Chicago') - interval '26 hours'
          AND {{dedup}}
          AND EXISTS (SELECT 1 FROM RecordTerms rt1 WHERE rt1.id = cr.id
                      AND rt1.kind = 'label' AND rt1.value = 'Professional')
          AND EXISTS (SELECT 1 FROM RecordTerms rt2 WHERE rt2.id = cr.id
                      AND rt2.kind = 'tag' AND rt2.value = 'connection_request')
        ORDER BY cr.processed_at DESC LIMIT 50

    todoist:                 # config block named by `target`; validated per-target
      project: "Professional"
      section: null          # optional Todoist section name
      skip_if: "is_job_alert"  # per_record only: skip when prompt JSON key is truthy
      priority: 3            # static default 1-4; prompt JSON may override
      labels: [clearfeed]
      due_string: null
      content: "Respond to {name}"   # {placeholders} resolved
      description: |
        {description}

    prompt: |                # LAST entry
      # per_record: interpolated with record column {placeholders}
      # aggregate:  {records_json} and {prior_actions_json} are available
      ...return strict JSON

SQL conventions:
  - Use alias 'cr' for ContentRecords.
  - {{dedup}} is required and is replaced with the NOT EXISTS ActionRuns clause.
    For aggregate mode with legacy context rows, use:
      AND (cr.received_at < (now() AT TIME ZONE 'America/Chicago') - interval '24 hours' OR {{dedup}})
    so legacy rows come through as context despite prior ActionRuns.
  - Record dict keys = SQL column aliases → available as {placeholders} in templates.

Aggregate JSON contract:
  {"actions": [{"type":..., "sector":..., "industry":..., "action":...,
                "description":..., "priority":1-4, "sources":...,
                "record_ids":[<ids of contributing current records>]}]}
  record_ids must reference ids from the input; unrecognised ids are ignored.

Adding a target: write a handler + validator + optional client factory and
register in _TARGET_HANDLERS / _TARGET_VALIDATORS / _TARGET_CLIENTS.
"""

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from db import DbError, get_connection, query_sql, query_prior_actions
from llm_client import LLMClient
from todoist_client import TodoistClient, TodoistError
from utils import (
    normalize_tag,
    purge_old_logs,
    read_yaml_profile,
    setup_logging,
)

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    record_id: int
    status: str        # created | skipped | failed
    external_id: str | None = None
    error: str | None = None
    action_content: str | None = None  # aggregate mode: JSON of the action object


# ------------------------------------------------------------------
# Profile loading
# ------------------------------------------------------------------


def _load_profile(path: Path) -> dict:
    """Load and validate a YAML action profile. Returns the profile dict.

    Validation is target-aware: the per-target config block (named by the
    profile's `target`, e.g. `todoist:`) is validated by that target's handler
    via `_TARGET_VALIDATORS`.
    """
    profile = read_yaml_profile(path)

    kind = profile.get("kind")
    if kind is not None and kind != "action":
        raise ValueError(f"Expected kind: action, got {kind!r}")

    if "inputs" in profile:
        raise ValueError(
            "Profile key 'inputs' is no longer supported. "
            "Record columns are determined by the SELECT list in 'trigger.sql'. "
            "See README for the new profile schema."
        )

    for required in ("name", "target", "trigger", "prompt"):
        if required not in profile:
            raise ValueError(f"Profile missing required field: {required!r}")

    target = profile["target"]
    if target not in _TARGET_HANDLERS:
        raise ValueError(
            f"Unsupported target {target!r}. Supported: {sorted(_TARGET_HANDLERS)}"
        )
    if target not in profile:
        raise ValueError(f"Profile missing required config block: {target!r}")

    trigger = profile["trigger"]
    for old_key in ("window_hours", "filter", "limit"):
        if old_key in trigger:
            raise ValueError(
                f"Trigger key {old_key!r} is no longer supported. "
                "Use 'sql:' with {{dedup}} placeholder instead. See README."
            )
    if "sql" not in trigger:
        raise ValueError("Profile trigger missing required field: 'sql'")
    if "{{dedup}}" not in trigger["sql"]:
        raise ValueError(
            "Trigger SQL must contain {{dedup}} placeholder — it is replaced with "
            "the ActionRuns deduplication clause at runtime. See README."
        )

    validator = _TARGET_VALIDATORS.get(target)
    if validator is not None:
        validator(profile[target])

    return profile


# ------------------------------------------------------------------
# DB queries
# ------------------------------------------------------------------


def _query_pending_records(profile: dict) -> list[dict]:
    """Execute the trigger SQL, substituting {{dedup}} with the ActionRuns exclusion.

    The dedup clause prevents re-actioning records already in ActionRuns for
    this profile. Profiles may use partial dedup (e.g. legacy context rows)
    by wrapping {{dedup}} in a condition:
        AND (legacy_condition OR {{dedup}})
    """
    profile_name: str = profile["name"]
    dedup_clause = (
        "NOT EXISTS (SELECT 1 FROM ActionRuns ar "
        f"WHERE ar.record_id = cr.id AND ar.profile_name = '{profile_name}')"
    )
    sql = profile["trigger"]["sql"].replace("{{dedup}}", dedup_clause)
    try:
        return query_sql(sql)
    except Exception as exc:
        raise DbError(f"DB error querying pending action records: {exc}") from exc


def _insert_action_run(
    profile_name: str,
    record_id: int,
    target: str,
    external_id: str | None,
    status: str,
    error: str | None = None,
    action_content: str | None = None,
) -> None:
    """Record the outcome of a single action attempt."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ActionRuns "
                "(profile_name, record_id, target, external_id, status, error, action_content) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    profile_name,
                    record_id,
                    target,
                    external_id,
                    status,
                    (error or "")[:1000] or None,
                    action_content,
                ),
            )
    except Exception as exc:
        raise DbError(f"DB error inserting ActionRun: {exc}") from exc


# ------------------------------------------------------------------
# Template rendering
# ------------------------------------------------------------------


def _render_template(template: str, namespace: dict[str, Any]) -> str:
    """Replace {key} placeholders in template with values from namespace.

    Unknown keys render as empty string with a warning. Values are coerced to
    str; None becomes empty string.
    """
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in namespace:
            log.warning("Template placeholder {%s} not found in namespace — rendering empty", key)
            return ""
        val = namespace[key]
        return "" if val is None else str(val)

    return re.sub(r"\{(\w+)\}", _replace, template)


def _build_prompt_text(profile: dict, record: dict) -> str:
    """Interpolate the profile's prompt template with record input values."""
    return _render_template(profile["prompt"], record)


def _build_todoist_payload(
    td_config: dict, record: dict, prompt_result: dict
) -> dict:
    """Merge record fields + prompt JSON into a flat namespace; render templates.

    prompt_result values shadow record values on key collision (prompt wins).
    """
    from datetime import date
    namespace: dict[str, Any] = {k: v for k, v in record.items()}
    namespace["today"] = date.today().strftime("%m/%d/%Y")
    namespace.update(prompt_result)

    content = _render_template(td_config["content"], namespace)
    description = _render_template(str(td_config.get("description") or ""), namespace)

    # Priority: prompt JSON overrides static config if present and valid.
    # Aggregate profiles use a "{priority}" template string — treat non-integer
    # static values as absent (prompt_result supplies priority instead).
    _raw_static = td_config.get("priority")
    try:
        static_priority: int = int(_raw_static) if _raw_static is not None else 1
    except (TypeError, ValueError):
        static_priority = 1
    raw_priority = prompt_result.get("priority")
    try:
        priority = int(raw_priority) if raw_priority is not None else static_priority
        priority = max(1, min(4, priority))
    except (TypeError, ValueError):
        priority = static_priority

    due_date: str | None = None
    fwd = td_config.get("forward_due_date")
    if fwd is not None:
        from datetime import timedelta
        due_date = (date.today() + timedelta(days=int(fwd))).strftime("%Y-%m-%d")

    # due_string: prompt_result overrides static td_config (dynamic per-action due dates)
    due_string = prompt_result.get("due_string") or td_config.get("due_string") or None

    return {
        "project": td_config["project"],
        "section": td_config.get("section"),
        "content": content,
        "description": description.strip() or None,
        "priority": priority,
        "labels": td_config.get("labels") or [],
        "due_date": due_date,
        "due_string": due_string,
        "reminder_minutes": prompt_result.get("reminder_minutes"),
    }


# ------------------------------------------------------------------
# Per-record action
# ------------------------------------------------------------------


def _action_one(
    record: dict,
    profile: dict,
    llm: LLMClient,
    clients: dict,
) -> ActionResult:
    """Run the prompt → target-handler pipeline for a single record.

    Common steps (prompt call) live here; the final "do the thing" step is
    delegated to the handler registered for the profile's `target`.
    """
    record_id: int = record["id"]
    context = f"record_id={record_id}"

    # Run the LLM prompt
    prompt_text = _build_prompt_text(profile, record)
    try:
        prompt_result = llm.call_json(
            "action",
            prompt_text,
            model_override=profile.get("model"),
            max_tokens=config.ACTION_MAX_TOKENS,
        )
    except Exception as exc:
        return ActionResult(record_id, "failed", error=f"LLM error: {exc}")

    log.info("[action] %s prompt result keys: %s", context, list(prompt_result.keys()))

    handler = _TARGET_HANDLERS[profile["target"]]
    return handler(record, profile, prompt_result, clients)


# ------------------------------------------------------------------
# Target handlers — one per `target`. To add a capability (e.g. Playwright),
# write a handler with this signature, a validator, and register both below.
#   handler(record, profile, prompt_result, clients) -> ActionResult
# `clients` holds lazily-created target clients keyed by target name.
# ------------------------------------------------------------------


def _validate_todoist(td: dict) -> None:
    """Validate the `todoist:` config block."""
    for field in ("project", "content", "description"):
        if field not in td:
            raise ValueError(f"Profile todoist section missing required field: {field!r}")


def _create_todoist_task(payload: dict, clients: dict) -> tuple[str | None, str | None]:
    """Create a Todoist task from a pre-built payload dict.

    Returns (task_id, error_str). One of the two is always None.
    """
    from datetime import datetime, timedelta, timezone
    todoist: TodoistClient = clients["todoist"]
    try:
        project_id = todoist.resolve_project_id(payload["project"])
        section_id: str | None = None
        if payload.get("section"):
            section_id = todoist.resolve_section_id(project_id, payload["section"])
        task_id = todoist.create_task(
            project_id=project_id,
            content=payload["content"],
            description=payload.get("description"),
            priority=payload["priority"],
            labels=payload.get("labels") or None,
            due_string=payload.get("due_string"),
            due_date=payload.get("due_date"),
            section_id=section_id,
        )
        reminder_minutes = payload.get("reminder_minutes")
        if reminder_minutes is not None:
            try:
                remind_at = datetime.now(tz=timezone.utc) + timedelta(minutes=int(reminder_minutes))
                todoist.create_reminder(task_id, remind_at.strftime("%Y-%m-%dT%H:%M:%S"))
            except Exception as exc:
                log.warning("[todoist] reminder creation failed for task %s: %s", task_id, exc)
        return task_id, None
    except TodoistError as exc:
        return None, str(exc)


def _handle_todoist(
    record: dict, profile: dict, prompt_result: dict, clients: dict
) -> ActionResult:
    """Create a Todoist task from the record + prompt output.

    Honors `todoist.skip_if`: if that key names a field in prompt_result that is
    truthy, the record is skipped (ActionRun written as 'skipped'; no task created).
    """
    record_id: int = record["id"]
    td_config: dict = profile["todoist"]

    skip_key = td_config.get("skip_if")
    if skip_key and prompt_result.get(skip_key):
        log.info(
            "[action] record_id=%d skipped — prompt returned %s=true", record_id, skip_key
        )
        return ActionResult(record_id, "skipped")

    try:
        payload = _build_todoist_payload(td_config, record, prompt_result)
    except Exception as exc:
        return ActionResult(record_id, "failed", error=f"Template error: {exc}")

    task_id, error = _create_todoist_task(payload, clients)
    if error:
        return ActionResult(record_id, "failed", error=f"Todoist error: {error}")
    return ActionResult(record_id, "created", external_id=task_id)


def _validate_playwright(cfg: dict) -> None:
    """Validate the `playwright:` config block. Placeholder — defines the seam.

    Expected (future) keys: e.g. `script` (path to a Playwright routine),
    `url`, and any step parameters the routine needs. Wire real validation in
    when the handler is implemented.
    """
    return None


def _handle_playwright(
    record: dict, profile: dict, prompt_result: dict, clients: dict
) -> ActionResult:
    """Placeholder for browser-automation actions via Playwright.

    Reuses the entire action pipeline (record scan, per-record prompt, ActionRuns
    dedup); only this final step differs. Implement by driving a Playwright
    routine with `prompt_result` + record fields, then return an ActionResult.
    """
    raise NotImplementedError("playwright target not yet implemented")


# target name → (handler, validator). Adding a capability = add one row.
_TARGET_HANDLERS = {
    "todoist": _handle_todoist,
    "playwright": _handle_playwright,
}
_TARGET_VALIDATORS = {
    "todoist": _validate_todoist,
    "playwright": _validate_playwright,
}

# target name → factory building the client this target needs (lazy, once per run).
_TARGET_CLIENTS = {
    "todoist": lambda: TodoistClient(),
}


# ------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------


def _is_legacy_row(row: dict) -> bool:
    """Return True if the row is a legacy context row (not to be actioned)."""
    v = row.get("legacy")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return False


def _run_aggregate(
    profile: dict,
    records: list[dict],
    llm: LLMClient,
    clients: dict,
) -> list[ActionResult]:
    """Aggregate mode: one LLM call over all records returns N tasks.

    Records marked legacy=1/True are passed to the LLM as context-only;
    they are never written to ActionRuns. Only current (non-legacy) records
    are tracked in ActionRuns.

    On LLM/parse failure: returns [] so the run is retried next cycle.
    On per-task failure: the contributing record_ids get status 'failed'.
    Records in the batch not referenced by any action get status 'skipped'.
    """
    import json as _json
    from datetime import date

    profile_name: str = profile["name"]
    target: str = profile["target"]
    td_config: dict = profile[target]

    current_ids: set[int] = {r["id"] for r in records if not _is_legacy_row(r)}

    # Inject prior-action context
    prior_hours = int(profile["trigger"].get("prior_actions_hours", 0))
    prior_actions: list[str] = []
    if prior_hours > 0:
        try:
            prior_actions = query_prior_actions(profile_name, prior_hours)
        except DbError as exc:
            log.warning("[aggregate] Could not load prior actions: %s", exc)

    from datetime import date as _date
    _today = _date.today()

    records_json = _json.dumps(records, indent=2, default=str)
    prior_actions_json = _json.dumps(prior_actions, indent=2, default=str)

    prompt_text = _render_template(
        profile["prompt"],
        {
            "records_json": records_json,
            "prior_actions_json": prior_actions_json,
            "today_date": _today.strftime("%Y-%m-%d"),
            "today_dow": _today.strftime("%A"),
        },
    )

    # LLM failure → no ActionRuns written; batch retries next run
    try:
        result = llm.call_json(
            "action",
            prompt_text,
            model_override=profile.get("model"),
            max_tokens=config.ACTION_MAX_TOKENS,
        )
    except Exception as exc:
        log.error("[aggregate] LLM call failed — no ActionRuns written: %s", exc)
        return []

    actions = result.get("actions")
    if not isinstance(actions, list):
        log.error("[aggregate] LLM response missing 'actions' list — no ActionRuns written")
        return []

    id_to_result: dict[int, ActionResult] = {}

    for action in actions:
        raw_ids = action.get("record_ids", [])
        valid_ids = [rid for rid in raw_ids if rid in current_ids]
        if not valid_ids:
            log.warning(
                "[aggregate] Action %r has no valid current record_ids — skipping",
                action.get("action"),
            )
            continue
        unknown = [rid for rid in raw_ids if rid not in current_ids]
        if unknown:
            log.warning("[aggregate] LLM returned unknown record_ids %s — ignored", unknown)

        namespace: dict[str, Any] = dict(action)
        namespace["today"] = date.today().strftime("%m/%d/%Y")
        try:
            payload = _build_todoist_payload(td_config, {}, namespace)
        except Exception as exc:
            err = f"Template error: {exc}"
            log.error("[aggregate] %s", err)
            for rid in valid_ids:
                id_to_result[rid] = ActionResult(rid, "failed", error=err)
            continue

        task_id, error = _create_todoist_task(payload, clients)
        if error:
            log.error("[aggregate] Todoist create failed: %s", error)
            for rid in valid_ids:
                id_to_result[rid] = ActionResult(rid, "failed", error=f"Todoist error: {error}")
        else:
            action_content = _json.dumps(action, default=str)
            log.info("[aggregate] Created task %s for record_ids=%s", task_id, valid_ids)
            for rid in valid_ids:
                id_to_result[rid] = ActionResult(
                    rid, "created", external_id=task_id, action_content=action_content
                )

    # Mark unactioned current records as skipped
    for rid in current_ids:
        if rid not in id_to_result:
            id_to_result[rid] = ActionResult(rid, "skipped")

    return list(id_to_result.values())


def run_action(profile_path: str | Path) -> list[ActionResult]:
    """Run the full action-dispatch pipeline for the given YAML profile.

    Loads profile, scans DB for pending records, runs per-record prompt + task
    creation, records outcomes in ActionRuns. Returns per-record results.
    """
    path = Path(profile_path)
    if not path.is_absolute():
        path = config.BASE_DIR / path

    profile = _load_profile(path)
    profile_name: str = profile["name"]
    target: str = profile["target"]

    mode: str = profile.get("mode", "per_record")
    log.info(
        "Action dispatch starting: profile=%s target=%s mode=%s",
        profile_name, target, mode,
    )

    try:
        records = _query_pending_records(profile)
    except DbError as exc:
        log.error("Failed to query pending records: %s", exc)
        return []

    log.info("Found %d record(s) for profile %r", len(records), profile_name)
    if not records:
        return []

    llm = LLMClient()
    clients: dict = {}
    client_factory = _TARGET_CLIENTS.get(target)
    if client_factory is not None:
        clients[target] = client_factory()

    if mode == "aggregate":
        results = _run_aggregate(profile, records, llm, clients)
        if not results:
            return []
    else:
        results = []
        for record in records:
            results.append(_action_one(record, profile, llm, clients))

    for result in results:
        try:
            _insert_action_run(
                profile_name=profile_name,
                record_id=result.record_id,
                target=target,
                external_id=result.external_id,
                status=result.status,
                error=result.error,
                action_content=result.action_content,
            )
        except DbError as exc:
            log.error("Could not write ActionRun for record %d: %s", result.record_id, exc)

        if result.status == "failed":
            log.error("Record %d: %s", result.record_id, result.error)
        else:
            log.info(
                "Record %d → %s (task_id=%s)",
                result.record_id, result.status, result.external_id,
            )

    created = sum(1 for r in results if r.status == "created")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    log.info(
        "Action dispatch complete: %d created, %d skipped, %d failed",
        created, skipped, failed,
    )
    return results


if __name__ == "__main__":
    setup_logging("action_dispatch")
    purge_old_logs()

    parser = argparse.ArgumentParser(
        description="ClearFeed action dispatch — post-classification task creation."
    )
    parser.add_argument(
        "profile",
        help="Path to the action profile YAML (e.g. profiles\\task_connection.yaml)",
    )
    args = parser.parse_args()

    run_action(args.profile)
