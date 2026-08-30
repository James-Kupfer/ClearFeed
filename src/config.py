"""Global configuration for ClearFeed.

All environment-tunable settings live here. Imported by other modules;
never the reverse. Secrets and PII live in security_config.py at project
root — never here.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # src/config.py -> project root

# --- Paths ---
INTAKE_DIR = Path(r"C:\Documents\_intake")  # file-intake watch folder (v2)
PROCESSED_DIR = INTAKE_DIR / "processed"
IMAGE_DIR = BASE_DIR / "images"  # permanent image store; never purged
PROFILES_DIR = BASE_DIR / "profiles"  # one self-contained .yaml per profile (digest/action/export)
PROMPTS_DIR = BASE_DIR / "prompts"  # ingest-stage prompts only (summarize.md, classify.md)
LOG_DIR = BASE_DIR / "logs"

# --- Database (PostgreSQL, local) ---
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "clearfeed"
DB_USER = "clearfeed"
DB_TIMEOUT_SECONDS = 30
# DB_PASSWORD is a secret and lives in security_config.py (from Secrets/db_keys.py).

# --- Delivery ---
DEFAULT_RECIPIENT = "summary@kupfer.me"
SMTP_SENDER = "james.kupfer@gmail.com"  # From address shown on outbound digest emails

# --- Digest PDF attachment ---
# Each digest is also attached as a PDF so its in-document links work on mobile
# mail apps that ignore email-body anchors (e.g. ProtonMail on Android — opening
# the PDF renders the links). The page is deliberately small (~52% of US Letter)
# so it reads large fit-to-width on a phone (~+35% vs. the original test), with
# slim left/right margins. Tune the four numbers below to resize.
#
# FI_OWN_PAGE starts every Further Information entry on a fresh page. Phone PDF
# viewers navigate page-by-page and largely ignore a destination's y-coordinate,
# so without it a "Further detail" link leaves its target near the bottom of the
# screen. Tested on-device: own-page lands the entry at the top; a single tall
# page (tried and abandoned) disables the links entirely, because every
# destination is then on the page already being viewed.
DIGEST_PDF_ENABLED = True
DIGEST_PDF_FI_OWN_PAGE = True
DIGEST_PDF_PAGE_WIDTH_IN = 4.41
DIGEST_PDF_PAGE_HEIGHT_IN = 28.50   # 5x the original 5.70in — long scroll, few breaks
DIGEST_PDF_MARGIN_TB_IN = 0.40   # top/bottom margin
DIGEST_PDF_MARGIN_LR_IN = 0.20   # left/right margin

# --- Gmail ingest ---
EMAIL_MAX_THREADS = 100             # cap per query per ingest run
EMAIL_INGEST_LOOKBACK_DAYS = 90   # inbox lookback window — emails older than this are ignored
EMAIL_TRASH_LOOKBACK_DAYS = 30    # how far back to sweep threads in Trash
INGEST_POLL_INTERVAL_MINUTES = 15 # polling interval used by run_ingestion.bat
INGEST_DRY_RUN_LIMIT = 10         # default thread cap for --dry-run mode
PROCESSED_LABEL = "ProcessedClearFeed"  # marks a thread as fully ingested

# --- Gmail OAuth token health monitoring ---
TOKEN_EXPIRATION_NOTIFICATION_EMAIL = "clearfeed@kupfer.me"  # where to send re-auth alerts
# Bound on the interactive browser consent flow (_get_credentials). Without this,
# a scheduled/hidden run that hits an invalid refresh token blocks forever waiting
# for a browser redirect nobody is present to complete, silently killing the
# ingest polling loop until someone notices and manually restarts it.
GMAIL_OAUTH_TIMEOUT_SECONDS = 120
BUCKET_LABELS = [  # Gmail labels auto-created at ingest startup. "Spam" is excluded — it's a reserved Gmail name;
                   # LLM Spam classifications are handled as a direct trash (no label, no DB write).
    "Business",
    "Technology",
    "Science",
    "Health",
    "Professional",
    "Personal",
    "Politics",
    "Culture",
    "Miscellaneous",
]

# --- LLM ---
MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}
LLM_ROUTING = {
    "summarize": "haiku",  # body_text -> summary + summary_confidence/rationale at ingest
    "classify": "haiku",  # labels + tags + classification_confidence/rationale (works off the summary)
    "digest": "haiku",  # short-window digest compose
    "synthesis": "sonnet",  # cross-period synthesis compose
}
# Max output tokens. Haiku 4.5 and Sonnet 4.6 both support up to 64,000 output
# tokens. Calls stream (see llm_client), so large values don't risk HTTP timeouts.
LLM_MAX_TOKENS = 16000      # default for classify/summarize (small/medium outputs)
DIGEST_MAX_TOKENS = 32000   # digest/synthesis compose — long HTML bodies with many items
# Auto-escalation: when the primary classify call returns classification_confidence at or
# below CLASSIFY_ESCALATION_THRESHOLD, re-run classify on the same input with
# CLASSIFY_ESCALATION_MODEL and use that result. Set THRESHOLD to 0 to disable.
# The summarize step is Haiku-only and never escalates.
CLASSIFY_ESCALATION_THRESHOLD = 2
CLASSIFY_ESCALATION_MODEL = "sonnet"
LLM_RETRY_ATTEMPTS = 3
LLM_RETRY_BASE_DELAY = 2.0  # seconds; delay before retry N = base * 2**N
LLM_TIMEOUT_SECONDS = 600  # streaming read timeout; matches SDK default but explicit so a
                            # stalled connection fails+retries instead of blocking silently

# --- Article scraping ---
ARTICLE_SCRAPE_ENABLED = True   # set False to disable entirely
ARTICLE_SCRAPE_MAX_LINKS = 20    # max links to follow per email
ARTICLE_SCRAPE_MAX_CHARS = 15_000  # max chars per article appended to body_text
ARTICLE_SCRAPE_TIMEOUT = 15     # request timeout in seconds

# --- Content limits ---
BODY_TEXT_CAP = 30_000  # chars; truncated with [truncated] marker
INGEST_MAX_RETRIES = 3  # consecutive same-source failures before CRITICAL log

# --- Image filtering (applied at download time) ---
IMAGE_MIN_WIDTH = 200  # px
IMAGE_MIN_HEIGHT = 200  # px
IMAGE_MIN_KB = 5
IMAGE_TRACKER_DOMAINS = {  # known open-pixel/beacon hosts; skip even if large enough
    "list-manage.com",
    "mailchimp.com",
    "sendgrid.net",
    "sg-mail.com",
    "customeriomail.com",
    "mktoresp.com",
    "mailgun.org",
    "click.email",
    "clicks.aweber.com",
    "links.beehiiv.com",
    "trk.klclick.com",
    "e.salesforce.com",
    "m.exct.net",
    "mkt.com",
}

# --- Trash exclusions ---
# Records matching ANY rule are labeled but NOT trashed after ingestion, and
# are excluded from reprocess runs. Each rule is AND'd: a record must carry
# ALL listed labels AND ALL listed tags for the rule to match.
TRASH_EXCLUSIONS: list[dict] = [
    {"labels": ["Professional"], "tags": ["inmail"]},
]

# --- Todoist ---
TODOIST_BASE_URL = "https://api.todoist.com/api/v1"
TODOIST_TIMEOUT_SECONDS = 15
# LLM operation key used for action-dispatch prompt calls (maps into LLM_ROUTING).
# Defaults to "sonnet" — overridden per-profile via the profile's `model` field.
LLM_ROUTING["action"] = "sonnet"

# --- Logging ---
LOG_RETENTION_DAYS = 90
