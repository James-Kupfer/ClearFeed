"""ClearFeed ingest orchestrator.

Runs one full ingest cycle:
  1. Gmail ingest  (ingest_gmail.run_ingest)
  2. Each action profile listed in orchestration/ingest.yaml, in order

Called by scripts/run_ingestion_service.bat in place of ingest_gmail.py directly.

Usage:
    python src/ingest_orchestrator.py
    python src/ingest_orchestrator.py --dry-run
"""

import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils import purge_old_logs, setup_logging
from ingest_gmail import run_ingest
from action_dispatch import run_action
from token_monitor import check_token_expiration

log = logging.getLogger(__name__)

_ORCHESTRATION_CONFIG = config.BASE_DIR / "orchestration" / "ingest.yaml"


def _load_orchestration_config() -> dict:
    if not _ORCHESTRATION_CONFIG.exists():
        log.warning(
            "Orchestration config not found at %s — no post-ingest actions will run.",
            _ORCHESTRATION_CONFIG,
        )
        return {}
    with _ORCHESTRATION_CONFIG.open() as fh:
        return yaml.safe_load(fh) or {}


def run_cycle(dry_run: bool = False) -> None:
    """Run one full ingest + post-ingest-action cycle."""
    orch = _load_orchestration_config()

    # Stage 0 — Token expiration check
    log.info("=== Ingest cycle start (dry_run=%s) ===", dry_run)
    log.info("[run_cycle] Stage 0: Checking Gmail token expiration...")
    try:
        token_info = check_token_expiration()
        if token_info:
            log.info("[run_cycle] Token status: healthy=%s", token_info["healthy"])
            if not token_info["healthy"]:
                log.error(
                    "[run_cycle] ✗ Gmail token is unhealthy: %s", token_info["error"]
                )
                if token_info["warning_sent"]:
                    log.warning(
                        "[run_cycle] Re-auth notification email was sent to %s",
                        token_info["notification_email"],
                    )
    except Exception:
        log.exception("[run_cycle] Token health check failed (non-fatal)")

    # Stage 1 — Gmail ingest
    if orch.get("gmail_ingest", True):
        log.info("[run_cycle] Stage 1: Starting Gmail ingest...")
        try:
            results = run_ingest(dry_run=dry_run)
            log.info(
                "[run_cycle] run_ingest() returned successfully with %d results",
                len(results),
            )
            ingested = sum(1 for r in results if r.status == "ingested")
            skipped = sum(1 for r in results if r.status == "skipped")
            failed = sum(1 for r in results if r.status == "failed")
            duplicate = sum(1 for r in results if r.status == "duplicate")
            trashed = sum(1 for r in results if r.status == "trashed")
            log.info(
                "Ingest complete: ingested=%d skipped=%d failed=%d duplicate=%d trashed=%d",
                ingested,
                skipped,
                failed,
                duplicate,
                trashed,
            )
        except Exception:
            log.exception("[run_cycle] ✗ FATAL: run_ingest() raised exception")
            return
    else:
        log.info(
            "[run_cycle] Stage 1: Gmail ingest disabled in orchestration config — skipping."
        )

    # Stage 2 — post-ingest action profiles
    action_profiles: list[str] = orch.get("post_ingest_actions") or []
    if not action_profiles:
        log.debug("No post_ingest_actions configured.")
        return

    for profile_path in action_profiles:
        log.info("Running action profile: %s", profile_path)
        try:
            action_results = run_action(profile_path)
            created = sum(1 for r in action_results if r.status == "created")
            skipped = sum(1 for r in action_results if r.status == "skipped")
            failed = sum(1 for r in action_results if r.status == "failed")
            log.info(
                "Action profile %s: created=%d skipped=%d failed=%d",
                profile_path,
                created,
                skipped,
                failed,
            )
        except Exception:
            log.exception(
                "Action profile %s raised an unhandled exception.", profile_path
            )

    log.info("=== Ingest cycle complete ===")


if __name__ == "__main__":
    import argparse

    setup_logging("ingest_orchestrator")
    purge_old_logs()

    parser = argparse.ArgumentParser(description="ClearFeed ingest orchestrator")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do not apply labels or trash threads",
    )
    args = parser.parse_args()

    run_cycle(dry_run=args.dry_run)
