"""Tests for export pipeline."""

import sys
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _mock_connection(col_names=None, rows=None):
    mock_cursor = MagicMock()
    mock_cursor.description = [(c,) for c in (col_names or [])]
    mock_cursor.fetchall.return_value = rows or []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    @contextmanager
    def _ctx():
        yield mock_conn

    return _ctx, mock_cursor


# ---------------------------------------------------------------------------
# Profile loading — valid and invalid
# ---------------------------------------------------------------------------


def _write_valid_profile(tmp_path):
    p = tmp_path / "export.yaml"
    p.write_text(
        "kind: export\nname: test_export\n"
        "sql: SELECT cr.id FROM ContentRecords cr LIMIT 500\n"
        "output_path: /tmp/exports\n"
        "filename_template: export_{timestamp}.xlsx\n"
    )
    return p


def test_load_profile_accepts_valid_export(tmp_path):
    from export import _load_profile

    p = _write_valid_profile(tmp_path)
    profile = _load_profile(p)
    assert profile["name"] == "test_export"
    assert "SELECT" in profile["sql"]


def test_load_profile_rejects_filter_key(tmp_path):
    from export import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: export\nname: x\nfilter: {labels: [Professional]}\n"
        "output_path: /tmp\nfilename_template: x_{timestamp}.xlsx\n"
    )
    with pytest.raises(ValueError, match="filter"):
        _load_profile(p)


def test_load_profile_rejects_columns_key(tmp_path):
    from export import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: export\nname: x\ncolumns: [subject, sender]\n"
        "output_path: /tmp\nfilename_template: x_{timestamp}.xlsx\n"
    )
    with pytest.raises(ValueError, match="columns"):
        _load_profile(p)


def test_load_profile_rejects_window_hours_key(tmp_path):
    from export import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: export\nname: x\nwindow_hours: 48\n"
        "output_path: /tmp\nfilename_template: x_{timestamp}.xlsx\n"
    )
    with pytest.raises(ValueError, match="window_hours"):
        _load_profile(p)


def test_load_profile_rejects_limit_key(tmp_path):
    from export import _load_profile

    p = tmp_path / "old.yaml"
    p.write_text(
        "kind: export\nname: x\nlimit: 500\n"
        "output_path: /tmp\nfilename_template: x_{timestamp}.xlsx\n"
    )
    with pytest.raises(ValueError, match="limit"):
        _load_profile(p)


def test_load_profile_raises_on_missing_sql(tmp_path):
    from export import _load_profile

    p = tmp_path / "no_sql.yaml"
    p.write_text(
        "kind: export\nname: x\noutput_path: /tmp\nfilename_template: x_{timestamp}.xlsx\n"
    )
    with pytest.raises(ValueError, match="sql"):
        _load_profile(p)


def test_load_profile_raises_on_missing_file(tmp_path):
    from export import _load_profile

    with pytest.raises(FileNotFoundError):
        _load_profile(tmp_path / "ghost.yaml")


# ---------------------------------------------------------------------------
# Column headers from cursor.description
# ---------------------------------------------------------------------------


def test_run_export_uses_cursor_description_for_headers(tmp_path):
    """Column headers in the xlsx must come from cursor.description, not a static list."""
    from export import run_export

    p = _write_valid_profile(tmp_path)

    col_names = ["id", "subject", "sender", "processed_at", "summary", "linked_article_url"]
    rows = [(1, "Test Subject", "a@b.com", "2026-06-12", "A summary.", "https://example.com")]

    saved_args: list = []

    def fake_save_excel(headers, data_rows, output_path, filename_template=None, profile_name=None):
        saved_args.append((headers, data_rows, output_path))

    with patch("export.query_sql") as mock_qs, \
         patch("export._save_excel", side_effect=fake_save_excel):
        # query_sql with_columns=True returns (col_names, rows)
        mock_qs.return_value = (col_names, [dict(zip(col_names, rows[0]))])
        run_export(p)

    assert len(saved_args) == 1
    headers, data_rows, _ = saved_args[0]
    assert headers == col_names


def test_run_export_query_sql_called_with_profile_sql(tmp_path):
    """run_export must call query_sql with the profile's SQL string."""
    from export import run_export

    p = _write_valid_profile(tmp_path)

    with patch("export.query_sql") as mock_qs, \
         patch("export._save_excel"):
        mock_qs.return_value = (["id"], [])
        run_export(p)

    called_sql = mock_qs.call_args[0][0]
    assert "SELECT" in called_sql
    assert "ContentRecords" in called_sql
