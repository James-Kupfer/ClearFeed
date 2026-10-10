"""Tests for GmailClient reply / message-trash methods."""

import base64
import email
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _client(service: MagicMock):
    import gmail_client

    with patch.object(gmail_client, "_build_service", return_value=service):
        return gmail_client.GmailClient()


def test_send_reply_builds_threaded_plain_text_message():
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.send.return_value.execute.return_value = {"id": "sent-9"}

    sent_id = _client(service).send_reply(
        to="jane@real.com",
        subject="Re: hello",
        body="Sincerely,\nJames",
        thread_id="thr-1",
        in_reply_to="<m2@x>",
        references="<m1@x>",
    )

    assert sent_id == "sent-9"
    body = messages.send.call_args.kwargs["body"]
    assert body["threadId"] == "thr-1"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["To"] == "jane@real.com"
    assert msg["Subject"] == "Re: hello"
    assert msg["In-Reply-To"] == "<m2@x>"
    assert msg["References"] == "<m1@x> <m2@x>"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_payload(decode=True).decode("utf-8") == "Sincerely,\nJames"


def test_send_reply_omits_thread_and_reply_headers_when_absent():
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.send.return_value.execute.return_value = {"id": "s"}

    _client(service).send_reply(to="a@b.com", subject="Re: x", body="hi")

    body = messages.send.call_args.kwargs["body"]
    assert "threadId" not in body
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["In-Reply-To"] is None and msg["References"] is None


def test_send_reply_is_not_retried_on_failure():
    from googleapiclient.errors import HttpError

    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.send.return_value.execute.side_effect = HttpError(MagicMock(status=503), b"x")

    import pytest

    with pytest.raises(HttpError):
        _client(service).send_reply(to="a@b.com", subject="Re: x", body="hi")
    assert messages.send.return_value.execute.call_count == 1


def test_trash_message_trashes_by_message_id():
    service = MagicMock()
    _client(service).trash_message("msg-1")
    service.users.return_value.messages.return_value.trash.assert_called_once_with(
        userId="me", id="msg-1"
    )


def test_get_message_headers_lowercases_names_and_returns_thread_id():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "threadId": "thr-1",
        "payload": {"headers": [{"name": "Reply-To", "value": "a@b.com"}, {"name": "Subject", "value": "S"}]},
    }
    meta = _client(service).get_message_headers("msg-1")
    assert meta == {"thread_id": "thr-1", "headers": {"reply-to": "a@b.com", "subject": "S"}}
