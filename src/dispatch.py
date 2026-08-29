"""Stage 3: Digest dispatch for ClearFeed.

Usage:
    python -m dispatch profiles/investment_digest.yaml

Loads the named profile, resolves each inputs[] band from the DB, assembles
context, calls the LLM to generate the digest, renders HTML, sends via SMTP,
and writes a DigestRuns record.

Profile schema (YAML, self-contained — prompt inlined as the last key):
    kind: digest                  # discriminator (validated; optional)
    name: investment_digest
    model: sonnet                 # optional; overrides LLM_ROUTING["synthesis"|"digest"]
    recipient: summary@kupfer.me  # optional; falls back to DEFAULT_RECIPIENT
    output: [email]               # optional; default [email]
    inputs:
      - section: new_this_week
        source: sql
        format: json              # json | markdown (default markdown)
        sql: |
          SELECT cr.id, cr.subject, cr.sender, cr.received_at, cr.summary,
                 cr.executive_summary, cr.linked_article_url,
                 (SELECT STRING_AGG(rt.value, ', ') FROM RecordTerms rt
                   WHERE rt.id = cr.id AND rt.kind = 'tag') AS tags
          FROM ContentRecords cr
          WHERE cr.enrichment_status = 'complete'
            AND cr.received_at >= (now() AT TIME ZONE 'America/Chicago') - interval '168 hours'
            AND EXISTS (SELECT 1 FROM RecordTerms rt WHERE rt.id = cr.id
                        AND rt.kind = 'label' AND rt.value = 'Investment')
          ORDER BY cr.received_at DESC LIMIT 200
      - section: prior_briefs
        source: digests
        from_profile: investment_daily
        window_hours: 168         # optional: only digests within this lookback
        limit: 7
    prompt: |                     # LAST key — the system prompt for this digest
      ...

SQL conventions: use alias 'cr' for ContentRecords; include 'cr.id AS id' and
'cr.received_at' for DigestRuns lineage and period computation; tag literals
are stored lowercase (e.g. 'actionable'), label literals as stored in the DB
(e.g. 'Business', 'Technology').

format: json emits the whole band as a fenced JSON blob.
format: markdown (default) renders one markdown bullet per record.
"""

import io
import logging
import re
import sys
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from db import (
    DbError,
    has_digest_run_since,
    insert_digest_run,
    query_digests_for_band,
    query_sql,
)
from gmail_client import send_email
from llm_client import LLMClient
from utils import (
    now_cst,
    normalize_tag,
    purge_old_logs,
    read_yaml_profile,
    setup_logging,
)

log = logging.getLogger(__name__)


@dataclass
class BandResult:
    """Resolved content for a single inputs[] band."""

    section: str
    records: list[dict] = field(default_factory=list)
    tag_freq: dict[str, int] = field(default_factory=dict)
    emphasis_tags: list[str] = field(default_factory=list)
    format: str = "markdown"  # "json" | "markdown"


@dataclass
class DigestOutput:
    """Structured output from the LLM before rendering."""

    html: str
    content_ids: list[int]
    prior_digest_ids: list[int]
    period_start: datetime
    period_end: datetime
    record_count: int


def _last_schedule_anchor(schedule: str) -> "datetime | None":
    """Return the most recent scheduled fire time for the given schedule string.

    Supported values:
      weekly_saturday  — most recent Saturday at 05:00 CST
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    cst = ZoneInfo("America/Chicago")
    now = datetime.now(cst).replace(tzinfo=None)

    if schedule == "weekly_saturday":
        days_since_saturday = (now.weekday() - 5) % 7  # Saturday == 5
        last_sat = (now - timedelta(days=days_since_saturday)).replace(
            hour=5, minute=0, second=0, microsecond=0
        )
        # If we haven't yet passed 05:00 on a Saturday, step back one week
        if last_sat > now:
            last_sat -= timedelta(weeks=1)
        return last_sat

    log.warning("Unknown dedup_schedule value %r — skipping dedup check.", schedule)
    return None


def run_dispatch(profile_path: str | Path) -> None:
    """Run the full dispatch pipeline for the given profile.

    Args:
        profile_path: path to the profile .yaml file.
    """
    profile = _load_profile(Path(profile_path))

    dedup_schedule = profile.get("dedup_schedule")
    if dedup_schedule:
        try:
            anchor = _last_schedule_anchor(dedup_schedule)
            if anchor and has_digest_run_since(profile["name"], anchor):
                log.info(
                    "Skipping dispatch: %s already ran since last scheduled time (%s).",
                    profile["name"], anchor,
                )
                return
        except DbError as exc:
            log.warning("Could not check digest dedup — proceeding: %s", exc)

    llm = LLMClient()

    log.info("Dispatch starting: profile=%s", profile["name"])

    # Resolve all input bands
    bands = _resolve_bands(profile["inputs"])
    if not any(b.records for b in bands):
        log.info("No content found for any band — skipping dispatch")
        return

    # Determine period bounds from records bands
    period_start, period_end = _compute_period(bands)
    log.info("Period: %s → %s", period_start, period_end)

    # Prompt is inlined in the profile (last key), self-contained.
    system_prompt = profile["prompt"]

    # Build user prompt from bands
    user_prompt = _build_prompt(bands, period_start, period_end)

    # Determine model: profile override → synthesis vs digest routing
    model_override = profile.get("model")
    operation = (
        "synthesis" if any(b.records for b in bands if not b.tag_freq) else "digest"
    )
    has_digests_band = any(inp.get("source") == "digests" for inp in profile["inputs"])
    operation = "synthesis" if has_digests_band else "digest"

    html_body = llm.call(
        operation,
        user_prompt,
        system=system_prompt,
        model_override=model_override,
        max_tokens=config.DIGEST_MAX_TOKENS,
    )
    html_body = _strip_code_fence(html_body)
    html_body = _fix_back_links(html_body)

    # Collect IDs for DigestRuns record
    # sql bands: has "id" and no "profile_name"; digests bands: has "profile_name"
    content_ids = [
        r["id"]
        for b in bands
        for r in b.records
        if "id" in r and "profile_name" not in r
    ]
    prior_digest_ids = [
        r["id"]
        for b in bands
        for r in b.records
        if "profile_name" in r
    ]

    # Write DigestRuns
    try:
        run_id = insert_digest_run(
            profile_name=profile["name"],
            period_start=period_start,
            period_end=period_end,
            summary_text=html_body,
            content_ids=content_ids,
            prior_digest_ids=prior_digest_ids,
        )
        log.info("DigestRun saved: id=%d", run_id)
    except DbError as exc:
        log.error("Failed to save DigestRun: %s", exc)

    # Send email
    recipient = profile.get("recipient", config.DEFAULT_RECIPIENT)
    period_label = _period_label(period_start, period_end)
    name = profile["name"]
    subject_template = profile.get(
        "subject_template", "[ClearFeed] {name} — {period_label}"
    )
    period_end_date = f"{period_end.month}/{period_end.day}/{period_end.year}"
    subject = subject_template.format(
        name=name, period_label=period_label, period_end_date=period_end_date
    )

    outputs = profile.get("output", ["email"])
    if "email" in outputs:
        full_html = _wrap_html(html_body, subject, period_label, len(content_ids))
        attachments = []
        if config.DIGEST_PDF_ENABLED:
            try:
                pdf_bytes = _render_digest_pdf(html_body, subject, period_label)
                pdf_name = _pdf_filename(profile["name"], period_end)
                attachments.append((pdf_name, pdf_bytes, "application/pdf"))
            except Exception as exc:  # never let PDF rendering block delivery
                log.error("PDF render failed — sending without attachment: %s", exc)
        send_email(recipient, subject, full_html, attachments=attachments or None)
        log.info("Email sent to %s: %s", recipient, subject)


def _load_profile(path: Path) -> dict:
    """Load and minimally validate a digest profile YAML file."""
    if not path.is_absolute():
        path = config.BASE_DIR / path
    profile = read_yaml_profile(path)
    kind = profile.get("kind")
    if kind is not None and kind != "digest":
        raise ValueError(f"Expected kind: digest, got {kind!r}")
    for required in ("name", "prompt", "inputs"):
        if required not in profile:
            raise ValueError(f"Profile missing required field: {required!r}")

    for old_key in ("window_hours", "record_columns"):
        if old_key in profile:
            raise ValueError(
                f"Digest profile key {old_key!r} is no longer supported. "
                "Embed window and column selection directly in the band 'sql:' statement. "
                "See README for the new profile schema."
            )
    for band in profile.get("inputs", []):
        source = band.get("source")
        if source == "records":
            raise ValueError(
                "Band 'source: records' is no longer supported. "
                "Replace with 'source: sql' and inline the query in 'sql:'. "
                "See README for the new profile schema."
            )
        if source == "sql" and "sql" not in band:
            raise ValueError(
                f"Band section {band.get('section')!r} has source: sql but is missing 'sql:'"
            )
        if source == "sql" and "filter" in band:
            raise ValueError(
                f"Band section {band.get('section')!r}: 'filter' is no longer supported. "
                "Embed filter logic in the 'sql:' statement."
            )

    return profile


def _resolve_bands(inputs: list[dict]) -> list[BandResult]:
    """Resolve each inputs[] entry to a BandResult with actual records."""
    results = []
    for band_def in inputs:
        section = band_def["section"]
        source = band_def["source"]

        if source == "sql":
            fmt = band_def.get("format", "markdown")
            records = query_sql(band_def["sql"])
            tag_freq = _compute_tag_freq(records) if fmt == "markdown" else {}
            emphasis = _normalize_list(band_def.get("emphasis_tags"))
            results.append(
                BandResult(
                    section=section,
                    records=records,
                    tag_freq=tag_freq,
                    emphasis_tags=emphasis,
                    format=fmt,
                )
            )
            log.info("Band '%s' (sql/%s): %d row(s)", section, fmt, len(records))
            if fmt == "markdown":
                for rec in records:
                    log.info(
                        "  [%s] %s | from: %s | tags: %s",
                        rec.get("received_at", ""),
                        rec.get("subject", "(no subject)"),
                        rec.get("sender", ""),
                        rec.get("tags", ""),
                    )

        elif source == "digests":
            from_profile = band_def.get("from_profile", "")
            digests = query_digests_for_band(
                from_profile=from_profile,
                limit=band_def.get("limit", 7),
                window_hours=band_def.get("window_hours"),
            )
            results.append(BandResult(section=section, records=digests))
            log.info(
                "Band '%s': %d prior digests from '%s'",
                section, len(digests), from_profile,
            )

        else:
            log.warning(
                "Unknown band source %r in section %r — skipping", source, section
            )

    return results


def _compute_tag_freq(records: list[dict]) -> dict[str, int]:
    """Count tag occurrences across a set of records.

    Tags may be a comma-separated string (from STRING_AGG in SQL) or a list.
    """
    counter: Counter = Counter()
    for rec in records:
        tags_val = rec.get("tags")
        if tags_val is None:
            continue
        if isinstance(tags_val, str):
            tags = [t.strip() for t in tags_val.split(",") if t.strip()]
        else:
            tags = list(tags_val)
        counter.update(tags)
    return dict(counter.most_common())


def _normalize_list(values: list[str] | None) -> list[str] | None:
    """Normalize a filter list, or return None if empty/absent."""
    if not values:
        return None
    return [normalize_tag(v) for v in values]


def _build_prompt(
    bands: list[BandResult], period_start: datetime, period_end: datetime
) -> str:
    """Assemble the user-turn prompt from all resolved bands."""
    import json as _json

    period_label = _period_label(period_start, period_end)
    parts = [f"Period: {period_label}\n"]

    for band in bands:
        parts.append(f"\n## {band.section}\n")

        if not band.records:
            parts.append("(no content in this band)\n")
            continue

        # JSON format: emit the whole band as a fenced JSON blob
        if band.format == "json":
            parts.append("```json\n")
            parts.append(_json.dumps(band.records, indent=2, default=str))
            parts.append("\n```\n")
            continue

        # Markdown format: existing per-record bullet rendering
        if band.tag_freq:
            top_tags = list(band.tag_freq.items())[:20]
            freq_str = ", ".join(f"{t} ({n})" for t, n in top_tags)
            parts.append(f"Tag frequency: {freq_str}\n")

        if band.emphasis_tags:
            parts.append(f"Emphasis topics: {', '.join(band.emphasis_tags)}\n")

        for rec in band.records:
            if "subject" in rec:
                lines = [
                    f"- **{rec.get('subject', '')}** ({rec.get('sender', '')})",
                    f"  {rec.get('summary', '')}",
                ]
                if rec.get("executive_summary"):
                    lines.append(f"  Executive summary: {rec['executive_summary']}")
                if rec.get("linked_article_url"):
                    lines.append(f"  URL: {rec['linked_article_url']}")
                tags_val = rec.get("tags")
                if tags_val is None:
                    tags_str = ""
                elif isinstance(tags_val, str):
                    tags_str = tags_val
                else:
                    tags_str = ", ".join(tags_val)
                lines.append(f"  Tags: {tags_str}")
                parts.append("\n".join(lines) + "\n")
            elif "summary_text" in rec:
                end = rec.get("period_end", "")
                parts.append(f"--- Prior digest ({end}) ---\n{rec['summary_text']}\n")

    return "\n".join(parts)


def _compute_period(bands: list[BandResult]) -> tuple[datetime, datetime]:
    """Derive period_start and period_end from the widest records band."""
    now = now_cst()
    max_window = 0
    for band in bands:
        if band.tag_freq is not None and band.records:
            received_dates = [
                r["received_at"]
                for r in band.records
                if r.get("received_at") and isinstance(r["received_at"], datetime)
            ]
            if received_dates:
                earliest = min(received_dates)
                if (now - earliest).total_seconds() / 3600 > max_window:
                    max_window = int((now - earliest).total_seconds() / 3600)

    from datetime import timedelta

    period_start = now - timedelta(hours=max_window or 168)
    return period_start, now


def _period_label(start: datetime, end: datetime) -> str:
    return f"{start:%b %d}–{end:%b %d, %Y}"


def _wrap_html(body: str, title: str, period_label: str, record_count: int) -> str:
    """Wrap the LLM HTML body in a simple email shell."""
    return f"""<div style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">
<h1 style="color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px;">{title}</h1>
<p style="color: #666; font-size: 12px;">{period_label} &nbsp;·&nbsp; {record_count} item(s) processed</p>
{body}
<hr style="margin-top: 32px; border: none; border-top: 1px solid #e0e0e0;">
<p style="color: #999; font-size: 11px;">Generated by ClearFeed</p>
</div>"""


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences that the LLM sometimes wraps HTML output in.

    Handles `` ```html``, `` ```HTML``, bare `` ``` ``, and trailing `` ``` ``.
    Safe to call when no fence is present — returns the text unchanged.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)   # opening fence
    text = re.sub(r"\n?```\s*$", "", text)           # closing fence
    return text.strip()


# Matches a whole Further Information entry: <div id="fi-{slug}-{n}">...</div>.
# Profiles render these as flat siblings with no nested <div>, so a non-greedy
# body match correctly stops at each entry's own closing tag.
_FURTHER_INFO_ENTRY_RE = re.compile(
    r'(<div\s+id="(fi-[\w-]+)"[^>]*>)(.*?)(</div>)', re.DOTALL
)
# The "↑ Back" link inside a Further Information entry, wherever its href points.
_BACK_LINK_HREF_RE = re.compile(
    r'(<a\s+href=")[^"]*("[^>]*>[^<]*Back[^<]*</a>)', re.IGNORECASE
)


def _fix_back_links(html: str) -> str:
    """Repoint each Further Information entry's "Back" link at its own item.

    The digest prompt asks the LLM to write a Back link like href="#aitech-3"
    that recalls the item number from earlier in a long generation — unlike
    the forward "Further detail" link, which it writes right next to the
    item's own id and so stays consistent. In practice the model frequently
    gets the recalled number wrong, so Back rarely lands on the source item.
    Fix it deterministically instead: each entry's own id ("fi-aitech-3")
    already encodes the target ("aitech-3"), so derive the Back href from it
    rather than trusting the model's free-text recall.
    """

    def _fix_entry(match: re.Match) -> str:
        open_tag, entry_id, body, close_tag = match.groups()
        target = entry_id[len("fi-") :]
        body = _BACK_LINK_HREF_RE.sub(rf"\g<1>#{target}\g<2>", body, count=1)
        return f"{open_tag}{body}{close_tag}"

    return _FURTHER_INFO_ENTRY_RE.sub(_fix_entry, html)


def _add_pdf_anchors(html: str) -> str:
    """Inject an <a name="X"> before every element with id="X".

    xhtml2pdf resolves intra-document links via <a name> destinations; the digest
    targets carry id="..." instead, so add matching named anchors for the PDF.
    """
    return re.sub(r'(<\w+[^>]*\sid="([^"]+)"[^>]*>)', r'<a name="\2"></a>\1', html)


def _render_digest_pdf(body: str, title: str, period_label: str) -> bytes:
    """Render the digest HTML body to a PDF whose in-document links work on mobile.

    Page size and margins come from config (small page → large text fit-to-width
    on a phone). Raises on failure so the caller can fall back to no attachment.
    """
    from xhtml2pdf import pisa  # lazy import — keep module importable without it

    w = config.DIGEST_PDF_PAGE_WIDTH_IN
    h = config.DIGEST_PDF_PAGE_HEIGHT_IN
    mtb = config.DIGEST_PDF_MARGIN_TB_IN
    mlr = config.DIGEST_PDF_MARGIN_LR_IN
    doc = f"""<html><head><meta charset="utf-8"><style>
@page {{ size: {w}in {h}in; margin: {mtb}in {mlr}in; }}
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 12pt; line-height: 1.4; }}
h1 {{ font-size: 17pt; color:#1a1a2e; }} h2 {{ font-size: 14pt; }} h3 {{ font-size: 12.5pt; }}
a {{ color:#1155cc; }}
</style></head><body>
<h1>{title}</h1>
<p style="color:#666;font-size:10pt;">{period_label}</p>
{_add_pdf_anchors(body)}
</body></html>"""

    buf = io.BytesIO()
    result = pisa.CreatePDF(doc, dest=buf)
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s)")
    return buf.getvalue()


def _pdf_filename(profile_name: str, period_end: datetime) -> str:
    """Build a safe PDF filename like 'clearfeed_action_digest_2026-06-06.pdf'."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", profile_name).strip("_") or "digest"
    return f"{safe}_{period_end:%Y-%m-%d}.pdf"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m dispatch <profile.yaml>")
        sys.exit(1)
    setup_logging("dispatch")
    purge_old_logs()
    run_dispatch(sys.argv[1])
