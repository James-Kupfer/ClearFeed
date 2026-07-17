"""Stage 1: Gmail ingestion for ClearFeed.

Fetches threads that have been labeled (by a prior run or Cowork) but not yet
marked ProcessedClearFeed. For each thread:
  1. Extract body + images
  2. Summarize with Haiku, then classify (labels, tags) off the summary
  3. Download and store images
  4. Write ContentRecords + RecordTerms
  5. Apply all bucket labels + ProcessedClearFeed to the Gmail thread
  6. Trash the thread (final, irreversible step)

Failure at any step before step 5 leaves the thread untouched for retry.
Duplicate source_refs are silently skipped (idempotent).
"""

import logging
import re
import sys
import os
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image as PILImage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from db import ContentRecord, DuplicateRecordError, DbError, insert_content_record, record_exists
from gmail_client import GmailClient
from llm_client import LLMClient
from article_scraper import fetch_email_articles
from utils import (
    _CST,
    classify_with_escalation,
    is_trash_excluded,
    normalize_tag,
    purge_old_logs,
    resolve_labels,
    setup_logging,
    source_ref_hash,
    strip_emoji,
    summarize_content,
    truncate_body,
)

log = logging.getLogger(__name__)

# Patterns for deterministic TRASH classification (checked before LLM call)
_TRASH_SUBJECT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"security alert",
        r"sign-in attempt",
        r"suspicious activity",
        r"unusual sign-in",
        r"account alert",
        r"verify your identity",
        r"% off",
        r"\bsale\b",
        r"\bdeal\b",
        r"limited time",
        r"special offer",
        r"\bdiscount\b",
        r"\bpromo\b",
        r"\bcoupon\b",
        r"shop now",
        r"buy now",
        r"free shipping",
    ]
]


@dataclass
class IngestResult:
    """Summary of a single-thread ingest attempt."""

    thread_id: str
    status: str  # skipped | duplicate | ingested | trashed | failed
    record_id: int | None = None
    error: str | None = None


def run_ingest(
    dry_run: bool = False,
    limit: int | None = None,
) -> list[IngestResult]:
    """Ingest eligible Gmail threads. Returns per-thread results.

    Args:
        dry_run: if True, classify and write to DB but skip apply_labels and
            trash_thread so the original emails are untouched.
        limit: cap the number of threads processed. Defaults to
            INGEST_DRY_RUN_LIMIT in dry-run mode, unlimited otherwise.
    """
    log.info("[run_ingest] === Starting run_ingest (dry_run=%s) ===", dry_run)

    log.info("[run_ingest] Step 1: Initializing Gmail client...")
    try:
        gmail = GmailClient()
        log.info("[run_ingest] ✓ Gmail client initialized")
    except Exception as exc:
        log.exception("[run_ingest] ✗ FAILED to initialize Gmail client")
        raise

    log.info("[run_ingest] Step 2: Initializing LLM client...")
    try:
        llm = LLMClient()
        log.info("[run_ingest] ✓ LLM client initialized")
    except Exception as exc:
        log.exception("[run_ingest] ✗ FAILED to initialize LLM client")
        raise

    log.info("[run_ingest] Step 3: Setting up labels...")
    all_labels = config.BUCKET_LABELS + [config.PROCESSED_LABEL]
    log.info("[run_ingest] Labels to ensure: %s", all_labels)
    try:
        label_map = gmail.ensure_labels(all_labels)
        log.info("[run_ingest] ✓ Labels setup complete. Map keys: %s", list(label_map.keys()))
    except Exception as exc:
        log.exception("[run_ingest] ✗ FAILED to setup labels")
        raise

    log.info("[run_ingest] Step 4: Fetching eligible threads...")
    try:
        threads = gmail.fetch_ingest_threads()
        log.info("[run_ingest] ✓ Thread fetch complete. Found %d threads", len(threads))
    except Exception as exc:
        log.exception("[run_ingest] ✗ FAILED to fetch threads")
        raise

    effective_limit = limit if limit is not None else (config.INGEST_DRY_RUN_LIMIT if dry_run else None)
    if effective_limit is not None:
        threads = threads[:effective_limit]
        log.info("[run_ingest] Applied limit: %d threads", len(threads))

    mode = f"DRY RUN (limit={effective_limit})" if dry_run else "normal"
    log.info("[run_ingest] Step 5: Processing %d thread(s) [mode: %s]", len(threads), mode)

    results = []
    for thread in threads:
        result = _ingest_thread(thread["id"], gmail, llm, label_map, dry_run=dry_run)
        results.append(result)
        if result.status == "failed":
            log.error("Thread %s: %s", thread["id"], result.error)
        else:
            log.info(
                "Thread %s: %s (record_id=%s)",
                thread["id"],
                result.status,
                result.record_id,
            )

    return results


def _ingest_thread(
    thread_id: str,
    gmail: GmailClient,
    llm: LLMClient,
    label_map: dict[str, str],
    dry_run: bool = False,
) -> IngestResult:
    """Process a single Gmail thread through the full ingest pipeline.

    In dry-run mode, steps 1-6 (fetch, classify, DB write) run normally.
    Steps 7-8 (apply_labels, trash_thread) are skipped so the email is
    left untouched in Gmail and remains eligible for a future real run.
    """
    try:
        # Step 1: fetch thread messages
        messages = gmail.get_thread_messages(thread_id)
        if not messages:
            return IngestResult(thread_id, "skipped", error="Empty thread")

        parts = gmail.extract_message_parts(messages)
        source_ref = messages[0]["id"]  # use first message ID as stable identifier

        # Step 2a: early duplicate check — avoids LLM call on already-ingested threads
        if record_exists("gmail", source_ref):
            log.info("Skipping duplicate (early check): gmail/%s", source_ref)
            _apply_processed_label(thread_id, gmail, label_map)
            return IngestResult(thread_id, "duplicate", error="Already in DB")

        # Step 2b: build plain body text from HTML if needed
        body_text = parts.get("body_text", "")
        if not body_text and parts.get("html_body"):
            body_text = _html_to_text(parts["html_body"])

        if not body_text:
            return IngestResult(thread_id, "failed", error="No body text extracted")

        body_text = truncate_body(body_text)

        # Step 2c: scrape linked article content and append before classification
        if config.ARTICLE_SCRAPE_ENABLED:
            article_text = fetch_email_articles(parts.get("html_body", ""))
            if article_text:
                body_text = truncate_body(
                    f"<email_body>\n{body_text}\n</email_body>\n\n{article_text}"
                )

        # Step 2d: replace emoji with text descriptions before storage and classification
        body_text = strip_emoji(body_text)

        # Step 3: deterministic trash check (security alerts / promos)
        subject = parts.get("subject", "")
        if _should_trash(subject, parts.get("sender", "")):
            if not dry_run:
                gmail.trash_thread(thread_id)
            else:
                log.info("DRY RUN: would trash thread %s (%s)", thread_id, subject)
            return IngestResult(thread_id, "trashed")

        # Step 4: LLM summarize, then classify.
        sender = parts.get("sender", "")
        (
            summary,
            executive_summary,
            summary_confidence,
            summary_rationale,
            classify_result,
            classification_confidence,
            escalated,
            classify_model,
            classify_tokens,
        ) = _summarize_and_classify(llm, subject, sender, body_text, source_ref)

        # Spam: trash immediately — no DB write, no Gmail label (Spam is a reserved Gmail name)
        if any(lbl.lower() == "spam" for lbl in classify_result.get("labels", [])):
            log.info("[ingest] %s → LLM Spam classification → trashing without storage", source_ref)
            if not dry_run:
                gmail.trash_thread(thread_id)
            return IngestResult(thread_id, "trashed")

        labels = resolve_labels(classify_result.get("labels", []))
        tags = [normalize_tag(t) for t in classify_result.get("tags", [])]
        classification_rationale = (classify_result.get("classification_rationale") or "")[:1000] or None

        log.info(
            "[ingest] %s → stored labels=%s summary_conf=%s class_conf=%s escalated=%s model=%s tokens=%s",
            source_ref,
            labels,
            summary_confidence,
            classification_confidence,
            escalated,
            classify_model,
            classify_tokens,
        )

        # Step 5: download images
        received_at = _parse_date(parts.get("date_str", ""))
        image_paths = _download_images(
            source_ref=source_ref,
            html_body=parts.get("html_body", ""),
            attachments=parts.get("image_attachments", []),
            gmail=gmail,
            message_id=messages[0]["id"],
        )

        # Step 6: write DB record (halt on failure; do NOT label/trash if this fails)
        linked_url = _extract_first_url(body_text)
        rec = ContentRecord(
            source_type="gmail",
            source_ref=source_ref,
            received_at=received_at,
            sender=parts.get("sender", ""),
            subject=subject,
            body_text=body_text,
            linked_article_url=linked_url,
            image_local_paths=image_paths,
            summary=summary,
            executive_summary=executive_summary,
            summary_confidence=summary_confidence,
            summary_rationale=summary_rationale,
            labels=labels,
            tags=tags,
            classification_confidence=classification_confidence,
            classification_rationale=classification_rationale,
            classify_model=classify_model,
            classify_tokens=classify_tokens,
            escalated=escalated,
            # Full original source, untruncated — captured before html2text strip,
            # article-scrape append, and truncation mutated body_text. Stored in
            # SourceDocuments (see db.insert_content_record).
            raw_html=parts.get("html_body", "") or None,
            raw_text=parts.get("body_text", "") or None,
        )

        try:
            record_id = insert_content_record(rec)
        except DuplicateRecordError:
            # Race condition: another run inserted between our early check and now
            log.info("Skipping duplicate (late check): gmail/%s", source_ref)
            _apply_processed_label(thread_id, gmail, label_map)
            return IngestResult(thread_id, "duplicate", error="Already in DB")
        except DbError as exc:
            return IngestResult(thread_id, "failed", error=str(exc))

        if dry_run:
            log.info(
                "DRY RUN: record %d written — labels=%s tags=%s",
                record_id, labels, tags,
            )
            # Apply ProcessedClearFeed so this thread is not fetched again on the next run
            _apply_processed_label(thread_id, gmail, label_map)
            return IngestResult(thread_id, "dry_run", record_id=record_id)

        # Step 7: apply labels to Gmail thread (non-fatal if this fails)
        label_ids = [label_map[lbl] for lbl in labels if lbl in label_map]
        label_ids.append(label_map[config.PROCESSED_LABEL])
        try:
            gmail.apply_labels(thread_id, label_ids)
        except Exception as exc:
            log.error("Label apply failed for thread %s: %s", thread_id, exc)
            return IngestResult(
                thread_id,
                "failed",
                record_id=record_id,
                error=f"Label apply failed: {exc}",
            )

        # Step 8: move to Trash — skip if record matches a TRASH_EXCLUSIONS rule
        if is_trash_excluded(labels, tags):
            log.info(
                "Thread %s kept in inbox (exclusion match: labels=%s tags=%s)",
                thread_id, labels, tags,
            )
            return IngestResult(thread_id, "kept", record_id=record_id)

        try:
            gmail.trash_thread(thread_id)
        except Exception as exc:
            log.error("Trash failed for thread %s (record saved): %s", thread_id, exc)

        return IngestResult(thread_id, "ingested", record_id=record_id)

    except Exception as exc:
        log.exception("Unexpected error ingesting thread %s", thread_id)
        return IngestResult(thread_id, "failed", error=str(exc))


def _apply_processed_label(
    thread_id: str, gmail: GmailClient, label_map: dict[str, str]
) -> None:
    """Apply only the ProcessedClearFeed label to a thread. Non-fatal."""
    label_id = label_map.get(config.PROCESSED_LABEL)
    if not label_id:
        return
    try:
        gmail.apply_labels(thread_id, [label_id])
    except Exception as exc:
        log.warning("Could not apply %s to %s: %s", config.PROCESSED_LABEL, thread_id, exc)


def _summarize_and_classify(
    llm: LLMClient, subject: str, sender: str, body_text: str, source_ref: str
) -> tuple[str, str | None, int | None, str | None, dict, int | None, bool, str | None, int | None]:
    """Run the two-step pipeline: summarize body_text, then classify off the summary.

    Returns (summary, executive_summary, summary_confidence, summary_rationale,
    classify_result, classification_confidence, escalated, classify_model,
    classify_tokens).

    InMail is handled deterministically (no LLM, no usage data): the subject is
    used as the summary and the classification is fixed to Professional.
    """
    if "inmail" in subject.lower():
        log.info("[classify] %s → deterministic InMail short-circuit: labels=['Professional']", source_ref)
        return (
            subject,  # summary
            None,     # executive_summary
            None,     # summary_confidence
            None,     # summary_rationale
            {"labels": ["Professional"], "tags": ["linkedin", "inmail"]},
            None,     # classification_confidence
            False,    # escalated
            None,     # classify_model
            None,     # classify_tokens
        )

    summary, executive_summary, summary_confidence, summary_rationale = _summarize(
        llm, subject, sender, body_text, source_ref
    )
    classify_result, classification_confidence, escalated, classify_model, classify_tokens = _classify(
        llm, subject, sender, summary, source_ref
    )
    return (
        summary,
        executive_summary,
        summary_confidence,
        summary_rationale,
        classify_result,
        classification_confidence,
        escalated,
        classify_model,
        classify_tokens,
    )


def _summarize(
    llm: LLMClient, subject: str, sender: str, body_text: str, source_ref: str
) -> tuple[str, str | None, int | None, str | None]:
    """Summarize the full body_text.

    Returns (summary, executive_summary, summary_confidence, summary_rationale).
    """
    system_prompt = (config.PROMPTS_DIR / "summarize.md").read_text(encoding="utf-8")
    user_prompt = f"Subject: {subject}\nFrom: {sender}\n\n{body_text}"
    return summarize_content(llm, system_prompt, user_prompt, source_ref)


def _classify(
    llm: LLMClient, subject: str, sender: str, summary: str, source_ref: str
) -> tuple[dict, int | None, bool, str | None, int | None]:
    """Classify off the produced summary (with subject/sender for purpose signals).

    Returns (classify_result, classification_confidence, escalated, classify_model,
    classify_tokens). Runs through classify_with_escalation (Haiku, escalating to
    Sonnet on low confidence or a Miscellaneous label).
    """
    system_prompt = (config.PROMPTS_DIR / "classify.md").read_text(encoding="utf-8")
    user_prompt = f"Subject: {subject}\nFrom: {sender}\n\nSummary:\n{summary}"
    return classify_with_escalation(llm, system_prompt, user_prompt, source_ref)


def _should_trash(subject: str, sender: str) -> bool:
    """Return True if this email matches known security-alert or promo patterns."""
    text = f"{subject} {sender}"
    return any(pat.search(text) for pat in _TRASH_SUBJECT_PATTERNS)


def _parse_date(date_str: str) -> datetime | None:
    """Parse an RFC 2822 email date string and return as naive CST datetime."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).astimezone(_CST).replace(tzinfo=None)
    except Exception:
        return None


def _extract_first_url(text: str) -> str | None:
    """Return the first http/https URL found in body text, or None."""
    match = re.search(r"https?://[^\s\"'<>]+", text)
    return match.group(0) if match else None


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text using html2text."""
    try:
        import html2text as h2t

        h = h2t.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        return h.handle(html)
    except ImportError:
        import re as _re

        return _re.sub(r"<[^>]+>", " ", html)


def _download_images(
    source_ref: str,
    html_body: str,
    attachments: list[dict],
    gmail: GmailClient,
    message_id: str,
) -> list[str]:
    """Download and store images for a record. Non-fatal; returns stored paths.

    The image directory is only created if at least one image passes the filter.
    """
    img_dir = config.IMAGE_DIR / datetime.now().strftime("%Y-%m") / source_ref_hash(source_ref)
    paths: list[str] = []

    def _ensure_dir() -> None:
        img_dir.mkdir(parents=True, exist_ok=True)

    # Inline images from HTML <img src="...">
    for url in _extract_img_urls(html_body):
        _ensure_dir()
        path = _download_one_image(url, img_dir)
        if path:
            paths.append(str(path))

    # MIME-attached images
    for att in attachments:
        if not att.get("attachment_id"):
            continue
        try:
            data = gmail.get_attachment_data(message_id, att["attachment_id"])
            _ensure_dir()
            # Derive extension from PIL format so the file is always openable
            ext = _ext_from_bytes(data) or (
                att["filename"].rsplit(".", 1)[-1] if "." in att["filename"] else "jpg"
            )
            filename = f"att_{att['attachment_id'][:8]}.{ext}"
            dest = img_dir / filename
            dest.write_bytes(data)
            if _passes_image_filter(dest):
                paths.append(str(dest))
            else:
                dest.unlink()
        except Exception as exc:
            log.debug("Attachment download failed: %s", exc)

    return paths


def _extract_img_urls(html: str) -> list[str]:
    """Extract unique image URLs from HTML img tags."""
    seen: set[str] = set()
    urls = []
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = match.group(1)
        if url.startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _download_one_image(url: str, dest_dir: Path) -> Path | None:
    """Download a single image URL to dest_dir. Returns path or None if filtered."""
    domain = urlparse(url).netloc.lower()
    if any(td in domain for td in config.IMAGE_TRACKER_DOMAINS):
        return None
    try:
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        data = resp.content
        # Use PIL-detected format for the extension so the file is always openable,
        # falling back to the URL path segment if PIL can't identify the format.
        ext = _ext_from_bytes(data)
        if ext:
            url_stem = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "image"
            filename = f"{url_stem[:56]}.{ext}"
        else:
            filename = urlparse(url).path.rsplit("/", 1)[-1] or "image.jpg"
        dest = dest_dir / filename[:64]
        dest.write_bytes(data)
        if _passes_image_filter(dest):
            return dest
        dest.unlink()
        return None
    except Exception as exc:
        log.debug("Image download failed (%s): %s", url, exc)
        return None


def _ext_from_bytes(data: bytes) -> str | None:
    """Detect image format from raw bytes using PIL. Returns lowercase extension or None."""
    try:
        import io
        with PILImage.open(io.BytesIO(data)) as img:
            fmt = img.format  # e.g. 'JPEG', 'PNG', 'WEBP', 'GIF'
        return fmt.lower() if fmt else None
    except Exception:
        return None


def _passes_image_filter(path: Path) -> bool:
    """Return True if the image meets minimum size requirements."""
    try:
        kb = path.stat().st_size / 1024
        if kb < config.IMAGE_MIN_KB:
            return False
        with PILImage.open(path) as img:
            w, h = img.size
        return w >= config.IMAGE_MIN_WIDTH and h >= config.IMAGE_MIN_HEIGHT
    except Exception:
        return False


if __name__ == "__main__":
    import argparse

    setup_logging("ingest_gmail")
    purge_old_logs()

    parser = argparse.ArgumentParser(description="ClearFeed Gmail ingest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and store records without labeling or trashing emails",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Max threads to process (dry-run default: {config.INGEST_DRY_RUN_LIMIT})",
    )
    args = parser.parse_args()

    results = run_ingest(dry_run=args.dry_run, limit=args.limit)

    ingested = sum(1 for r in results if r.status == "ingested")
    kept = sum(1 for r in results if r.status == "kept")
    dry_run_count = sum(1 for r in results if r.status == "dry_run")
    trashed = sum(1 for r in results if r.status == "trashed")
    failed = sum(1 for r in results if r.status == "failed")

    if args.dry_run:
        log.info(
            "DRY RUN complete: %d stored, %d would-trash, %d failed — no emails modified",
            dry_run_count, trashed, failed,
        )
    else:
        log.info(
            "Ingest complete: %d ingested, %d kept, %d trashed, %d failed",
            ingested, kept, trashed, failed,
        )
