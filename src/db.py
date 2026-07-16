"""PostgreSQL database client for ClearFeed.

Single connection-per-call pattern using psycopg (psycopg 3). All queries use
parameterized form (``%s`` placeholders) — no string interpolation — except
query_sql(), which executes trusted profile-supplied SQL verbatim.

Timestamp convention: naive US-Central datetimes. ``_CST_NOW`` expands to
``(now() AT TIME ZONE 'America/Chicago')``, which returns a naive local
timestamp — matching how Python writes timestamps via utils.now_cst().
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generator

import psycopg
from psycopg import errors as pg_errors

import config
import security_config

# SQL expression for current time in US Central (CST/CDT). Used everywhere a
# server-side "now" is needed so all stored timestamps are consistent.
_CST_NOW = "(now() AT TIME ZONE 'America/Chicago')"

log = logging.getLogger(__name__)


class DbError(Exception):
    """Raised when a database operation fails after retries."""


class DuplicateRecordError(Exception):
    """Raised when source_type + source_ref already exists (idempotency signal)."""


def _conn_kwargs() -> dict[str, Any]:
    return {
        "host": config.DB_HOST,
        "port": config.DB_PORT,
        "dbname": config.DB_NAME,
        "user": config.DB_USER,
        "password": security_config.DB_PASSWORD,
        "connect_timeout": config.DB_TIMEOUT_SECONDS,
    }


@contextmanager
def get_connection() -> Generator[psycopg.Connection, None, None]:
    """Context manager yielding a psycopg connection with autocommit off."""
    conn = psycopg.connect(autocommit=False, **_conn_kwargs())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class ContentRecord:
    """Mirrors the ContentRecords table for insert/read operations."""

    source_type: str
    source_ref: str
    received_at: datetime | None = None
    sender: str | None = None
    subject: str | None = None
    body_text: str | None = None
    linked_article_url: str | None = None
    image_local_paths: list[str] = field(default_factory=list)
    summary: str | None = None
    executive_summary: str | None = None   # summarize-step high-level summary
    summary_confidence: int | None = None  # summarize-step certainty 1-5; NULL when unavailable
    summary_rationale: str | None = None   # summarize-step explanation; max 1000 chars
    labels: list[str] = field(default_factory=list)  # RecordTerms kind=label
    tags: list[str] = field(default_factory=list)  # RecordTerms kind=tag
    classification_confidence: int | None = None  # classify-step certainty 1-5; NULL when unavailable
    classification_rationale: str | None = None   # classify-step label/tag explanation; max 1000 chars
    classify_model: str | None = None # full model ID used for the final classify call
    classify_tokens: int | None = None# total input+output tokens across all classify calls
    escalated: bool | None = None     # True if Sonnet re-call was triggered
    raw_html: str | None = None       # untruncated original text/html — stored in SourceDocuments
    raw_text: str | None = None       # untruncated original text/plain — stored in SourceDocuments
    id: int | None = None


def insert_content_record(rec: ContentRecord) -> int:
    """Insert a ContentRecord row plus its RecordTerms and SourceDocuments. Returns the new id.

    Raises:
        DuplicateRecordError: if source_type+source_ref already present.
        DbError: on other database failures.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                INSERT INTO ContentRecords
                    (source_type, source_ref, received_at, sender, subject,
                     body_text, linked_article_url, image_local_paths, summary,
                     executive_summary, summary_confidence, summary_rationale,
                     classification_confidence, classification_rationale,
                     classify_model, classify_tokens, escalated,
                     enrichment_status, processed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', {_CST_NOW})
                RETURNING id
                """,
                (
                    rec.source_type,
                    rec.source_ref,
                    rec.received_at,
                    rec.sender,
                    rec.subject,
                    rec.body_text,
                    rec.linked_article_url,
                    json.dumps(rec.image_local_paths),
                    rec.summary,
                    rec.executive_summary,
                    rec.summary_confidence,
                    rec.summary_rationale,
                    rec.classification_confidence,
                    rec.classification_rationale,
                    rec.classify_model,
                    rec.classify_tokens,
                    rec.escalated,
                ),
            )
            record_id: int = cursor.fetchone()[0]
            _insert_terms(cursor, record_id, "label", rec.labels)
            _insert_terms(cursor, record_id, "tag", rec.tags)
            _insert_source_document(cursor, record_id, rec.raw_html, rec.raw_text)
            return record_id
    except pg_errors.UniqueViolation as exc:
        raise DuplicateRecordError(
            f"Already ingested: {rec.source_type}/{rec.source_ref}"
        ) from exc
    except psycopg.Error as exc:
        raise DbError(f"DB error inserting record: {exc}") from exc


def _insert_terms(cursor: Any, record_id: int, kind: str, values: list[str]) -> None:
    """Bulk-insert RecordTerms rows for a single record + kind."""
    for value in values:
        cursor.execute(
            "INSERT INTO RecordTerms (id, kind, value) VALUES (%s, %s, %s)",
            (record_id, kind, value),
        )


def _insert_source_document(
    cursor: Any, record_id: int, raw_html: str | None, raw_text: str | None
) -> None:
    """Insert the full original source into SourceDocuments (1:1 with the record).

    Skipped when neither raw part is present (e.g. non-Gmail sources that don't
    yet capture originals).
    """
    if not (raw_html or raw_text):
        return
    cursor.execute(
        "INSERT INTO SourceDocuments (id, raw_html, raw_text) VALUES (%s, %s, %s)",
        (record_id, raw_html or None, raw_text or None),
    )


def record_exists(source_type: str, source_ref: str) -> bool:
    """Return True if a ContentRecord with this source identity already exists."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM ContentRecords WHERE source_type = %s AND source_ref = %s",
                (source_type, source_ref),
            )
            return cursor.fetchone() is not None
    except psycopg.Error as exc:
        raise DbError(f"DB error checking record existence: {exc}") from exc


def query_sql(
    sql: str,
    params: tuple = (),
    *,
    with_columns: bool = False,
) -> "list[dict] | tuple[list[str], list[dict]]":
    """Execute trusted profile-supplied SQL and return results as dicts.

    SECURITY NOTE: This function executes caller-supplied SQL without
    modification. It is intentionally unrestricted — ClearFeed is a
    single-user tool and all SQL comes from local YAML profile files.
    Never pass untrusted or user-supplied strings here.

    When ``params`` is empty the statement is executed with no parameter
    argument, so literal ``%`` characters (e.g. in ``LIKE '%foo%'`` clauses)
    are preserved rather than interpreted by psycopg as placeholders.

    Args:
        sql: SQL statement to execute.
        params: optional positional parameters for the statement.
        with_columns: when True, return (col_names, rows) instead of rows.

    Returns:
        list[dict] keyed by column name, or (list[str], list[dict]) when
        with_columns=True.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            col_names = [desc[0] for desc in cursor.description]
            rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]
            if with_columns:
                return col_names, rows
            return rows
    except psycopg.Error as exc:
        raise DbError(f"DB error executing profile SQL: {exc}") from exc


def query_digests_for_band(
    from_profile: str, limit: int = 7, window_hours: int | None = None
) -> list[dict]:
    """Return the most recent DigestRuns for a profile, newest first.

    Args:
        from_profile: profile_name to pull digests from.
        limit: number of prior digests to return.
        window_hours: when set, only digests whose period_end falls within
            this many hours of now are returned (legacy period cutoff).

    Returns:
        List of dicts with id, profile_name, period_start, period_end, summary_text.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            conditions = ["profile_name = %s"]
            params: list[Any] = [from_profile]
            if window_hours is not None:
                conditions.append(f"period_end >= {_CST_NOW} + (%s * interval '1 hour')")
                params.append(-window_hours)
            where = " AND ".join(conditions)
            cursor.execute(
                f"SELECT id, profile_name, period_start, period_end, summary_text "
                f"FROM DigestRuns WHERE {where} ORDER BY period_end DESC LIMIT %s",
                (*params, limit),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "profile_name": r[1],
                    "period_start": r[2],
                    "period_end": r[3],
                    "summary_text": r[4],
                }
                for r in rows
            ]
    except psycopg.Error as exc:
        raise DbError(f"DB error querying digests for band: {exc}") from exc


def query_prior_actions(profile_name: str, hours: int) -> list[str]:
    """Return action_content strings from recent successful ActionRuns for this profile.

    Used to inject prior-action context into aggregate-mode prompts so the LLM
    can suppress redundant task creation.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT action_content FROM ActionRuns
                WHERE profile_name = %s
                  AND status = 'created'
                  AND action_content IS NOT NULL
                  AND created_at >= {_CST_NOW} + (%s * interval '1 hour')
                ORDER BY created_at DESC
                """,
                (profile_name, -hours),
            )
            return [row[0] for row in cursor.fetchall()]
    except psycopg.Error as exc:
        raise DbError(f"DB error querying prior actions: {exc}") from exc


def _fetch_terms_for_ids(
    cursor: Any, record_ids: list[int]
) -> dict[int, dict[str, list[str]]]:
    """Fetch all RecordTerms for a list of record IDs.

    Returns: {record_id: {kind: [value, ...]}}
    """
    if not record_ids:
        return {}
    placeholders = ",".join(["%s"] * len(record_ids))
    cursor.execute(
        f"SELECT id, kind, value FROM RecordTerms WHERE id IN ({placeholders})",
        tuple(record_ids),
    )
    result: dict[int, dict[str, list[str]]] = {}
    for record_id, kind, value in cursor.fetchall():
        result.setdefault(record_id, {}).setdefault(kind, []).append(value)
    return result


# Gap conditions per step: a record is "incomplete" (an aborted/never-run gap)
# when ANY field that step's prompt produces is missing. Used by --gaps mode to
# fill only the records a crashed run left behind. Columns are unqualified to
# match the query below (table is ContentRecords, no alias).
_GAP_CONDITIONS: dict[str, str] = {
    "summarize": (
        "(summary IS NULL OR summary = '' "
        "OR executive_summary IS NULL OR executive_summary = '' "
        "OR summary_confidence IS NULL OR summary_rationale IS NULL)"
    ),
    "classify": (
        "(classification_confidence IS NULL OR classification_rationale IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM RecordTerms WHERE id = ContentRecords.id AND kind = 'label') "
        "OR NOT EXISTS (SELECT 1 FROM RecordTerms WHERE id = ContentRecords.id AND kind = 'tag'))"
    ),
}


def query_records_for_reprocess(
    filter_labels: list[str] | None = None,
    filter_tags: list[str] | None = None,
    since: datetime | None = None,
    days: int | None = None,
    gaps_for: str | None = None,
) -> list[dict]:
    """Fetch ContentRecords for selective reprocessing.

    All provided filters are AND'd. With no filters, returns all records that
    have body_text (i.e. every ingestable record).

    Args:
        filter_labels: match records carrying ANY of these labels.
        filter_tags: match records carrying ANY of these tags.
        since: lower bound on received_at (inclusive).
        days: alternative to since — records received in the last N days.
        gaps_for: when "summarize" or "classify", additionally restrict to records
            missing any output of that step (gap-fill mode for aborted runs).

    Returns:
        List of dicts with id, subject, sender, body_text, summary, processed_at.
    """
    if gaps_for is not None and gaps_for not in _GAP_CONDITIONS:
        raise ValueError(f"Unknown gaps_for {gaps_for!r}. Valid: {list(_GAP_CONDITIONS)}")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            conditions: list[str] = ["body_text IS NOT NULL"]
            params: list[Any] = []

            if gaps_for is not None:
                conditions.append(_GAP_CONDITIONS[gaps_for])

            if filter_labels:
                placeholders = ",".join(["%s"] * len(filter_labels))
                conditions.append(
                    f"EXISTS (SELECT 1 FROM RecordTerms WHERE id = ContentRecords.id "
                    f"AND kind = 'label' AND value IN ({placeholders}))"
                )
                params.extend(filter_labels)

            if filter_tags:
                placeholders = ",".join(["%s"] * len(filter_tags))
                conditions.append(
                    f"EXISTS (SELECT 1 FROM RecordTerms WHERE id = ContentRecords.id "
                    f"AND kind = 'tag' AND value IN ({placeholders}))"
                )
                params.extend(filter_tags)

            if days:
                conditions.append(f"received_at >= {_CST_NOW} + (%s * interval '1 day')")
                params.append(-days)
            elif since:
                conditions.append("received_at >= %s")
                params.append(since)

            where = " AND ".join(conditions)
            cursor.execute(
                f"SELECT id, subject, sender, body_text, processed_at, summary "
                f"FROM ContentRecords WHERE {where} ORDER BY received_at DESC",
                tuple(params),
            )
            rows = cursor.fetchall()
            record_ids = [r[0] for r in rows]
            terms_by_id = _fetch_terms_for_ids(cursor, record_ids)
            return [
                {
                    "id": r[0],
                    "subject": r[1] or "",
                    "sender": r[2] or "",
                    "body_text": r[3],
                    "processed_at": r[4],
                    "summary": r[5] or "",
                    "labels": terms_by_id.get(r[0], {}).get("label", []),
                    "tags": terms_by_id.get(r[0], {}).get("tag", []),
                }
                for r in rows
            ]
    except psycopg.Error as exc:
        raise DbError(f"DB error querying records for reprocess: {exc}") from exc


def update_record_classification(
    record_id: int,
    labels: list[str],
    tags: list[str],
    classification_confidence: int | None = None,
    classification_rationale: str | None = None,
    classify_model: str | None = None,
    classify_tokens: int | None = None,
    escalated: bool | None = None,
) -> None:
    """Overwrite classify-step fields for an existing record and bump processed_at.

    Replaces all label/tag RecordTerms rows and updates ContentRecords columns:
    classification_confidence, classification_rationale, classify_model,
    classify_tokens, escalated. The summary and its confidence/rationale are left
    untouched — reprocess re-runs only the classify step.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM RecordTerms WHERE id = %s AND kind IN ('label', 'tag')",
                (record_id,),
            )
            _insert_terms(cursor, record_id, "label", labels)
            _insert_terms(cursor, record_id, "tag", tags)
            cursor.execute(
                f"""UPDATE ContentRecords
                    SET classification_confidence = %s, classification_rationale = %s,
                        classify_model = %s, classify_tokens = %s, escalated = %s,
                        processed_at = {_CST_NOW}
                    WHERE id = %s""",
                (
                    classification_confidence,
                    classification_rationale,
                    classify_model,
                    classify_tokens,
                    escalated,
                    record_id,
                ),
            )
    except psycopg.Error as exc:
        raise DbError(f"DB error updating record classification: {exc}") from exc


def update_record_summary(
    record_id: int,
    summary: str,
    executive_summary: str | None = None,
    summary_confidence: int | None = None,
    summary_rationale: str | None = None,
) -> None:
    """Overwrite summarize-step fields for an existing record and bump processed_at.

    Updates ContentRecords columns: summary, executive_summary, summary_confidence,
    summary_rationale. Labels/tags and the classify-step fields are left untouched —
    this re-runs only the summarize step.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""UPDATE ContentRecords
                    SET summary = %s, executive_summary = %s,
                        summary_confidence = %s, summary_rationale = %s,
                        processed_at = {_CST_NOW}
                    WHERE id = %s""",
                (
                    summary,
                    executive_summary,
                    summary_confidence,
                    summary_rationale,
                    record_id,
                ),
            )
    except psycopg.Error as exc:
        raise DbError(f"DB error updating record summary: {exc}") from exc


def has_digest_run_since(profile_name: str, since: datetime) -> bool:
    """Return True if a DigestRun for this profile was created at or after `since`."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM DigestRuns "
                "WHERE profile_name = %s AND created_at >= %s LIMIT 1",
                (profile_name, since),
            )
            return cursor.fetchone() is not None
    except psycopg.Error as exc:
        raise DbError(f"DB error checking digest run since {since}: {exc}") from exc


def insert_digest_run(
    profile_name: str,
    period_start: datetime,
    period_end: datetime,
    summary_text: str,
    content_ids: list[int],
    prior_digest_ids: list[int],
) -> int:
    """Insert a DigestRuns row. Returns the new id."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO DigestRuns
                    (profile_name, period_start, period_end, summary_text,
                     content_ids, prior_digest_ids, record_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    profile_name,
                    period_start,
                    period_end,
                    summary_text,
                    json.dumps(content_ids),
                    json.dumps(prior_digest_ids),
                    len(content_ids),
                ),
            )
            return cursor.fetchone()[0]
    except psycopg.Error as exc:
        raise DbError(f"DB error inserting digest run: {exc}") from exc
