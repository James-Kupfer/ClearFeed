"""Gmail API wrapper for ClearFeed.

Handles OAuth bootstrap, label management, thread fetching with body/attachment
extraction, applying labels, trashing threads, and sending digest emails.

Both ingest (read) and dispatch (send) use the same OAuth token — no app
password or SMTP credentials required.

OAuth bootstrap: first run opens a browser for consent; token is cached at
GMAIL_TOKEN_CACHE so subsequent runs are non-interactive.
"""

import base64
import logging
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import security_config

from utils import retry

log = logging.getLogger(__name__)

# Scopes required for ClearFeed operations.
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read, label, trash
    "https://www.googleapis.com/auth/gmail.send",    # send digest emails
]


def _get_credentials() -> Credentials:
    """Load cached OAuth credentials or run the desktop consent flow.

    On first run, opens a browser window. Subsequent runs are non-interactive
    as long as the token cache file exists and the refresh token is valid.
    """
    log.info("[_get_credentials] Starting credential retrieval...")
    token_path = Path(security_config.GMAIL_TOKEN_CACHE)
    secret_path = Path(security_config.GMAIL_OAUTH_CLIENT_SECRET)

    log.info("[_get_credentials] Token path: %s (exists: %s)", token_path, token_path.exists())
    log.info("[_get_credentials] Secret path: %s (exists: %s)", secret_path, secret_path.exists())

    creds: Credentials | None = None
    if token_path.exists():
        log.info("[_get_credentials] Loading cached credentials from %s", token_path)
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
            log.info("[_get_credentials] ✓ Cached credentials loaded")
        except Exception as exc:
            log.exception("[_get_credentials] ✗ Failed to load cached credentials")
            raise

    if not creds or not creds.valid:
        log.info("[_get_credentials] Credentials missing or invalid (creds=%s, valid=%s)",
                 creds is not None, getattr(creds, 'valid', False) if creds else False)
        if creds and creds.expired and creds.refresh_token:
            log.info("[_get_credentials] Refreshing expired token...")
            try:
                creds.refresh(Request())
                log.info("[_get_credentials] ✓ Token refreshed")
            except Exception as exc:
                log.exception("[_get_credentials] ✗ Failed to refresh token")
                raise
        else:
            log.warning("[_get_credentials] No valid token to refresh, will attempt browser consent flow")
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), _SCOPES)
                log.info(
                    "[_get_credentials] Starting browser consent flow (timeout=%ds)...",
                    config.GMAIL_OAUTH_TIMEOUT_SECONDS,
                )
                # timeout_seconds is required here: ClearFeed's scheduled/hidden runs
                # (run_ingestion_service.bat, launch_digest.vbs, launch_action.vbs) have
                # no one present to complete a browser consent. Without a bound, this
                # call blocks forever waiting for the OAuth redirect, which silently
                # stalls the whole ingest polling loop (the batch file's `goto loop`
                # never runs again). Timing out lets it raise instead, so the caller's
                # exception handling logs it and the next scheduled cycle retries.
                creds = flow.run_local_server(
                    port=0, timeout_seconds=config.GMAIL_OAUTH_TIMEOUT_SECONDS
                )
                log.info("[_get_credentials] ✓ Browser consent flow completed")
            except Exception as exc:
                log.exception("[_get_credentials] ✗ Browser consent flow failed")
                raise

        log.info("[_get_credentials] Writing new token to %s", token_path)
        try:
            token_path.write_text(creds.to_json(), encoding="utf-8")
            log.info("[_get_credentials] ✓ Token cached")
        except Exception as exc:
            log.exception("[_get_credentials] ✗ Failed to cache token")
            raise

    log.info("[_get_credentials] ✓ Credentials ready for use")
    return creds


def _build_service():
    """Return an authenticated Gmail API service object."""
    log.info("[_build_service] Building Gmail API service...")
    try:
        creds = _get_credentials()
        log.info("[_build_service] Got credentials, calling build()...")
        service = build("gmail", "v1", credentials=creds)
        log.info("[_build_service] ✓ Gmail API service built successfully")
        return service
    except Exception as exc:
        log.exception("[_build_service] ✗ Failed to build Gmail API service")
        raise


class GmailClient:
    """Gmail API operations for ClearFeed ingest and label management.

    Instantiate once per run; the service object is reused.
    """

    def __init__(self) -> None:
        self._service = _build_service()
        self._label_cache: dict[str, str] = {}  # name -> id

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        """Ensure all label names exist, creating any that are missing.

        Args:
            names: Gmail label names to guarantee.

        Returns:
            Dict mapping label name -> Gmail label ID.
        """
        existing = self._list_all_labels()
        result: dict[str, str] = {}
        for name in names:
            if name in existing:
                result[name] = existing[name]
            else:
                label_id = self._create_label(name)
                result[name] = label_id
                log.info("Created Gmail label: %s (%s)", name, label_id)
        self._label_cache.update(result)
        return result

    @retry(exceptions=(HttpError,))
    def _list_all_labels(self) -> dict[str, str]:
        """Return all labels in the account as {name: id}."""
        response = self._service.users().labels().list(userId="me").execute()
        return {lbl["name"]: lbl["id"] for lbl in response.get("labels", [])}

    @retry(exceptions=(HttpError,))
    def _create_label(self, name: str) -> str:
        """Create a Gmail label and return its ID."""
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        result = self._service.users().labels().create(userId="me", body=body).execute()
        return result["id"]

    # ------------------------------------------------------------------
    # Thread fetching
    # ------------------------------------------------------------------

    def fetch_ingest_threads(
        self, max_results: int = config.EMAIL_MAX_THREADS
    ) -> list[dict]:
        """Fetch threads eligible for ingest: all inbox threads within
        EMAIL_INGEST_LOOKBACK_DAYS, plus labeled trash threads within
        EMAIL_TRASH_LOOKBACK_DAYS. Deduplicates by ID.

        Returns:
            List of thread summary dicts (id, snippet).
        """
        threads_by_id: dict[str, dict] = {}

        # Inbox: all unprocessed emails within the lookback window
        inbox_cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.EMAIL_INGEST_LOOKBACK_DAYS
        )
        inbox_query = (
            f"in:inbox -label:{config.PROCESSED_LABEL} "
            f"after:{inbox_cutoff:%Y/%m/%d}"
        )
        for thread in self._list_threads(inbox_query, max_results):
            threads_by_id[thread["id"]] = thread

        # Trash: labeled threads trashed within the shorter lookback window
        trash_cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.EMAIL_TRASH_LOOKBACK_DAYS
        )
        trash_query = (
            f"in:trash has:userlabels -label:{config.PROCESSED_LABEL} "
            f"after:{trash_cutoff:%Y/%m/%d}"
        )
        for thread in self._list_threads(trash_query, max_results):
            threads_by_id.setdefault(thread["id"], thread)

        return list(threads_by_id.values())

    def _list_threads(self, query: str, max_results: int) -> list[dict]:
        """Execute a threads.list query and return results."""
        try:
            response = (
                self._service.users()
                .threads()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return response.get("threads", [])
        except HttpError as exc:
            log.error("Failed to list threads (query=%r): %s", query, exc)
            raise

    @retry(exceptions=(HttpError,))
    def get_thread_messages(self, thread_id: str) -> list[dict]:
        """Fetch full message data for a thread.

        Returns:
            List of raw message dicts (Gmail API format).
        """
        thread = (
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        return thread.get("messages", [])

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def extract_message_parts(self, messages: list[dict]) -> dict:
        """Extract the first message's headers, plain text body, and attachment info.

        Args:
            messages: raw message list from get_thread_messages().

        Returns:
            Dict with keys: subject, sender, received_at (str), body_text,
            html_body, image_attachments (list of {filename, data, mime_type}).
        """
        if not messages:
            return {}
        msg = messages[0]  # use first (oldest) message in thread
        headers = {
            h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])
        }

        body_parts: list[str] = []
        html_parts: list[str] = []
        image_attachments: list[dict] = []

        _walk_parts(msg["payload"], body_parts, html_parts, image_attachments)
        body_text = "\n".join(body_parts)
        html_body = "\n".join(html_parts)

        # Fallback: decode plain body if no parts walked
        if not body_text and not html_body:
            raw = msg["payload"].get("body", {}).get("data", "")
            body_text = _decode_b64(raw)

        return {
            "subject": headers.get("subject", ""),
            "sender": headers.get("from", ""),
            "date_str": headers.get("date", ""),
            "body_text": body_text,
            "html_body": html_body,
            "image_attachments": image_attachments,
        }

    @retry(exceptions=(HttpError,))
    def get_attachment_data(self, message_id: str, attachment_id: str) -> bytes:
        """Download an attachment's raw bytes."""
        result = (
            self._service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(result["data"])

    # ------------------------------------------------------------------
    # Label / trash operations
    # ------------------------------------------------------------------

    @retry(exceptions=(HttpError,))
    def apply_labels(self, thread_id: str, label_ids: list[str]) -> None:
        """Add label IDs to a thread (does not remove existing labels)."""
        self._service.users().threads().modify(
            userId="me",
            id=thread_id,
            body={"addLabelIds": label_ids},
        ).execute()

    @retry(exceptions=(HttpError,))
    def trash_thread(self, thread_id: str) -> None:
        """Move a thread to Gmail Trash. Recoverable for 30 days; not a permanent delete."""
        self._service.users().threads().trash(userId="me", id=thread_id).execute()


# ------------------------------------------------------------------
# SMTP outbound (dispatch)
# ------------------------------------------------------------------

# Gmail clips (truncates with "View entire message") messages larger than ~102 KB.
# A clipped digest hides its lower half, breaking the in-message anchor links.
_GMAIL_CLIP_BYTES = 102_000


def send_email(
    to: str,
    subject: str,
    html_body: str,
    plain_body: str = "",
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send a digest email via the Gmail API (OAuth — no app password needed).

    Args:
        to: recipient address.
        subject: email subject line.
        html_body: HTML version of the digest.
        plain_body: plain-text fallback. Defaults to a short stub rather than a
            full html2text dump: a full plain copy doubles the message size and,
            combined with a long HTML digest, pushes the total over Gmail's
            ~102 KB clipping threshold — which truncates the bottom of the email
            and breaks the in-message "Further detail" / "Back" anchor links.
        attachments: optional list of (filename, content_bytes, mimetype) files.
            Used to ship the full digest as a self-contained .html or .pdf;
            opening it on mobile renders the in-document links, which some mail
            apps (e.g. ProtonMail on Android) ignore in the email body itself.
    """
    if not plain_body:
        plain_body = (
            f"{subject}\n\n"
            "This digest is formatted as HTML. View it in an HTML-capable mail "
            "client to see the full content and links."
        )

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(plain_body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))

    if attachments:
        msg: MIMEMultipart = MIMEMultipart("mixed")
        msg.attach(alternative)
        for filename, content, mimetype in attachments:
            maintype, _, subtype = mimetype.partition("/")
            part = MIMEBase(maintype, subtype or "octet-stream")
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = alternative

    msg["Subject"] = subject
    msg["From"] = config.SMTP_SENDER
    msg["To"] = to

    # Gmail clips the displayed HTML body (not attachments) past ~102 KB. Measure
    # the body alone so a large PDF attachment doesn't trigger a false warning.
    html_bytes = len(html_body.encode("utf-8"))
    if html_bytes > _GMAIL_CLIP_BYTES:
        log.warning(
            "HTML body is %d bytes (> %d) — Gmail will clip it, hiding the bottom "
            "of the digest and breaking in-message anchor links. The PDF "
            "attachment is unaffected; consider shortening Further Information.",
            html_bytes,
            _GMAIL_CLIP_BYTES,
        )

    raw_bytes = msg.as_bytes()
    raw = base64.urlsafe_b64encode(raw_bytes).decode()
    service = _build_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    log.info("Sent email to %s: %s (%d bytes)", to, subject, len(raw_bytes))


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _walk_parts(
    payload: dict,
    text_acc: list[str],
    html_acc: list[str],
    image_acc: list[dict],
) -> None:
    """Recursively walk MIME parts, collecting text/html bodies and images."""
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if parts:
        for part in parts:
            _walk_parts(part, text_acc, html_acc, image_acc)
        return

    body_data = payload.get("body", {})
    data = body_data.get("data", "")

    if mime_type == "text/plain" and data:
        text_acc.append(_decode_b64(data))
    elif mime_type == "text/html" and data:
        html_acc.append(_decode_b64(data))
    elif mime_type.startswith("image/") and body_data.get("attachmentId"):
        image_acc.append(
            {
                "filename": payload.get("filename", ""),
                "mime_type": mime_type,
                "attachment_id": body_data["attachmentId"],
            }
        )


def _decode_b64(data: str) -> str:
    """Decode a URL-safe base64 Gmail body part."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""
