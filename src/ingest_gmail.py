"""Stage 1: Gmail ingestion for ClearFeed.

Fetches threads not yet marked ProcessedClearFeed. For each message in the thread
(oldest first; Gmail appends repeat alerts to an existing thread):
  1. Extract body + images
  2. Summarize with Haiku, then classify (labels, tags) off the summary
  3. Download and store images
  4. Write ContentRecords + RecordTerms
Then, for the thread:
  5. Apply all bucket labels + ProcessedClearFeed (the latter only if no message failed)
  6. Trash each stored message (message-level; kept if excluded or deferred)

A message that fails before step 4 is not stored and keeps the thread unmarked for
retry. Duplicate source_refs (message ids) are silently skipped (idempotent).
"""

import logging
import re
import sys
import os
from dataclasses import dataclass, field
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
    include_processed: bool = False,
) -> list[IngestResult]:
    """Ingest eligible Gmail threads. Returns per-thread results.

    Args:
        dry_run: if True, classify and write to DB but skip apply_labels and
            trash_thread so the original emails are untouched.
        limit: cap the number of threads processed. Defaults to
            INGEST_DRY_RUN_LIMIT in dry-run mode, unlimited otherwise.
        include_processed: also fetch inbox threads already labeled
            ProcessedClearFeed, to store messages earlier runs labeled but skipped.
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
        threads = gmail.fetch_ingest_threads(include_processed=include_processed)
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


@dataclass
class _MessageOutcome:
    """Result of ingesting one message of a thread."""

    status: str  # stored | duplicate | trashed | failed
    message_id: str = ""
    record_id: int | None = None
    labels: list[str] = field(default_factory=list)
    keep: bool = False  # leave in the inbox: trash-excluded or trash-deferred sender
    error: str | None = None


def _ingest_thread(
    thread_id: str,
    gmail: GmailClient,
    llm: LLMClient,
    label_map: dict[str, str],
    dry_run: bool = False,
) -> IngestResult:
    """Process every message of a Gmail thread through the ingest pipeline.

    Each message becomes its own record (source_ref = message id), so a message
    Gmail appends to an already-ingested thread is picked up too. Messages already
    in the DB are skipped. Stored messages are trashed one by one (message-level, so
    an older kept message in the same thread is untouched) unless they match a
    TRASH_EXCLUSIONS rule or a TRASH_DEFERRED_SENDERS entry.

    The thread gets ProcessedClearFeed only when no message failed, so a failed
    message is retried on the next run. In dry-run mode the DB writes happen but
    nothing in Gmail is trashed or labeled except ProcessedClearFeed.
    """
    try:
        messages = gmail.get_thread_messages(thread_id)
        if not messages:
            return IngestResult(thread_id, "skipped", error="Empty thread")

        outcomes = [_ingest_message(m, gmail, llm, dry_run) for m in messages]
        stored = [o for o in outcomes if o.status == "stored"]
        failed = next((o for o in outcomes if o.status == "failed"), None)
        record_id = stored[0].record_id if stored else None

        if not stored:
            if failed:
                return IngestResult(thread_id, "failed", error=failed.error)
            if all(o.status == "trashed" for o in outcomes):
                return IngestResult(thread_id, "trashed")
            _apply_processed_label(thread_id, gmail, label_map)
            return IngestResult(thread_id, "duplicate", error="Already in DB")

        if dry_run:
            log.info("DRY RUN: %d record(s) written for thread %s", len(stored), thread_id)
            if not failed:
                # Mark processed so this thread is not fetched again on the next run
                _apply_processed_label(thread_id, gmail, label_map)
            return IngestResult(thread_id, "failed" if failed else "dry_run",
                                record_id=record_id, error=failed.error if failed else None)

        # Apply bucket labels to the thread (non-fatal for the DB, which is already written).
        labels = list(dict.fromkeys(lbl for o in stored for lbl in o.labels))
        label_ids = [label_map[lbl] for lbl in labels if lbl in label_map]
        if not failed:
            label_ids.append(label_map[config.PROCESSED_LABEL])
        try:
            gmail.apply_labels(thread_id, label_ids)
        except Exception as exc:
            log.error("Label apply failed for thread %s: %s", thread_id, exc)
            return IngestResult(
                thread_id, "failed", record_id=record_id, error=f"Label apply failed: {exc}"
            )

        # Trash each stored message that is not excluded or deferred.
        for o in stored:
            if o.keep:
                log.info("Message %s kept in inbox (trash exclusion/deferral)", o.message_id)
                continue
            try:
                gmail.trash_message(o.message_id)
            except Exception as exc:
                log.error("Trash failed for message %s (record saved): %s", o.message_id, exc)

        if failed:
            return IngestResult(thread_id, "failed", record_id=record_id, error=failed.error)
        status = "kept" if any(o.keep for o in stored) else "ingested"
        return IngestResult(thread_id, status, record_id=record_id)

    except Exception as exc:
        log.exception("Unexpected error ingesting thread %s", thread_id)
        return IngestResult(thread_id, "failed", error=str(exc))


def _ingest_message(
    msg: dict, gmail: GmailClient, llm: LLMClient, dry_run: bool
) -> _MessageOutcome:
    """Fetch, classify and store one message. Never raises.

    Spam and security/promo messages are trashed (message-level) without a DB write.
    """
    message_id = msg.get("id", "")
    try:
        parts = gmail.extract_message_parts([msg])
        source_ref = message_id

        # Early duplicate check — avoids an LLM call on already-ingested messages
        if record_exists("gmail", source_ref):
            log.info("Skipping duplicate (early check): gmail/%s", source_ref)
            return _MessageOutcome("duplicate", message_id)

        # Build plain body text from HTML if needed
        body_text = parts.get("body_text", "")
        if not body_text and parts.get("html_body"):
            body_text = _html_to_text(parts["html_body"])

        if not body_text:
            return _MessageOutcome("failed", message_id, error="No body text extracted")

        body_text = truncate_body(body_text)

        # Scrape linked article content and append before classification
        if config.ARTICLE_SCRAPE_ENABLED:
            article_text = fetch_email_articles(parts.get("html_body", ""))
            if article_text:
                body_text = truncate_body(
                    f"<email_body>\n{body_text}\n</email_body>\n\n{article_text}"
                )

        # Replace emoji with text descriptions before storage and classification
        body_text = strip_emoji(body_text)

        # Deterministic trash check (security alerts / promos)
        subject = parts.get("subject", "")
        sender = parts.get("sender", "")
        if _should_trash(subject, sender):
            if not dry_run:
                gmail.trash_message(message_id)
            else:
                log.info("DRY RUN: would trash message %s (%s)", message_id, subject)
            return _MessageOutcome("trashed", message_id)

        # LLM summarize, then classify.
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
                gmail.trash_message(message_id)
            return _MessageOutcome("trashed", message_id)

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

        # Download images
        received_at = _parse_date(parts.get("date_str", ""))
        image_paths = _download_images(
            source_ref=source_ref,
            html_body=parts.get("html_body", ""),
            attachments=parts.get("image_attachments", []),
            gmail=gmail,
            message_id=message_id,
        )

        # Write DB record (halt on failure; the caller does not label/trash this message)
        linked_url = _extract_first_url(body_text)
        rec = ContentRecord(
            source_type="gmail",
            source_ref=source_ref,
            received_at=received_at,
            sender=sender,
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
            return _MessageOutcome("duplicate", message_id)
        except DbError as exc:
            return _MessageOutcome("failed", message_id, error=str(exc))

        if dry_run:
            log.info("DRY RUN: record %d written — labels=%s tags=%s", record_id, labels, tags)

        return _MessageOutcome(
            "stored",
            message_id,
            record_id=record_id,
            labels=labels,
            keep=is_trash_excluded(labels, tags) or _is_trash_deferred(sender),
        )

    except Exception as exc:
        log.exception("Unexpected error ingesting message %s", message_id)
        return _MessageOutcome("failed", message_id, error=str(exc))


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


def _is_trash_deferred(sender: str) -> bool:
    """True if the sender is in config.TRASH_DEFERRED_SENDERS (trashed by a later stage)."""
    sender = sender.lower()
    return any(s.lower() in sender for s in config.TRASH_DEFERRED_SENDERS)


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
    parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Also fetch inbox threads already labeled ProcessedClearFeed (one-off backfill "
        "of messages that earlier runs labeled without storing)",
    )
    args = parser.parse_args()

    results = run_ingest(
        dry_run=args.dry_run, limit=args.limit, include_processed=args.include_processed
    )

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
