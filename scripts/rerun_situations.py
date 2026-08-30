"""One-off helper: rerun task_investment for specific Special Situation records.

Why this exists: task_investment.yaml's dedup clause (ActionRuns NOT EXISTS)
means a plain rerun of `action_dispatch.py profiles\\task_investment.yaml`
will NOT reprocess records that already have an ActionRuns row for this
profile -- including records that were wrongly merged into one omnibus
"Special Situations" task before the v24 synthesis-rule fix. This script
finds those specific records, shows what's already on file for them (so you
can manually close/delete the old combined Todoist task first), then clears
their ActionRuns rows and reruns the profile so they get fresh, per-situation
actions under the corrected rule.

Usage (from the ClearFeed repo root, same venv as clearfeed.bat):
    python scripts\\rerun_situations.py "Ashland" "Mayne Pharma" "Ziff Davis"
        --> dry run: shows matching records + existing ActionRuns, changes nothing

    python scripts\\rerun_situations.py "Ashland" "Mayne Pharma" "Ziff Davis" --apply
        --> deletes those ActionRuns rows, then reruns
            profiles\\task_investment.yaml (via action_dispatch.run_action)

Optional --since-hours N (default 24) widens/narrows the "today" window.
Optional --profile lets you point at a different action profile.
"""

import argparse
import os
import sys

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, _REPO_ROOT)  # security_config.py lives at project root, not src/

import action_dispatch  # noqa: E402
from db import DbError, get_connection, query_sql  # noqa: E402


def find_matching_records(situations: list[str], since_hours: int) -> list[dict]:
    """Business+actionable records from the last `since_hours` hours whose
    subject/summary/executive_summary mention any of the given situations."""
    like_clauses = " OR ".join(
        "(cr.subject ILIKE %s OR cr.summary ILIKE %s OR cr.executive_summary ILIKE %s)"
        for _ in situations
    )
    params: list = []
    for s in situations:
        pat = f"%{s}%"
        params.extend([pat, pat, pat])
    sql = f"""
        SELECT cr.id, cr.sender, cr.subject, cr.received_at
        FROM ContentRecords cr
        WHERE cr.received_at >= (now() AT TIME ZONE 'America/Chicago') - interval '{int(since_hours)} hours'
          AND EXISTS (SELECT 1 FROM RecordTerms rt WHERE rt.id = cr.id
                      AND rt.kind = 'label' AND rt.value = 'Business')
          AND EXISTS (SELECT 1 FROM RecordTerms rt2 WHERE rt2.id = cr.id
                      AND rt2.kind = 'tag' AND rt2.value = 'actionable')
          AND ({like_clauses})
        ORDER BY cr.received_at DESC
    """
    return query_sql(sql, tuple(params))


def find_action_runs(profile_name: str, record_ids: list[int]) -> list[dict]:
    if not record_ids:
        return []
    placeholders = ", ".join(["%s"] * len(record_ids))
    sql = f"""
        SELECT id, record_id, status, external_id, error, action_content, created_at
        FROM ActionRuns
        WHERE profile_name = %s AND record_id IN ({placeholders})
        ORDER BY created_at DESC
    """
    return query_sql(sql, (profile_name, *record_ids))


def delete_action_runs(run_ids: list[int]) -> None:
    if not run_ids:
        return
    placeholders = ", ".join(["%s"] * len(run_ids))
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM ActionRuns WHERE id IN ({placeholders})", tuple(run_ids))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("situations", nargs="+", help="Situation keywords, e.g. Ashland \"Mayne Pharma\" \"Ziff Davis\"")
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--profile", default="profiles/task_investment.yaml")
    parser.add_argument("--apply", action="store_true", help="Actually delete ActionRuns + rerun. Omit for dry run.")
    args = parser.parse_args()

    profile_name = os.path.splitext(os.path.basename(args.profile))[0]

    print(f"Searching last {args.since_hours}h for records matching: {', '.join(args.situations)}")
    try:
        records = find_matching_records(args.situations, args.since_hours)
    except DbError as exc:
        print(f"DB error: {exc}")
        sys.exit(1)

    if not records:
        print("No matching ContentRecords found. Nothing to do.")
        return

    print(f"\nFound {len(records)} matching record(s):")
    for r in records:
        print(f"  id={r['id']:<6} received={r['received_at']}  sender={r['sender']!r}  subject={r['subject']!r}")

    record_ids = [r["id"] for r in records]
    runs = find_action_runs(profile_name, record_ids)

    if runs:
        print(f"\n{len(runs)} existing ActionRuns row(s) for profile={profile_name!r} on these records:")
        for run in runs:
            print(
                f"  run_id={run['id']} record_id={run['record_id']} status={run['status']} "
                f"external_id={run['external_id']!r} created_at={run['created_at']}"
            )
            if run["action_content"]:
                print(f"    action_content: {run['action_content'][:200]}")
        todoist_ids = sorted({r["external_id"] for r in runs if r["external_id"]})
        if todoist_ids:
            print(
                f"\n  >>> These Todoist task id(s) were created from the old run: {', '.join(todoist_ids)}\n"
                f"  >>> This script does NOT touch Todoist -- go close/delete the wrongly-combined "
                f"task in Todoist yourself before (or after) re-running."
            )
    else:
        print(f"\nNo existing ActionRuns for profile={profile_name!r} on these records -- they're already pending.")

    if not args.apply:
        print("\nDry run only -- no changes made. Re-run with --apply to clear ActionRuns and re-dispatch.")
        return

    run_ids = [run["id"] for run in runs]
    if run_ids:
        print(f"\nDeleting {len(run_ids)} ActionRuns row(s) so these records are eligible again...")
        try:
            delete_action_runs(run_ids)
        except DbError as exc:
            print(f"DB error deleting ActionRuns: {exc}")
            sys.exit(1)

    print(f"\nRunning action_dispatch for {args.profile} ...")
    results = action_dispatch.run_action(args.profile)
    if not results:
        print("No results returned (no pending records matched the profile's trigger SQL, or LLM call failed -- check logs).")
        return

    for res in results:
        print(f"  record_id={res.record_id} status={res.status} external_id={res.external_id} error={res.error}")


if __name__ == "__main__":
    main()
