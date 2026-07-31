"""Tests for Gmail ingest pipeline — focused on data-loss prevention.

All external dependencies (Gmail API, DB, LLM) are mocked so the suite runs
unattended without credentials.
"""

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

# Put src/ on path so we can import without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gmail():
    """Minimal GmailClient mock."""
    m = MagicMock()
    m.ensure_labels.return_value = {
        "Investment": "Label_1",
        "AI": "Label_2",
        "Technology": "Label_3",
        "Science": "Label_4",
        "Medicine": "Label_5",
        "Professional": "Label_6",
        "Personal": "Label_7",
        "Miscellaneous": "Label_8",
        "Spam": "Label_9",
        "ProcessedClearFeed": "Label_10",
    }
    m.fetch_ingest_threads.return_value = [{"id": "thread_001"}]
    m.get_thread_messages.return_value = [
        {
            "id": "msg_001",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Earnings beat: NVDA"},
                    {"name": "From", "value": "newsletter@example.com"},
                    {"name": "Date", "value": "Mon, 26 May 2026 09:00:00 +0000"},
                ],
                "mimeType": "text/plain",
                "body": {
                    "data": "TlZEQSBiZWF0IGVhcm5pbmdzIGV4cGVjdGF0aW9ucyBpbiBRMS4="  # "NVDA beat earnings expectations in Q1."
                },
                "parts": [],
            },
        }
    ]
    m.extract_message_parts.return_value = {
        "subject": "Earnings beat: NVDA",
        "sender": "newsletter@example.com",
        "date_str": "Mon, 26 May 2026 09:00:00 +0000",
        "body_text": "NVDA beat earnings expectations in Q1.",
        "html_body": "",
        "image_attachments": [],
    }
    return m


@pytest.fixture
def mock_llm():
    m = MagicMock()
    # One dict serves both call_json calls (summarize + classify); each step
    # reads only the keys it needs.
    m.call_json.return_value = {
        "summary": "NVDA beat Q1 earnings expectations.",
        "executive_summary": "NVDA topped Q1 estimates.",
        "summary_confidence": 4,
        "summary_rationale": "Clear earnings beat; key figures captured.",
        "tags": ["earnings", "semiconductor"],
        "labels": ["Investment"],
        "classification_confidence": 4,
        "classification_rationale": "Tags cluster in Investment (earnings, semiconductor). No purpose signals.",
    }
    m.consume_usage.return_value = (None, None)
    return m


# ---------------------------------------------------------------------------
# Data-loss guard: validation failure must not label or trash
# ---------------------------------------------------------------------------


def test_empty_body_does_not_label_or_trash(mock_gmail, mock_llm):
    """If body extraction yields nothing, thread must not be labeled or trashed."""
    mock_gmail.extract_message_parts.return_value = {
        "subject": "Test",
        "sender": "x@example.com",
        "date_str": "",
        "body_text": "",
        "html_body": "",
        "image_attachments": [],
    }

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record"
    ) as mock_insert, patch(
        "ingest_gmail._download_images", return_value=[]
    ):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "failed"
    mock_gmail.apply_labels.assert_not_called()
    mock_gmail.trash_thread.assert_not_called()
    mock_insert.assert_not_called()


def test_db_error_does_not_label_or_trash(mock_gmail, mock_llm):
    """If DB insert fails, thread must not be labeled or trashed."""
    from db import DbError

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", side_effect=DbError("DB down")
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "failed"
    mock_gmail.apply_labels.assert_not_called()
    mock_gmail.trash_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency: duplicate source_ref must skip without LLM call, apply label
# ---------------------------------------------------------------------------


def test_duplicate_source_ref_is_skipped(mock_gmail, mock_llm):
    """Early duplicate check skips before LLM call and applies ProcessedClearFeed."""
    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=True), patch(
        "ingest_gmail.insert_content_record"
    ) as mock_insert:
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "duplicate"
    # LLM must not be called — duplicate detected before classify
    mock_llm.call_json.assert_not_called()
    # ProcessedClearFeed label applied to prevent re-fetching
    mock_gmail.apply_labels.assert_called_once_with("thread_001", ["Label_10"])
    # Thread must not be trashed
    mock_gmail.trash_thread.assert_not_called()
    # DB insert must not be called
    mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path: successful ingest applies labels then trashes
# ---------------------------------------------------------------------------


def test_successful_ingest_applies_labels_then_trashes(mock_gmail, mock_llm):
    """Happy path: DB insert succeeds, labels applied, then thread trashed."""
    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", return_value=42
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "ingested"
    assert results[0].record_id == 42
    mock_gmail.apply_labels.assert_called_once()
    mock_gmail.trash_thread.assert_called_once_with("thread_001")

    # Labels must be applied BEFORE trash
    label_call_idx = [c[0] for c in mock_gmail.method_calls].index("apply_labels")
    trash_call_idx = [c[0] for c in mock_gmail.method_calls].index("trash_thread")
    assert label_call_idx < trash_call_idx


# ---------------------------------------------------------------------------
# Source retention: full original raw HTML/plain captured untruncated
# ---------------------------------------------------------------------------


def test_ingest_captures_untruncated_raw_source(mock_gmail, mock_llm):
    """rec.raw_html/raw_text must hold the full original, even when body_text is truncated."""
    import config

    big_plain = "PLAIN " * 8000          # ~48k chars, exceeds BODY_TEXT_CAP (30k)
    big_html = "<p>" + ("HTML " * 8000) + "</p>"
    mock_gmail.extract_message_parts.return_value = {
        "subject": "Long newsletter",
        "sender": "newsletter@example.com",
        "date_str": "Mon, 26 May 2026 09:00:00 +0000",
        "body_text": big_plain,
        "html_body": big_html,
        "image_attachments": [],
    }

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.fetch_email_articles", return_value=""
    ), patch(
        "ingest_gmail.insert_content_record", return_value=7
    ) as mock_insert, patch(
        "ingest_gmail._download_images", return_value=[]
    ):
        from ingest_gmail import run_ingest

        run_ingest()

    rec = mock_insert.call_args[0][0]
    # body_text is truncated to the cap; raw parts retain the full original.
    assert len(rec.body_text) <= config.BODY_TEXT_CAP + len("\n\n[truncated]")
    assert rec.raw_text == big_plain
    assert rec.raw_html == big_html
    assert len(rec.raw_text) > len(rec.body_text)


# ---------------------------------------------------------------------------
# Classifier: deterministic rules
# ---------------------------------------------------------------------------


def test_inmail_classified_as_professional_without_llm(mock_gmail):
    """InMail subject must map to Professional deterministically, no LLM call."""
    mock_gmail.extract_message_parts.return_value = {
        "subject": "InMail from Jane Recruiter",
        "sender": "recruiter@linkedin.com",
        "date_str": "",
        "body_text": "Hi, I'd like to connect about an opportunity.",
        "html_body": "",
        "image_attachments": [],
    }
    mock_llm = MagicMock()

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", return_value=1
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    # InMail → Professional + inmail tag → matches TRASH_EXCLUSIONS → kept, not trashed
    assert results[0].status == "kept"
    mock_llm.call_json.assert_not_called()


def test_excluded_email_is_kept_not_trashed(mock_gmail, mock_llm):
    """Thread matching a TRASH_EXCLUSIONS rule must be labeled but not trashed."""
    mock_llm.call_json.return_value = {
        "summary": "LinkedIn InMail from a recruiter.",
        "summary_confidence": 4,
        "summary_rationale": "Recruiter outreach; intent clear.",
        "tags": ["inmail", "recruiter"],
        "labels": ["Professional"],
        "classification_confidence": 4,
        "classification_rationale": "InMail in subject; recruiter purpose signal. Professional only.",
    }

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", return_value=5
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "kept"
    assert results[0].record_id == 5
    mock_gmail.apply_labels.assert_called_once()  # labels applied
    mock_gmail.trash_thread.assert_not_called()   # NOT trashed


def test_security_alert_is_trashed_without_insert(mock_gmail, mock_llm):
    """Security alert subject must trigger immediate trash, no DB insert."""
    mock_gmail.extract_message_parts.return_value = {
        "subject": "Security alert: new sign-in",
        "sender": "no-reply@accounts.google.com",
        "date_str": "",
        "body_text": "Someone signed in from a new device.",
        "html_body": "",
        "image_attachments": [],
    }

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record"
    ) as mock_insert, patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "trashed"
    mock_insert.assert_not_called()
    mock_gmail.trash_thread.assert_called_once()


# ---------------------------------------------------------------------------
# Trash sweep: fetch_ingest_threads deduplicates inbox + trash results
# ---------------------------------------------------------------------------


def test_fetch_ingest_threads_deduplicates_inbox_and_trash():
    """Threads returned by both inbox and trash queries are deduplicated by ID."""
    from unittest.mock import MagicMock, patch

    mock_service = MagicMock()
    # inbox returns thread_a; trash returns thread_b + thread_a (overlap)
    mock_service.users.return_value.threads.return_value.list.return_value.execute.side_effect = [
        {"threads": [{"id": "thread_a", "snippet": "inbox"}]},
        {"threads": [{"id": "thread_b", "snippet": "trash"}, {"id": "thread_a", "snippet": "dup"}]},
    ]

    import gmail_client

    with patch.object(gmail_client, "_build_service", return_value=mock_service):
        client = gmail_client.GmailClient()
        results = client.fetch_ingest_threads()

    ids = {t["id"] for t in results}
    assert ids == {"thread_a", "thread_b"}
    assert mock_service.users.return_value.threads.return_value.list.call_count == 2


# ---------------------------------------------------------------------------
# Reprocess
# ---------------------------------------------------------------------------


def test_reprocess_updates_record_on_success():
    """Successful reprocess calls update_record_classification with new fields."""
    from unittest.mock import MagicMock, patch

    records = [
        {
            "id": 1,
            "subject": "Earnings beat: NVDA",
            "sender": "news@example.com",
            "body_text": "NVDA beat Q1 expectations.",
            "summary": "Stored summary: NVDA beat Q1.",
            "processed_at": None,
            "labels": ["Investment"],
            "tags": ["earning"],
        }
    ]
    # Reprocess re-runs only the classify step (one call_json).
    classify_output = {
        "tags": ["earnings"],
        "labels": ["Business"],
        "classification_confidence": 4,
        "classification_rationale": "Tags cluster in Investment (earnings). No purpose signals.",
    }
    mock_llm = MagicMock()
    mock_llm.call_json.return_value = classify_output
    mock_llm.consume_usage.return_value = (None, None)

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=["Investment"], filter_tags=None, since=None, days=None)

    assert results[0].status == "updated"
    # Only one LLM call (classify); it works off the stored summary, not body_text.
    mock_llm.call_json.assert_called_once()
    classify_prompt = mock_llm.call_json.call_args[0][1]
    assert "Stored summary: NVDA beat Q1." in classify_prompt
    assert "NVDA beat Q1 expectations." not in classify_prompt  # body_text not re-sent
    mock_update.assert_called_once_with(
        1, ["Business"], ["earnings"],
        4, "Tags cluster in Investment (earnings). No purpose signals.",
        None, None, False,
    )


def test_reprocess_skips_record_with_no_body():
    """Records with no body_text produce status=skipped, not failed."""
    from unittest.mock import MagicMock, patch

    records = [{"id": 2, "subject": "Empty", "sender": "x@x.com", "body_text": None, "processed_at": None, "labels": [], "tags": []}]
    mock_llm = MagicMock()

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()


def test_reprocess_skips_record_with_no_summary():
    """Reprocess re-runs only classify; a record with body but no stored summary is skipped."""
    from unittest.mock import MagicMock, patch

    records = [{"id": 8, "subject": "No summary", "sender": "x@x.com", "body_text": "Some body.", "summary": "", "processed_at": None, "labels": [], "tags": []}]
    mock_llm = MagicMock()

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()


def test_reprocess_skips_record_missing_a_classify_input_field():
    """A record missing data in any classify input field (here: blank sender) is skipped."""
    from unittest.mock import MagicMock, patch

    records = [{"id": 9, "subject": "Has subject", "sender": "   ", "body_text": "Some body.", "summary": "A summary.", "processed_at": None, "labels": [], "tags": []}]
    mock_llm = MagicMock()

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    assert "sender" in (results[0].error or "")
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()


def test_reprocess_skips_excluded_record():
    """Records matching a TRASH_EXCLUSIONS rule must be skipped by reprocess."""
    from unittest.mock import MagicMock, patch

    records = [
        {
            "id": 10,
            "subject": "InMail from Recruiter",
            "sender": "recruiter@linkedin.com",
            "body_text": "Hi, I'd like to connect.",
            "processed_at": None,
            "labels": ["Professional"],
            "tags": ["inmail"],
        }
    ]
    mock_llm = MagicMock()

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()


def test_reprocess_continues_after_llm_failure():
    """LLM failure on one record does not abort remaining records."""
    from unittest.mock import MagicMock, patch

    records = [
        {"id": 3, "subject": "A", "sender": "a@a.com", "body_text": "body a", "summary": "sum a", "processed_at": None, "labels": [], "tags": []},
        {"id": 4, "subject": "B", "sender": "b@b.com", "body_text": "body b", "summary": "sum b", "processed_at": None, "labels": [], "tags": []},
    ]
    good_output = {
        "tags": ["macro"],
        "labels": ["Business"],
        "classification_confidence": 4,
        "classification_rationale": "Tags cluster in Investment (macro). Clear.",
    }
    mock_llm = MagicMock()
    mock_llm.consume_usage.return_value = (None, None)

    def call_json_side_effect(operation, prompt, system="", **kwargs):
        # Record A fails at the classify step (its prompt carries "Subject: A").
        if "Subject: A" in prompt:
            raise RuntimeError("LLM timeout")
        return good_output

    mock_llm.call_json.side_effect = call_json_side_effect

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "failed"
    assert results[1].status == "updated"
    mock_update.assert_called_once_with(
        4, ["Business"], ["macro"],
        4, "Tags cluster in Investment (macro). Clear.",
        None, None, False,
    )


# ---------------------------------------------------------------------------
# Article scraper
# ---------------------------------------------------------------------------


def test_should_skip_unsubscribe_url():
    """Unsubscribe and preference URLs must be skipped."""
    from article_scraper import should_skip

    assert should_skip("https://example.com/unsubscribe?token=abc", "") is True
    assert should_skip("https://news.example.com/preferences", "") is True
    assert should_skip("https://example.com/legal/privacy-policy", "") is True
    assert should_skip("https://example.com/article/fed-rates-2026", "") is False


def test_should_skip_management_anchor_text():
    """Links with management anchor text must be skipped even if URL looks clean."""
    from article_scraper import should_skip

    assert should_skip("https://example.com/p/abc123", "Manage preferences") is True
    assert should_skip("https://example.com/p/abc123", "Unsubscribe") is True
    assert should_skip("https://example.com/p/abc123", "Read the full article") is False


def test_extract_article_links_scores_and_filters():
    """Article links are extracted, skip links are filtered, scores applied."""
    from article_scraper import extract_article_links

    html = """
    <a href="https://news.example.com/article/nvda-earnings">Read the full article</a>
    <a href="https://example.com/unsubscribe">Unsubscribe</a>
    <a href="https://blog.example.com/analysis">AI chip market deep dive — full analysis</a>
    """
    links = extract_article_links(html)
    urls = [l["url"] for l in links]

    assert "https://example.com/unsubscribe" not in urls
    assert "https://news.example.com/article/nvda-earnings" in urls
    assert "https://blog.example.com/analysis" in urls
    # Article-signal anchor scores highest
    assert links[0]["url"] == "https://news.example.com/article/nvda-earnings"


def test_fetch_email_articles_appends_content(mock_gmail, mock_llm):
    """Article content is appended to body_text before classification."""
    from unittest.mock import patch

    mock_gmail.extract_message_parts.return_value = {
        "subject": "Earnings beat: NVDA",
        "sender": "alerts@example.com",
        "date_str": "",
        "body_text": "Short teaser.",
        "html_body": '<a href="https://example.com/article">Read the full article</a>',
        "image_attachments": [],
    }

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", return_value=1
    ), patch("ingest_gmail._download_images", return_value=[]), patch(
        "ingest_gmail.fetch_email_articles", return_value="Full article content here."
    ) as mock_scrape:
        from ingest_gmail import run_ingest

        results = run_ingest()

    mock_scrape.assert_called_once()
    # Summarize step (first LLM call) gets the full body with article content appended
    summarize_prompt = mock_llm.call_json.call_args_list[0][0][1]
    assert "Full article content here." in summarize_prompt


# ---------------------------------------------------------------------------
# Body truncation
# ---------------------------------------------------------------------------


def test_resolve_labels_maps_canonical():
    """LLM output must map to canonical BUCKET_LABELS entries case-insensitively."""
    from unittest.mock import patch
    from utils import resolve_labels

    with patch("utils.config") as mock_cfg:
        mock_cfg.BUCKET_LABELS = ["Investment", "AI", "Technology", "Miscellaneous"]
        assert resolve_labels(["investment", "ai"]) == ["Investment", "AI"]
        assert resolve_labels(["TECHNOLOGY"]) == ["Technology"]
        assert resolve_labels([]) == ["Miscellaneous"]
        assert resolve_labels(["Unknown"]) == ["Miscellaneous"]
        # Deduplication
        assert resolve_labels(["AI", "ai"]) == ["AI"]


def test_body_truncated_at_cap():
    """Body text longer than BODY_TEXT_CAP must be truncated with marker."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import importlib
    import utils

    importlib.reload(utils)
    import config

    long_text = "x" * (config.BODY_TEXT_CAP + 100)
    result = utils.truncate_body(long_text)
    assert len(result) <= config.BODY_TEXT_CAP + len("\n\n[truncated]")
    assert result.endswith("[truncated]")


# ---------------------------------------------------------------------------
# Confidence parsing
# ---------------------------------------------------------------------------


def test_parse_confidence_valid_int():
    from utils import parse_confidence

    assert parse_confidence(3, "test") == 3
    assert parse_confidence(1, "test") == 1
    assert parse_confidence(5, "test") == 5


def test_parse_confidence_numeric_string():
    from utils import parse_confidence

    assert parse_confidence("4", "test") == 4


def test_parse_confidence_missing_returns_null(caplog):
    import logging
    from utils import parse_confidence

    with caplog.at_level(logging.WARNING):
        result = parse_confidence(None, "src_abc")
    assert result is None
    assert "src_abc" in caplog.text


def test_parse_confidence_out_of_range_returns_null(caplog):
    import logging
    from utils import parse_confidence

    with caplog.at_level(logging.WARNING):
        result = parse_confidence(9, "src_abc")
    assert result is None
    assert "src_abc" in caplog.text


def test_parse_confidence_non_numeric_returns_null(caplog):
    import logging
    from utils import parse_confidence

    with caplog.at_level(logging.WARNING):
        result = parse_confidence("high", "src_abc")
    assert result is None
    assert "src_abc" in caplog.text


# ---------------------------------------------------------------------------
# Confidence escalation
# ---------------------------------------------------------------------------


def test_high_confidence_no_escalation():
    """Confidence above threshold must not trigger a second LLM call."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    llm = MagicMock()
    llm.call_json.return_value = {
        "tags": ["macro"], "labels": ["Investment"],
        "classification_confidence": 4, "classification_rationale": "Clear.",
    }
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is False
    assert confidence == 4
    llm.call_json.assert_called_once()


def test_low_confidence_triggers_escalation():
    """Confidence at or below threshold must trigger re-call with escalation model."""
    from unittest.mock import MagicMock, call, patch
    from utils import classify_with_escalation

    weak = {"tags": [], "labels": ["Miscellaneous"], "classification_confidence": 1, "classification_rationale": "Unclear."}
    strong = {"tags": ["macro"], "labels": ["Investment"], "classification_confidence": 5, "classification_rationale": "Clear."}

    llm = MagicMock()
    llm.call_json.side_effect = [weak, strong]
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is True
    assert confidence == 5
    assert result["labels"] == ["Investment"]
    assert llm.call_json.call_count == 2
    # Second call must use model_override
    second_call_kwargs = llm.call_json.call_args_list[1][1]
    assert second_call_kwargs.get("model_override") == "sonnet"


def test_null_confidence_no_confidence_escalation():
    """Missing confidence must NOT trigger confidence-based escalation (non-Misc label)."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    llm = MagicMock()
    # Non-Misc label so only the confidence gate is relevant; no classification_confidence key
    llm.call_json.return_value = {"tags": ["macro"], "labels": ["Investment"], "classification_rationale": "Clear."}
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is False
    assert confidence is None
    llm.call_json.assert_called_once()


def test_escalation_disabled_by_threshold_zero():
    """Threshold of 0 must disable confidence-based escalation."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    llm = MagicMock()
    # Low confidence but non-Misc label — only the threshold gate applies here
    llm.call_json.return_value = {
        "tags": ["macro"], "labels": ["Investment"],
        "classification_confidence": 1, "classification_rationale": "Clear.",
    }
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 0
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is False
    llm.call_json.assert_called_once()


def test_miscellaneous_label_triggers_escalation_regardless_of_confidence():
    """Miscellaneous label must escalate to sonnet even when confidence is above threshold."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    misc = {"tags": ["uncategorized"], "labels": ["Miscellaneous"], "classification_confidence": 4, "classification_rationale": "No cluster."}
    better = {"tags": ["macro"], "labels": ["Investment"], "classification_confidence": 4, "classification_rationale": "Investment cluster."}

    llm = MagicMock()
    llm.call_json.side_effect = [misc, better]
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2  # confidence 4 would NOT trigger alone
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is True
    assert result["labels"] == ["Investment"]
    assert llm.call_json.call_count == 2
    second_kwargs = llm.call_json.call_args_list[1][1]
    assert second_kwargs.get("model_override") == "sonnet"


def test_missed_actionable_triggers_escalation():
    """Business label + high-precision trade tag without `actionable` must escalate."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    missed = {
        "tags": ["trade_alert", "equity"], "labels": ["Business"],
        "classification_confidence": 5, "classification_rationale": "No directed action.",
    }
    caught = {
        "tags": ["actionable", "trade_alert", "equity"], "labels": ["Business"],
        "classification_confidence": 5, "classification_rationale": "Actionable: applied.",
    }

    llm = MagicMock()
    llm.call_json.side_effect = [missed, caught]
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2  # confidence 5 would NOT trigger alone
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        mock_cfg.CLASSIFY_ACTIONABLE_REVIEW_TAGS = frozenset({
            "trade_alert", "options", "insider_activity", "merger_arb", "arbitrage",
            "special_situations", "rights_offering", "spinoff",
        })
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is True
    assert "actionable" in result["tags"]
    assert llm.call_json.call_count == 2
    second_kwargs = llm.call_json.call_args_list[1][1]
    assert second_kwargs.get("model_override") == "sonnet"


def test_actionable_present_does_not_trigger_missed_actionable_escalation():
    """`actionable` already applied must not double-trigger the missed-actionable check."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    llm = MagicMock()
    llm.call_json.return_value = {
        "tags": ["actionable", "trade_alert", "equity"], "labels": ["Business"],
        "classification_confidence": 5, "classification_rationale": "Actionable: applied.",
    }
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        mock_cfg.CLASSIFY_ACTIONABLE_REVIEW_TAGS = frozenset({
            "trade_alert", "options", "insider_activity", "merger_arb", "arbitrage",
            "special_situations", "rights_offering", "spinoff",
        })
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is False
    llm.call_json.assert_called_once()


def test_non_business_label_does_not_trigger_missed_actionable_escalation():
    """Trade-signal tags on a non-Business label must not trigger the missed-actionable check."""
    from unittest.mock import MagicMock, patch
    from utils import classify_with_escalation

    llm = MagicMock()
    llm.call_json.return_value = {
        "tags": ["trade_alert"], "labels": ["Technology"],
        "classification_confidence": 5, "classification_rationale": "Clear.",
    }
    llm.consume_usage.return_value = (None, None)

    with patch("utils.config") as mock_cfg:
        mock_cfg.CLASSIFY_ESCALATION_THRESHOLD = 2
        mock_cfg.CLASSIFY_ESCALATION_MODEL = "sonnet"
        mock_cfg.CLASSIFY_ACTIONABLE_REVIEW_TAGS = frozenset({
            "trade_alert", "options", "insider_activity", "merger_arb", "arbitrage",
            "special_situations", "rights_offering", "spinoff",
        })
        result, confidence, escalated, classify_model, classify_tokens = classify_with_escalation(llm, "sys", "user", "ctx")

    assert escalated is False
    llm.call_json.assert_called_once()


def test_confidence_and_rationale_stored_on_ingest(mock_gmail, mock_llm):
    """Summarize + classify confidence/rationale and escalation metadata must reach insert_content_record."""
    from unittest.mock import patch

    inserted_records = []

    def capture_insert(rec):
        inserted_records.append(rec)
        return 99

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", side_effect=capture_insert
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "ingested"
    rec = inserted_records[0]
    assert rec.summary == "NVDA beat Q1 earnings expectations."
    assert rec.executive_summary == "NVDA topped Q1 estimates."
    assert rec.summary_confidence == 4
    assert rec.summary_rationale == "Clear earnings beat; key figures captured."
    assert rec.classification_confidence == 4
    assert rec.classification_rationale == "Tags cluster in Investment (earnings, semiconductor). No purpose signals."
    assert rec.escalated is False
    assert rec.classify_model is None   # mock returns None from consume_usage
    assert rec.classify_tokens is None


def test_missing_confidence_stored_as_none_on_ingest(mock_gmail):
    """Missing confidence/rationale in LLM output must result in None on the record."""
    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {
        "summary": "Summary.", "tags": ["earnings"], "labels": ["Investment"]
        # no summary_/classification_ confidence or rationale keys
    }
    mock_llm.consume_usage.return_value = (None, None)
    inserted_records = []

    def capture_insert(rec):
        inserted_records.append(rec)
        return 99

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", side_effect=capture_insert
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "ingested"
    rec = inserted_records[0]
    assert rec.executive_summary is None
    assert rec.summary_confidence is None
    assert rec.summary_rationale is None
    assert rec.classification_confidence is None
    assert rec.classification_rationale is None
    assert rec.escalated is False


def test_rationale_truncated_before_storage(mock_gmail):
    """Both rationale fields cap at 1000 chars before storage."""
    mock_llm = MagicMock()
    mock_llm.call_json.return_value = {
        "summary": "Summary.",
        "summary_confidence": 4,
        "summary_rationale": "y" * 1200,
        "tags": ["earnings"],
        "labels": ["Investment"],
        "classification_confidence": 4,
        "classification_rationale": "x" * 1200,
    }
    mock_llm.consume_usage.return_value = (None, None)
    inserted_records = []

    def capture_insert(rec):
        inserted_records.append(rec)
        return 99

    with patch("ingest_gmail.GmailClient", return_value=mock_gmail), patch(
        "ingest_gmail.LLMClient", return_value=mock_llm
    ), patch("ingest_gmail.record_exists", return_value=False), patch(
        "ingest_gmail.insert_content_record", side_effect=capture_insert
    ), patch("ingest_gmail._download_images", return_value=[]):
        from ingest_gmail import run_ingest

        results = run_ingest()

    assert results[0].status == "ingested"
    assert len(inserted_records[0].classification_rationale) == 1000
    assert len(inserted_records[0].summary_rationale) == 1000


def test_low_confidence_escalation_on_reprocess():
    """Reprocess must escalate to sonnet when initial confidence is low."""
    from unittest.mock import MagicMock, patch

    records = [
        {
            "id": 7,
            "subject": "Unclear topic",
            "sender": "x@x.com",
            "body_text": "Ambiguous content.",
            "summary": "Stored summary of an unclear item.",
            "processed_at": None,
            "labels": ["Miscellaneous"],
            "tags": [],
        }
    ]
    # Call sequence: 1) classify (weak, low conf), 2) escalation (strong). No summarize.
    weak = {"tags": [], "labels": ["Miscellaneous"], "classification_confidence": 1, "classification_rationale": "Unclear."}
    strong = {"tags": ["macro"], "labels": ["Business"], "classification_confidence": 4, "classification_rationale": "Clear Investment cluster."}

    mock_llm = MagicMock()
    mock_llm.call_json.side_effect = [weak, strong]
    mock_llm.consume_usage.return_value = (None, None)

    with patch("reprocess.query_records_for_reprocess", return_value=records), patch(
        "reprocess.update_record_classification"
    ) as mock_update, patch("reprocess.LLMClient", return_value=mock_llm):
        from reprocess import run_reprocess

        results = run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "updated"
    assert mock_llm.call_json.call_count == 2
    # Classification from the escalated (sonnet) call; summary fields untouched.
    mock_update.assert_called_once_with(
        7, ["Business"], ["macro"],
        4, "Clear Investment cluster.",
        None, None, True,
    )


# ---------------------------------------------------------------------------
# Reprocess-summary (re-run only the summarize step)
# ---------------------------------------------------------------------------


def test_reprocess_summary_updates_record_on_success():
    """Re-summarize runs only the summarize step and updates summary fields off body_text."""
    from unittest.mock import MagicMock, patch

    records = [
        {
            "id": 1,
            "subject": "Earnings beat: NVDA",
            "sender": "news@example.com",
            "body_text": "NVDA beat Q1 expectations.",
            "summary": "Stale summary.",
            "processed_at": None,
            "labels": ["Investment"],
            "tags": ["earnings"],
        }
    ]
    summarize_output = {
        "summary": "Fresh detailed summary.",
        "executive_summary": "Fresh exec summary.",
        "summary_confidence": 5,
        "summary_rationale": "Source fully covered.",
    }
    mock_llm = MagicMock()
    mock_llm.call_json.return_value = summarize_output
    mock_llm.consume_usage.return_value = (None, None)

    with patch("reprocess_summary.query_records_for_reprocess", return_value=records), patch(
        "reprocess_summary.update_record_summary"
    ) as mock_update, patch("reprocess_summary.LLMClient", return_value=mock_llm):
        from reprocess_summary import run_reprocess_summary

        results = run_reprocess_summary(filter_labels=["Investment"], filter_tags=None, since=None, days=None)

    assert results[0].status == "updated"
    # Only one LLM call (summarize); the full body_text is sent.
    mock_llm.call_json.assert_called_once()
    summarize_prompt = mock_llm.call_json.call_args[0][1]
    assert "NVDA beat Q1 expectations." in summarize_prompt
    mock_update.assert_called_once_with(
        1, "Fresh detailed summary.", "Fresh exec summary.", 5, "Source fully covered."
    )


def test_reprocess_gaps_passes_classify_gap_filter():
    """run_reprocess(gaps=True) asks the query for classify-step gaps only."""
    from unittest.mock import MagicMock, patch

    with patch("reprocess.query_records_for_reprocess", return_value=[]) as mock_query, patch(
        "reprocess.LLMClient", return_value=MagicMock()
    ):
        from reprocess import run_reprocess

        run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None, gaps=True)

    assert mock_query.call_args.kwargs.get("gaps_for") == "classify"


def test_reprocess_no_gaps_passes_no_gap_filter():
    """Without gaps, the query receives gaps_for=None."""
    from unittest.mock import MagicMock, patch

    with patch("reprocess.query_records_for_reprocess", return_value=[]) as mock_query, patch(
        "reprocess.LLMClient", return_value=MagicMock()
    ):
        from reprocess import run_reprocess

        run_reprocess(filter_labels=None, filter_tags=None, since=None, days=None)

    assert mock_query.call_args.kwargs.get("gaps_for") is None


def test_reprocess_summary_gaps_passes_summarize_gap_filter():
    """run_reprocess_summary(gaps=True) asks the query for summarize-step gaps only."""
    from unittest.mock import MagicMock, patch

    with patch("reprocess_summary.query_records_for_reprocess", return_value=[]) as mock_query, patch(
        "reprocess_summary.LLMClient", return_value=MagicMock()
    ):
        from reprocess_summary import run_reprocess_summary

        run_reprocess_summary(filter_labels=None, filter_tags=None, since=None, days=None, gaps=True)

    assert mock_query.call_args.kwargs.get("gaps_for") == "summarize"


def test_reprocess_summary_skips_record_with_no_body():
    """Records with no body_text produce status=skipped, not failed."""
    from unittest.mock import MagicMock, patch

    records = [{"id": 2, "subject": "Empty", "sender": "x@x.com", "body_text": None, "summary": "x", "processed_at": None, "labels": [], "tags": []}]
    mock_llm = MagicMock()

    with patch("reprocess_summary.query_records_for_reprocess", return_value=records), patch(
        "reprocess_summary.update_record_summary"
    ) as mock_update, patch("reprocess_summary.LLMClient", return_value=mock_llm):
        from reprocess_summary import run_reprocess_summary

        results = run_reprocess_summary(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()


def test_reprocess_summary_skips_excluded_record():
    """Records matching a TRASH_EXCLUSIONS rule must be skipped by re-summarize."""
    from unittest.mock import MagicMock, patch

    records = [
        {
            "id": 10,
            "subject": "InMail from Recruiter",
            "sender": "recruiter@linkedin.com",
            "body_text": "Hi, I'd like to connect.",
            "summary": "Recruiter outreach.",
            "processed_at": None,
            "labels": ["Professional"],
            "tags": ["inmail"],
        }
    ]
    mock_llm = MagicMock()

    with patch("reprocess_summary.query_records_for_reprocess", return_value=records), patch(
        "reprocess_summary.update_record_summary"
    ) as mock_update, patch("reprocess_summary.LLMClient", return_value=mock_llm):
        from reprocess_summary import run_reprocess_summary

        results = run_reprocess_summary(filter_labels=None, filter_tags=None, since=None, days=None)

    assert results[0].status == "skipped"
    mock_llm.call_json.assert_not_called()
    mock_update.assert_not_called()
