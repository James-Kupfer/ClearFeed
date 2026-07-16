"""Selective re-run of the summarize step on existing ContentRecords.

Re-runs only the Haiku summarize call against each record's stored body_text,
overwrites the summary and its confidence/rationale in the DB, and bumps
processed_at. Labels/tags and the classify-step fields are left untouched — this
never re-classifies. Gmail labels on the original trashed threads are NOT updated
— the DB is the source of truth for dispatch.

To refresh classifications instead, use reprocess.py (re-runs only classify
against the stored summary).

Usage:
    clearfeed.bat reprocess-summary --label Investment
    clearfeed.bat reprocess-summary --tag fed
    clearfeed.bat reprocess-summary --days 7
    clearfeed.bat reprocess-summary --since 2026-05-01
    clearfeed.bat reprocess-summary --label Investment --days 30
    clearfeed.bat reprocess-summary --all
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: F401 — side effect: sets up paths for downstream imports
from db import DbError, query_records_for_reprocess, update_record_summary
from llm_client import LLMClient
from utils import is_trash_excluded, purge_old_logs, setup_logging, summarize_content

log = logging.getLogger(__name__)


@dataclass
class ReprocessResult:
    """Outcome of a single record re-summarize attempt."""

    record_id: int
    status: str  # updated | skipped | failed
    error: str | None = None


def run_reprocess_summary(
    filter_labels: list[str] | None,
    filter_tags: list[str] | None,
    since: datetime | None,
    days: int | None,
    gaps: bool = False,
) -> list[ReprocessResult]:
    """Re-summarize records matching the given filters. Returns per-record results.

    When gaps=True, only records missing a summarize-step output (summary,
    executive_summary, summary_confidence, or summary_rationale) are processed —
    used to fill in what an aborted run left behind.
    """
    llm = LLMClient()

    try:
        records = query_records_for_reprocess(
            filter_labels=filter_labels,
            filter_tags=filter_tags,
            since=since,
            days=days,
            gaps_for="summarize" if gaps else None,
        )
    except DbError as exc:
        log.error("Failed to query records: %s", exc)
        return []

    log.info("Found %d record(s) to re-summarize%s", len(records), " (gap-fill)" if gaps else "")
    results: list[ReprocessResult] = []

    for rec in records:
        if is_trash_excluded(rec.get("labels", []), rec.get("tags", [])):
            log.info("Record %d skipped (matches trash exclusion rule)", rec["id"])
            results.append(ReprocessResult(rec["id"], "skipped", error="Excluded"))
            continue
        result = _resummarize_one(rec, llm)
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


def _resummarize_one(rec: dict, llm: LLMClient) -> ReprocessResult:
    """Re-run only the summarize step on a single record using its stored body_text.

    Labels/tags and the classify-step fields are left untouched. Records with no
    body_text are skipped.
    """
    record_id = rec["id"]
    context = f"record_id={record_id}"

    if not rec.get("body_text"):
        return ReprocessResult(record_id, "skipped", error="No body_text")

    log.info("[reprocess-summary] record_id=%d starting", record_id)

    try:
        system_prompt = (config.PROMPTS_DIR / "summarize.md").read_text(encoding="utf-8")
        user_prompt = (
            f"Subject: {rec['subject']}\nFrom: {rec['sender']}\n\n{rec['body_text']}"
        )
        summary, executive_summary, summary_confidence, summary_rationale = summarize_content(
            llm, system_prompt, user_prompt, context
        )
    except Exception as exc:
        return ReprocessResult(record_id, "failed", error=f"LLM error: {exc}")

    log.info(
        "[reprocess-summary] record_id=%d → summary_conf=%s len(summary)=%d has_exec_summary=%s",
        record_id,
        summary_confidence,
        len(summary),
        executive_summary is not None,
    )

    try:
        update_record_summary(
            record_id, summary, executive_summary, summary_confidence, summary_rationale
        )
    except DbError as exc:
        return ReprocessResult(record_id, "failed", error=f"DB error: {exc}")

    return ReprocessResult(record_id, "updated")


def main() -> None:
    """Entry point: parse args, run re-summarize, log summary."""
    setup_logging("reprocess_summary")
    purge_old_logs()

    parser = argparse.ArgumentParser(
        description="Re-run the summarize step on ClearFeed records against stored body_text."
    )
    parser.add_argument("--label", help="Filter: records carrying this label (e.g. Investment)")
    parser.add_argument("--tag", help="Filter: records carrying this tag (e.g. fed)")
    parser.add_argument("--days", type=int, help="Filter: records received in the last N days")
    parser.add_argument("--since", help="Filter: records received after YYYY-MM-DD")
    parser.add_argument(
        "--all", action="store_true", dest="all_records", help="Re-summarize all records"
    )
    parser.add_argument(
        "--gaps",
        action="store_true",
        help="Gap-fill: only records missing a summarize output (summary, "
        "executive_summary, summary_confidence/rationale). Combine with other filters to scope.",
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

    results = run_reprocess_summary(
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
        "Reprocess-summary complete: %d updated, %d skipped, %d failed", updated, skipped, failed
    )


if __name__ == "__main__":
    main()
