"""Tests for GmailClient.trash_message."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_trash_message_trashes_by_message_id():
    import gmail_client

    service = MagicMock()
    with patch.object(gmail_client, "_build_service", return_value=service):
        gmail_client.GmailClient().trash_message("msg-1")
    service.users.return_value.messages.return_value.trash.assert_called_once_with(
        userId="me", id="msg-1"
    )


def test_list_message_ids_follows_pagination():
    import gmail_client

    service = MagicMock()
    listing = service.users.return_value.messages.return_value.list
    listing.return_value.execute.side_effect = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t2"},
        {"messages": [{"id": "c"}]},
    ]
    with patch.object(gmail_client, "_build_service", return_value=service):
        ids = gmail_client.GmailClient().list_message_ids("from:x")
    assert ids == ["a", "b", "c"]
    assert listing.call_args_list[1].kwargs["pageToken"] == "t2"
