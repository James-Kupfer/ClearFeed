"""Selective re-run of the classify step on existing ContentRecords.

Re-runs only the Haiku classify call against each record's stored summary,
overwrites labels/tags and the classification confidence/rationale fields in the
DB, and bumps processed_at. The stored summary (and its confidence/rationale) is
left untouched — reprocess never re-summarizes. Gmail labels on the original
trashed threads are NOT updated — the DB is the source of truth for dispatch.

Usage:
    clearfeed.bat reprocess --label Investment
    clearfeed.bat reprocess --tag fed
    clearfeed.bat reprocess --days 7
    clearfeed.bat reprocess --since 2026-05-01
    clearfeed.bat reprocess --label Investment --days 30
    clearfeed.bat reprocess --all
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: F401 — side effect: sets up paths for downstream imports
from db import DbError, query_records_for_reprocess, update_record_classification
from llm_client import LLMClient
from utils import classify_with_escalation, is_trash_excluded, normalize_tag, purge_old_logs, resolve_labels, setup_logging

log = logging.getLogger(__name__)


@dataclass
class ReprocessResult:
    """Outcome of a single record reprocess attempt."""

    record_id: int
    status: str  # updated | skipped | failed
    error: str | None = None


def run_reprocess(
    filter_labels: list[str] | None,
    filter_tags: list[str] | None,
    since: datetime | None,
    days: int | None,
    gaps: bool = False,
) -> list[ReprocessResult]:
    """Reprocess records matching the given filters. Returns per-record results.

    When gaps=True, only records missing a classify-step output (labels, tags,
    classification_confidence, or classification_rationale) are processed — used
    to fill in what an aborted run left behind.
    """
    llm = LLMClient()

    try:
        records = query_records_for_reprocess(
            filter_labels=filter_labels,
            filter_tags=filter_tags,
            since=since,
            days=days,
            gaps_for="classify" if gaps else None,
        )
    except DbError as exc:
        log.error("Failed to query records: %s", exc)
        return []

    log.info("Found %d record(s) to reprocess%s", len(records), " (gap-fill)" if gaps else "")
    results: list[ReprocessResult] = []

    for rec in records:
        if is_trash_excluded(rec.get("labels", []), rec.get("tags", [])):
            log.info("Record %d skipped (matches trash exclusion rule)", rec["id"])
            results.append(ReprocessResult(rec["id"], "skipped", error="Excluded"))
            continue
        result = _reprocess_one(rec, llm)
        results.append(result)
        if result.status == "failed":
            log.error("Record %d: %s", result.record_id, result.error)
        else:
            log.info(
                "Record %d (%s): %s",
                result.record_id,
                rec.get("subject", "")[:60],
                result.status,
            )

    return results


# Fields fed to the classify prompt — a record is only reclassified when every
# one carries data. body_text is included as a sanity guard (a record with no
# body is malformed even though the classify step works off the summary).
_REQUIRED_INPUT_FIELDS = ("body_text", "subject", "sender", "summary")


def _reprocess_one(rec: dict, llm: LLMClient) -> ReprocessResult:
    """Re-run only the classify step on a single record using its stored summary.

    The summary is reused as-is (never re-generated). Calls the LLM directly — no
    ingest-specific shortcuts (InMail override) apply here. A record is skipped
    unless it has data in every classify input field (see _REQUIRED_INPUT_FIELDS).
    """
    record_id = rec["id"]
    context = f"record_id={record_id}"

    missing = [f for f in _REQUIRED_INPUT_FIELDS if not str(rec.get(f) or "").strip()]
    if missing:
        return ReprocessResult(
            record_id, "skipped", error=f"Missing input field(s): {', '.join(missing)}"
        )

    log.info(
        "[reprocess] record_id=%d starting — existing labels=%s",
        record_id,
        rec.get("labels", []),
    )

    try:
        classify_system = (config.PROMPTS_DIR / "classify.md").read_text(encoding="utf-8")
        classify_user = (
            f"Subject: {rec['subject']}\nFrom: {rec['sender']}\n\nSummary:\n{rec['summary']}"
        )
        classify_result, classification_confidence, escalated, classify_model, classify_tokens = (
            classify_with_escalation(llm, classify_system, classify_user, context)
        )
    except Exception as exc:
        return ReprocessResult(record_id, "failed", error=f"LLM error: {exc}")

    labels = resolve_labels(classify_result.get("labels", []))
    tags = [normalize_tag(t) for t in classify_result.get("tags", [])]
    classification_rationale = (classify_result.get("classification_rationale") or "")[:1000] or None

    log.info(
        "[reprocess] record_id=%d → stored labels=%s class_conf=%s escalated=%s model=%s tokens=%s",
        record_id,
        labels,
        classification_confidence,
        escalated,
        classify_model,
        classify_tokens,
    )

    try:
        update_record_classification(
            record_id, labels, tags,
            classification_confidence, classification_rationale,
            classify_model, classify_tokens, escalated,
        )
    except DbError as exc:
        return ReprocessResult(record_id, "failed", error=f"DB error: {exc}")

    return ReprocessResult(record_id, "updated")


def main() -> None:
    """Entry point: parse args, run reprocess, log summary."""
    setup_logging("reprocess")
    purge_old_logs()

    parser = argparse.ArgumentParser(
        description="Reprocess ClearFeed records — re-runs classify against stored body_text."
    )
    parser.add_argument("--label", help="Filter: records carrying this label (e.g. Investment)")
    parser.add_argument("--tag", help="Filter: records carrying this tag (e.g. fed)")
    parser.add_argument("--days", type=int, help="Filter: records received in the last N days")
    parser.add_argument("--since", help="Filter: records received after YYYY-MM-DD")
    parser.add_argument(
        "--all", action="store_true", dest="all_records", help="Reprocess all records"
    )
    parser.add_argument(
        "--gaps",
        action="store_true",
        help="Gap-fill: only records missing a classify output (labels, tags, "
        "classification_confidence/rationale). Combine with other filters to scope.",
    )
    args = parser.parse_args()

    if not any([args.label, args.tag, args.days, args.since, args.all_records, args.gaps]):
        parser.error("Specify at least one filter (--label, --tag, --days, --since), --all, or --gaps")

    since_dt: datetime | None = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error(f"Invalid --since date {args.since!r} — expected YYYY-MM-DD")

    results = run_reprocess(
        filter_labels=[args.label] if args.label else None,
        filter_tags=[args.tag] if args.tag else None,
        since=since_dt,
        days=args.days,
        gaps=args.gaps,
    )

    updated = sum(1 for r in results if r.status == "updated")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    log.info(
        "Reprocess complete: %d updated, %d skipped, %d failed", updated, skipped, failed
    )


if __name__ == "__main__":
    main()
