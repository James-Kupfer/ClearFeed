"""Gmail OAuth token expiration monitoring for ClearFeed.

Checks token expiration and sends email notifications before the token expires.
"""

import json
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import security_config
from gmail_client import send_email

log = logging.getLogger(__name__)


def check_token_expiration() -> dict | None:
    """Check if Gmail token will expire soon and send notification if needed.

    Returns:
        Dict with token info if expiration check was performed, None if token missing/invalid.
        Format: {
            'expires_at': unix_timestamp,
            'expires_datetime': datetime object,
            'days_until_expiration': int,
            'warning_sent': bool,
            'notification_email': str
        }
    """
    token_path = Path(security_config.GMAIL_TOKEN_CACHE)

    log.info("[token_monitor] Checking token expiration...")
    log.info("[token_monitor] Token path: %s (exists: %s)", token_path, token_path.exists())

    if not token_path.exists():
        log.warning("[token_monitor] Token file not found at %s", token_path)
        return None

    try:
        with open(token_path, "r", encoding="utf-8") as f:
            token_data = json.load(f)
    except Exception as exc:
        log.exception("[token_monitor] Failed to read token file")
        return None

    expires_at = token_data.get("expires_at")
    if not expires_at:
        log.warning("[token_monitor] No expires_at field in token file")
        return None

    # Convert Unix timestamp to datetime
    expires_datetime = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    days_until_expiration = (expires_datetime - now).days

    log.info(
        "[token_monitor] Token expires at %s (UTC) — %d days remaining",
        expires_datetime.isoformat(),
        days_until_expiration,
    )

    result = {
        "expires_at": expires_at,
        "expires_datetime": expires_datetime,
        "days_until_expiration": days_until_expiration,
        "warning_sent": False,
        "notification_email": config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL,
    }

    # Check if warning should be sent
    if days_until_expiration <= 0:
        log.error(
            "[token_monitor] ✗ Token has EXPIRED or will expire today! (%d days)",
            days_until_expiration,
        )
        result["warning_sent"] = _send_expiration_notification(
            expires_datetime, days_until_expiration, is_expired=True
        )
    elif days_until_expiration <= config.TOKEN_EXPIRATION_WARNING_DAYS:
        log.warning(
            "[token_monitor] Token will expire in %d day(s) — sending notification",
            days_until_expiration,
        )
        result["warning_sent"] = _send_expiration_notification(
            expires_datetime, days_until_expiration, is_expired=False
        )
    else:
        log.info("[token_monitor] Token is healthy (%d days until expiration)", days_until_expiration)

    return result


def _send_expiration_notification(
    expires_datetime: datetime, days_remaining: int, is_expired: bool = False
) -> bool:
    """Send email notification about token expiration.

    Args:
        expires_datetime: When the token expires
        days_remaining: Days until expiration (negative if already expired)
        is_expired: True if token has already expired

    Returns:
        True if email was sent successfully, False otherwise
    """
    try:
        if is_expired:
            subject = "🚨 URGENT: Gmail OAuth Token EXPIRED - ClearFeed Ingest is Down"
            body_text = f"""Your Gmail OAuth token for ClearFeed has EXPIRED.

Token expired at: {expires_datetime.isoformat()}

⚠️  ClearFeed email ingestion is currently non-functional.

ACTION REQUIRED:
1. Delete the expired token file
2. Restart the ClearFeed ingest service
3. Complete the Gmail authorization in your browser
4. New token will be cached automatically

Token path: {security_config.GMAIL_TOKEN_CACHE}

Once authorized, email ingestion will resume and emails will be properly classified."""
        else:
            subject = f"⏰ Gmail OAuth Token Expiring in {days_remaining} Day(s) - ClearFeed"
            body_text = f"""Your Gmail OAuth token for ClearFeed will expire soon.

Token will expire at: {expires_datetime.isoformat()}
Days remaining: {days_remaining}

To prevent ClearFeed from stopping, you should re-authenticate:
1. Delete the token file: {security_config.GMAIL_TOKEN_CACHE}
2. Restart the ClearFeed ingest service
3. Complete the Gmail authorization when prompted
4. New token will be cached automatically

You can do this anytime before expiration. The sooner you refresh, the sooner the new token will be ready."""

        log.info("[token_monitor] Sending expiration notification to %s", config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL)

        # Send the email using Gmail API
        send_email(
            to=config.TOKEN_EXPIRATION_NOTIFICATION_EMAIL,
            subject=subject,
            html_body=f"<pre>{body_text}</pre>",
            plain_body=body_text,
        )

        log.info("[token_monitor] ✓ Expiration notification sent successfully")
        return True

    except Exception as exc:
        log.exception("[token_monitor] ✗ Failed to send expiration notification")
        return False
