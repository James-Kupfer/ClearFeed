"""ClearFeed digest orchestrator.

Runs every digest profile listed in orchestration/digest.yaml, in order.
Intended to be called by a scheduled task.

Usage:
    python src/digest_orchestrator.py
    python src/digest_orchestrator.py --dry-run
"""

import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils import purge_old_logs, setup_logging
from dispatch import run_dispatch

log = logging.getLogger(__name__)

_ORCHESTRATION_CONFIG = config.BASE_DIR / "orchestration" / "digest.yaml"


def _load_orchestration_config() -> dict:
    if not _ORCHESTRATION_CONFIG.exists():
        log.warning(
            "Digest orchestration config not found at %s — nothing to run.",
            _ORCHESTRATION_CONFIG,
        )
        return {}
    with _ORCHESTRATION_CONFIG.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def run_all_digests(dry_run: bool = False) -> None:
    """Run every digest profile listed in orchestration/digest.yaml."""
    orch = _load_orchestration_config()
    profiles: list[str] = orch.get("digests") or []

    if not profiles:
        log.info("No digest profiles configured — exiting.")
        return

    log.info("=== Digest run start: %d profile(s) (dry_run=%s) ===", len(profiles), dry_run)

    for profile_path in profiles:
        log.info("--- Running digest: %s ---", profile_path)
        if dry_run:
            log.info("[dry-run] Would dispatch %s", profile_path)
            continue
        try:
            run_dispatch(profile_path)
            log.info("Digest complete: %s", profile_path)
        except Exception:
            log.exception("Digest %s raised an unhandled exception — continuing.", profile_path)

    log.info("=== Digest run complete ===")


if __name__ == "__main__":
    import argparse

    setup_logging("digest_orchestrator")
    purge_old_logs()

    parser = argparse.ArgumentParser(description="ClearFeed digest orchestrator")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log which digests would run without calling the LLM or sending email",
    )
    args = parser.parse_args()

    run_all_digests(dry_run=args.dry_run)
