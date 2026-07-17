"""Gmail OAuth token health monitoring for ClearFeed.

Verifies the cached Gmail token can still be refreshed and sends an email
notification if it can't.
"""

import logging
import sys
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import security_config
from gmail_client import _SCOPES, send_email

log = logging.getLogger(__name__)


def check_token_expiration() -> dict | None:
    """Verify the cached Gmail token can still be refreshed; notify if not.

    Google does not expose an expiration timestamp for the long-lived refresh
    token itself — the `expiry` field in the cached credentials file is the
    short-lived (~1 hour) access token's expiry, which is unrelated to
    whether the refresh token is still valid. The only reliable signal is
    attempting an actual refresh, so that's what this does.

    Returns:
        Dict with token health info, or None if the check could not be
        performed (e.g. no token file). Format: {
            'healthy': bool,
            'error': str | None,
            'warning_sent': bool,
            'notification_email': str
        }
    """
    token_path = Path(security_config.GMAIL_TOKEN_CACHE)

    log.info("[token_monitor] Checking token health...")
    log.info("[token_monitor] Token path: %s (exists: %s)", token_path, token_path.exists())

    if not token_path.exists():
        log.warning("[token_monitor] Token file not found at %s", token_path)
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    except Exception:
        log.exception("[token_monitor] Failed to load cached credentials")
        return None

    result = {
        "healthy": True,
        "error": None,
        "warning_sent": False,
        "notification_email": config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL,
    }

    if creds.valid:
        log.info("[token_monitor] Token is healthy (access token still valid)")
        return result

    if not creds.refresh_token:
        result["healthy"] = False
        result["error"] = "Cached credentials have no refresh_token"
        log.error("[token_monitor] ✗ %s — re-auth required", result["error"])
        result["warning_sent"] = _send_expiration_notification(result["error"])
        return result

    try:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        log.info("[token_monitor] ✓ Token refreshed successfully — healthy")
        return result
    except Exception as exc:
        result["healthy"] = False
        result["error"] = str(exc)
        log.error("[token_monitor] ✗ Token refresh FAILED: %s", exc)
        result["warning_sent"] = _send_expiration_notification(result["error"])
        return result


def _send_expiration_notification(error: str) -> bool:
    """Send email notification that the Gmail token needs re-authentication.

    Args:
        error: the refresh failure reason, included in the notification body.

    Returns:
        True if the email was sent successfully, False otherwise. Note this
        send itself goes through the same (broken) Gmail credentials, so it
        will typically also fail — the attempt is still made in case the
        access token portion is usable even though the refresh token isn't
        (e.g. it hasn't been used yet this hour).
    """
    try:
        subject = "🚨 URGENT: Gmail OAuth Token Invalid - ClearFeed Ingest is Down"
        body_text = f"""Your Gmail OAuth token for ClearFeed could not be refreshed.

Reason: {error}

⚠️  ClearFeed email ingestion is currently non-functional.

ACTION REQUIRED:
1. Delete the token file
2. Restart the ClearFeed ingest service
3. Complete the Gmail authorization in your browser
4. New token will be cached automatically

Token path: {security_config.GMAIL_TOKEN_CACHE}

Once authorized, email ingestion will resume and emails will be properly classified."""

        log.info(
            "[token_monitor] Sending re-auth notification to %s",
            config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL,
        )

        send_email(
            to=config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL,
            subject=subject,
            html_body=f"<pre>{body_text}</pre>",
            plain_body=body_text,
        )

        log.info("[token_monitor] ✓ Re-auth notification sent successfully")
        return True

    except Exception:
        log.exception("[token_monitor] ✗ Failed to send re-auth notification")
        return False
