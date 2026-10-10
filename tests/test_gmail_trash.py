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
