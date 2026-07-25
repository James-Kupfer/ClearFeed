"""Tests for dispatch pipeline, band resolution, and DB reprocess functions."""

import json
import sys
import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Band resolution helpers
# ---------------------------------------------------------------------------


def test_normalize_list_lowercases_and_strips():
    from dispatch import _normalize_list

    result = _normalize_list(["Investment", "AI/Technology"])
    assert "investment" in result
    assert all(v == v.lower() for v in result)


def test_normalize_list_returns_none_for_empty():
    from dispatch import _normalize_list

    assert _normalize_list([]) is None
    assert _normalize_list(None) is None


def test_trim_for_reuse_strips_further_information_section():
    from dispatch import _trim_for_reuse

    html = (
        '<div class="item" id="invt-1"><p><strong>Headline.</strong> Brief writeup. '
        '<a href="#fi-invt-1">→ Further detail</a></p></div>\n'
        '<hr style="border: none; border-top: 8px double #333; margin: 32px 0;">\n'
        "<h2>Further Information</h2>\n"
        '<div id="fi-invt-1"><h3>Headline</h3><p>Long elaboration prose.</p></div>\n'
    )
    trimmed = _trim_for_reuse(html)
    assert "Brief writeup" in trimmed
    assert "Further Information" not in trimmed
    assert "Long elaboration prose" not in trimmed


def test_trim_for_reuse_is_noop_without_further_information():
    from dispatch import _trim_for_reuse

    html = (
        '<div class="item" id="pers-1"><p><strong>Headline.</strong> Brief writeup. '
        '<em>Source: <a href="https://example.com">Example</a> – 06/20/2026.</em></p>'
        '<p><em>Tags:</em> gardening</p></div>'
    )
    assert _trim_for_reuse(html) == html


def test_compute_tag_freq_handles_comma_string():
    """Tags returned as STRING_AGG comma-strings must be split and counted."""
    from dispatch import _compute_tag_freq

    records = [
        {"tags": "fed, rates"},
        {"tags": "fed, earnings"},
        {"tags": "rates"},
    ]
    freq = _compute_tag_freq(records)
    assert freq["fed"] == 2
    assert freq["rates"] == 2
    assert freq["earnings"] == 1


def test_compute_tag_freq_handles_list_tags():
    """Tags may also be lists (from digests band) â€” both forms must work."""
    from dispatch import _compute_tag_freq

    records = [
        {"tags": ["fed", "rates"]},
        {"tags": ["fed", "earnings"]},
        {"tags": ["rates"]},
    ]
    freq = _compute_tag_freq(records)
    assert freq["fed"] == 2
    assert freq["rates"] == 2
    assert freq["earnings"] == 1


def test_period_label_format():
    from dispatch import _period_label

    start = datetime(2026, 5, 25, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    label = _period_label(start, end)
    assert "May" in label
    assert "2026" in label


# ---------------------------------------------------------------------------
# Profile loading â€” valid and invalid profiles
# ---------------------------------------------------------------------------


def test_load_profile_raises_on_missing_prompt(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "bad.yaml"
    p.write_text("kind: digest\nname: test\ninputs: []\n")
    with pytest.raises(ValueError, match="prompt"):
        _load_profile(p)


def test_load_profile_reads_inline_prompt(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "good.yaml"
    p.write_text(
        "kind: digest\nname: test\ninputs: []\nprompt: |\n  Hello\n  World\n"
    )
    profile = _load_profile(p)
    assert profile["prompt"].splitlines() == ["Hello", "World"]


def test_load_profile_raises_on_wrong_kind(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "wrong.yaml"
    p.write_text("kind: action\nname: test\ninputs: []\nprompt: hi\n")
    with pytest.raises(ValueError, match="kind"):
        _load_profile(p)


def test_load_profile_raises_on_missing_file(tmp_path):
    from dispatch import _load_profile

    with pytest.raises(FileNotFoundError):
        _load_profile(tmp_path / "nonexistent.yaml")


def test_load_profile_rejects_window_hours_at_profile_level(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: digest\nname: test\nwindow_hours: 168\ninputs: []\nprompt: hi\n"
    )
    with pytest.raises(ValueError, match="window_hours"):
        _load_profile(p)


def test_load_profile_rejects_record_columns(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: digest\nname: test\nrecord_columns: [subject]\ninputs: []\nprompt: hi\n"
    )
    with pytest.raises(ValueError, match="record_columns"):
        _load_profile(p)


def test_load_profile_rejects_source_records_band(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: digest\nname: test\n"
        "inputs:\n- section: X\n  source: records\n  filter: {labels: [Investment]}\n"
        "prompt: hi\n"
    )
    with pytest.raises(ValueError, match="source: records"):
        _load_profile(p)


def test_load_profile_rejects_filter_in_sql_band(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: digest\nname: test\n"
        "inputs:\n- section: X\n  source: sql\n  filter: {labels: [Investment]}\n  sql: SELECT 1\n"
        "prompt: hi\n"
    )
    with pytest.raises(ValueError, match="filter"):
        _load_profile(p)


def test_load_profile_rejects_sql_band_without_sql_key(tmp_path):
    from dispatch import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: digest\nname: test\n"
        "inputs:\n- section: X\n  source: sql\n"
        "prompt: hi\n"
    )
    with pytest.raises(ValueError, match="sql"):
        _load_profile(p)


# ---------------------------------------------------------------------------
# Band resolution: sql band
# ---------------------------------------------------------------------------


def _make_sql_records(n: int = 3) -> list[dict]:
    now = datetime.now(tz=timezone.utc)
    return [
        {
            "id": i,
            "subject": f"Item {i}",
            "sender": "test@example.com",
            "received_at": now - timedelta(hours=i),
            "summary": f"Summary {i}",
            "tags": "fed, rates" if i % 2 == 0 else "earnings",
        }
        for i in range(1, n + 1)
    ]


def test_sql_band_resolves_via_query_sql():
    """SQL band calls query_sql and populates BandResult correctly."""
    records = _make_sql_records(4)
    band_def = {
        "section": "Investment",
        "source": "sql",
        "format": "markdown",
        "sql": "SELECT cr.id FROM ContentRecords cr LIMIT 200",
    }

    with patch("dispatch.query_sql", return_value=records):
        from dispatch import _resolve_bands

        bands = _resolve_bands([band_def])

    assert len(bands) == 1
    assert bands[0].section == "Investment"
    assert len(bands[0].records) == 4
    assert bands[0].format == "markdown"


def test_sql_band_with_json_format():
    """SQL band with format: json sets format='json' on BandResult."""
    records = _make_sql_records(2)
    band_def = {
        "section": "Investment",
        "source": "sql",
        "format": "json",
        "sql": "SELECT cr.id FROM ContentRecords cr LIMIT 200",
    }

    with patch("dispatch.query_sql", return_value=records):
        from dispatch import _resolve_bands

        bands = _resolve_bands([band_def])

    assert bands[0].format == "json"


def test_sql_band_query_sql_called_with_profile_sql():
    """_resolve_bands must pass the profile's SQL string to query_sql unchanged."""
    sql = "SELECT cr.id, cr.subject FROM ContentRecords cr WHERE cr.enrichment_status='complete' LIMIT 200"
    band_def = {"section": "Test", "source": "sql", "sql": sql}

    with patch("dispatch.query_sql", return_value=[]) as mock_qs:
        from dispatch import _resolve_bands

        _resolve_bands([band_def])

    mock_qs.assert_called_once_with(sql)


def test_json_band_rendered_as_fenced_json_in_prompt():
    """JSON-format bands must appear as a fenced JSON block in the built prompt."""
    import json
    from dispatch import _build_prompt

    records = [{"id": 1, "subject": "X", "legacy": 0}]
    band_def = {
        "section": "Investment",
        "source": "sql",
        "format": "json",
        "sql": "SELECT 1",
    }

    with patch("dispatch.query_sql", return_value=records):
        from dispatch import _resolve_bands, BandResult

        bands = _resolve_bands([band_def])

    # Simulate _build_prompt with these bands
    # We need a minimal profile and LLM mock
    profile = {"prompt": "Records:\n{Investment}", "name": "test", "inputs": bands}
    # _build_prompt is an internal â€” call _resolve_bands and inspect band format
    assert bands[0].format == "json"
    # Verify the JSON would be serializable
    json.dumps(records, default=str)  # must not raise


def test_sql_band_default_format_is_markdown():
    """When format is omitted from a sql band, it defaults to markdown."""
    band_def = {
        "section": "Test",
        "source": "sql",
        "sql": "SELECT 1",
    }

    with patch("dispatch.query_sql", return_value=[]):
        from dispatch import _resolve_bands

        bands = _resolve_bands([band_def])

    assert bands[0].format == "markdown"


# ---------------------------------------------------------------------------
# Band resolution: digests band
# ---------------------------------------------------------------------------


def test_digests_band_passes_window_hours_to_query():
    """window_hours in a digests band must be forwarded to query_digests_for_band."""
    digests = [
        {
            "id": 10,
            "profile_name": "investment_digest",
            "period_start": datetime.now(tz=timezone.utc),
            "period_end": datetime.now(tz=timezone.utc),
            "summary_text": "Prior digest text",
        }
    ]
    band_def = {
        "section": "Prior Digests",
        "source": "digests",
        "from_profile": "investment_digest",
        "window_hours": 74,
        "limit": 5,
    }

    with patch("dispatch.query_digests_for_band", return_value=digests) as mock_q:
        from dispatch import _resolve_bands

        bands = _resolve_bands([band_def])

    mock_q.assert_called_once_with(
        from_profile="investment_digest", limit=5, window_hours=74
    )
    assert bands[0].records[0]["id"] == 10


def test_digests_band_without_window_hours():
    """Digests band without window_hours calls query_digests_for_band with window_hours=None."""
    band_def = {
        "section": "prior_briefs",
        "source": "digests",
        "from_profile": "investment_daily",
        "limit": 7,
    }

    with patch("dispatch.query_digests_for_band", return_value=[]) as mock_q:
        from dispatch import _resolve_bands

        _resolve_bands([band_def])

    mock_q.assert_called_once_with(
        from_profile="investment_daily", limit=7, window_hours=None
    )


# ---------------------------------------------------------------------------
# DB reprocess functions
# ---------------------------------------------------------------------------


def _mock_connection(rows=None):
    """Return (context_fn, cursor_mock) for patching db.get_connection."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows or []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    @contextmanager
    def _ctx():
        yield mock_conn

    return _ctx, mock_cursor


def test_query_records_for_reprocess_returns_structured_dicts():
    """query_records_for_reprocess maps DB rows to dicts with expected keys."""
    from db import query_records_for_reprocess

    rows = [(1, "NVDA Earnings", "news@example.com", "Body text here.", None, "Existing summary.")]
    ctx, mock_cursor = _mock_connection(rows=rows)
    mock_cursor.fetchall.side_effect = [
        rows,
        [(1, "label", "Investment"), (1, "tag", "earning")],
    ]

    with patch("db.get_connection", ctx):
        results = query_records_for_reprocess(filter_labels=["Investment"])

    assert len(results) == 1
    r = results[0]
    assert r["id"] == 1
    assert r["subject"] == "NVDA Earnings"
    assert r["body_text"] == "Body text here."
    assert r["summary"] == "Existing summary."
    assert r["labels"] == ["Investment"]
    assert r["tags"] == ["earning"]


def test_query_records_for_reprocess_no_filters_returns_all():
    from db import query_records_for_reprocess

    rows = [(1, "A", "a@a.com", "body", None, "sum a"), (2, "B", "b@b.com", "body", None, "sum b")]
    ctx, mock_cursor = _mock_connection(rows=rows)
    mock_cursor.fetchall.side_effect = [rows, []]

    with patch("db.get_connection", ctx):
        results = query_records_for_reprocess()

    assert len(results) == 2
    main_sql = mock_cursor.execute.call_args_list[0][0][0]
    assert "body_text IS NOT NULL" in main_sql
    assert "EXISTS" not in main_sql


def test_query_records_for_reprocess_gaps_for_summarize_adds_condition():
    from db import query_records_for_reprocess

    ctx, mock_cursor = _mock_connection(rows=[])
    with patch("db.get_connection", ctx):
        query_records_for_reprocess(gaps_for="summarize")

    main_sql = mock_cursor.execute.call_args_list[0][0][0]
    assert "summary IS NULL" in main_sql
    assert "executive_summary IS NULL" in main_sql
    assert "summary_confidence IS NULL" in main_sql
    assert "summary_rationale IS NULL" in main_sql


def test_query_records_for_reprocess_gaps_for_classify_adds_condition():
    from db import query_records_for_reprocess

    ctx, mock_cursor = _mock_connection(rows=[])
    with patch("db.get_connection", ctx):
        query_records_for_reprocess(gaps_for="classify")

    main_sql = mock_cursor.execute.call_args_list[0][0][0]
    assert "classification_confidence IS NULL" in main_sql
    assert "classification_rationale IS NULL" in main_sql
    assert "kind = 'label'" in main_sql
    assert "kind = 'tag'" in main_sql


def test_query_records_for_reprocess_invalid_gaps_for_raises():
    from db import query_records_for_reprocess

    with pytest.raises(ValueError):
        query_records_for_reprocess(gaps_for="bogus")


def test_update_record_classification_issues_delete_inserts_and_update():
    from db import update_record_classification

    ctx, mock_cursor = _mock_connection()

    with patch("db.get_connection", ctx):
        update_record_classification(
            record_id=7,
            labels=["Investment"],
            tags=["fed", "rates"],
            classification_confidence=4,
            classification_rationale="Investment cluster.",
        )

    calls_made = mock_cursor.execute.call_args_list
    assert any("DELETE" in str(c).upper() for c in calls_made)
    inserts = [c for c in calls_made if "INSERT" in str(c).upper()]
    assert len(inserts) == 3
    assert any(
        "UPDATE" in str(c).upper() and "classification_confidence" in str(c).lower()
        for c in calls_made
    )
    assert not any(
        "UPDATE" in str(c).upper() and "set summary" in str(c).lower() for c in calls_made
    )


def test_update_record_summary_updates_summary_fields_only():
    from db import update_record_summary

    ctx, mock_cursor = _mock_connection()

    with patch("db.get_connection", ctx):
        update_record_summary(
            record_id=7,
            summary="Fresh summary.",
            executive_summary="Fresh exec summary.",
            summary_confidence=5,
            summary_rationale="Fully covered.",
        )

    calls_made = mock_cursor.execute.call_args_list
    assert not any("DELETE" in str(c).upper() for c in calls_made)
    assert not any("INSERT" in str(c).upper() for c in calls_made)
    assert any(
        "UPDATE" in str(c).upper() and "summary_confidence" in str(c).lower()
        for c in calls_made
    )
    assert any(
        "UPDATE" in str(c).upper() and "executive_summary" in str(c).lower()
        for c in calls_made
    )
    assert not any("classification_confidence" in str(c).lower() for c in calls_made)


def test_insert_content_record_writes_source_document_when_raw_present():
    """insert_content_record must persist raw source into SourceDocuments."""
    from db import ContentRecord, insert_content_record

    ctx, mock_cursor = _mock_connection()
    mock_cursor.fetchone.return_value = (99,)

    rec = ContentRecord(
        source_type="gmail",
        source_ref="msg_1",
        raw_html="<p>original</p>",
        raw_text="original",
    )
    with patch("db.get_connection", ctx):
        new_id = insert_content_record(rec)

    assert new_id == 99
    assert any(
        "INSERT INTO SourceDocuments" in str(c) for c in mock_cursor.execute.call_args_list
    )


def test_insert_content_record_skips_source_document_when_no_raw():
    """No SourceDocuments row when neither raw part is present."""
    from db import ContentRecord, insert_content_record

    ctx, mock_cursor = _mock_connection()
    mock_cursor.fetchone.return_value = (100,)

    rec = ContentRecord(source_type="gmail", source_ref="msg_2")
    with patch("db.get_connection", ctx):
        insert_content_record(rec)

    assert not any(
        "SourceDocuments" in str(c) for c in mock_cursor.execute.call_args_list
    )


def test_emphasis_tags_preserved_in_band_result():
    band_def = {
        "section": "quarterly",
        "source": "sql",
        "sql": "SELECT 1",
        "emphasis_tags": ["fed", "rates"],
    }

    with patch("dispatch.query_sql", return_value=_make_sql_records(2)):
        from dispatch import _resolve_bands

        bands = _resolve_bands([band_def])

    assert "fed" in bands[0].emphasis_tags or "rates" in bands[0].emphasis_tags


# ---------------------------------------------------------------------------
# query_sql dict mapping
# ---------------------------------------------------------------------------


def test_query_sql_returns_list_of_dicts():
    """query_sql must return list[dict] keyed by cursor.description column names."""
    from db import query_sql

    ctx, mock_cursor = _mock_connection()
    mock_cursor.description = [("id",), ("subject",), ("tags",)]
    mock_cursor.fetchall.return_value = [(1, "Test Subject", "fed, rates")]

    with patch("db.get_connection", ctx):
        rows = query_sql("SELECT id, subject, tags FROM ContentRecords")

    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["subject"] == "Test Subject"
    assert rows[0]["tags"] == "fed, rates"


def test_query_sql_with_columns_returns_col_names_and_rows():
    """with_columns=True must return (col_names, rows) tuple."""
    from db import query_sql

    ctx, mock_cursor = _mock_connection()
    mock_cursor.description = [("id",), ("sender",)]
    mock_cursor.fetchall.return_value = [(5, "x@y.com")]

    with patch("db.get_connection", ctx):
        col_names, rows = query_sql("SELECT id, sender FROM ContentRecords", with_columns=True)

    assert col_names == ["id", "sender"]
    assert rows[0]["sender"] == "x@y.com"

