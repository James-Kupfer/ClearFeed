"""Article scraper for ClearFeed.

Fetches linked article content from email HTML bodies, filtering out
unsubscribe, preferences, and legal links. Fetched content is appended
to body_text before LLM classification so the model sees the full article,
not just the email teaser.
"""

import logging
import re

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger(__name__)

# Substrings that indicate a management / legal / app-store URL — skip these
_SKIP_URL_TERMS = [
    "unsubscribe", "optout", "opt-out", "opt_out",
    "preferences", "manage-sub", "manage_sub", "email-pref",
    "privacy-policy", "privacy_policy",
    "terms-of-service", "terms-of-use", "terms-and-condition",
    "/legal", "/disclaimer", "/cookie",
    "view-in-browser", "view_in_browser", "viewinbrowser",
    "forward-to-friend", "remove-me", "email-settings",
    "apps.apple.com", "play.google.com/store",  # app store links
    "app.adjust.com", "app.link",               # app tracking / deep links
]

# Anchor text phrases that indicate management, legal, or promotional CTA links.
# Checked as substrings of the lowercased anchor text.
_SKIP_ANCHOR_PHRASES = [
    # Management / legal
    "unsubscribe", "opt out", "opt-out", "manage preference",
    "update preference", "email preference", "manage subscription",
    "privacy policy", "terms of service", "terms of use",
    "legal", "disclaimer", "view in browser", "view email",
    "forward to a friend", "contact us", "about us",
    # Promotional / app CTAs
    "claim my", "claim your",
    "get the app", "download the app", "get app",
    "start your substack", "start a substack", "start substack",
    "upgrade to paid", "upgrade to pro", "upgrade now", "upgrade your",
    "subscribe now", "subscribe today", "subscribe for free",
    "sign up", "sign-up",
    "join now", "join free", "join today",
    "try for free", "try free", "try it free",
    "get started", "get access",
    "become a member", "become a paid",
    "support this newsletter", "support my work",
    "sponsor", "advertise",
    "share this", "tweet this", "post this",
]

# Anchor text that signals likely article content
_ARTICLE_ANCHOR_RE = re.compile(
    r"\b(read|view|full|article|story|post|more|continue|open|details)\b",
    re.IGNORECASE,
)

_MIN_ARTICLE_CHARS = 300  # skip responses shorter than this (login/error pages)


def should_skip(url: str, anchor_text: str) -> bool:
    """Return True if this link should not be scraped."""
    url_l = url.lower()
    anchor_l = anchor_text.lower().strip()
    if any(t in url_l for t in _SKIP_URL_TERMS):
        return True
    if any(t in anchor_l for t in _SKIP_ANCHOR_PHRASES):
        return True
    return False


def extract_article_links(html: str) -> list[dict]:
    """Extract scoreable article links from HTML, highest-scored first.

    Scores: 3 = article-signal anchor text, 2 = long anchor text (likely a
    title), 1 = any other http link that passes the skip filter.
    Deduplicates by URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, dict] = {}

    for tag in soup.find_all("a", href=True):
        url = tag["href"].strip()
        if not url.startswith("http"):
            continue
        anchor = tag.get_text(separator=" ", strip=True)
        if should_skip(url, anchor):
            continue

        score = 1
        if _ARTICLE_ANCHOR_RE.search(anchor):
            score = 3
        elif len(anchor) > 25:
            score = 2

        if url not in seen or score > seen[url]["score"]:
            seen[url] = {"url": url, "anchor": anchor, "score": score}

    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


def fetch_article(url: str) -> str | None:
    """Fetch a URL and return its content as plain text, or None on failure."""
    try:
        resp = requests.get(
            url,
            timeout=config.ARTICLE_SCRAPE_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClearFeed/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()

        if "html" not in resp.headers.get("content-type", ""):
            return None

        text = _html_to_text(resp.text)
        if len(text) < _MIN_ARTICLE_CHARS:
            return None

        return text

    except Exception as exc:
        log.debug("Article fetch failed (%s): %s", url, exc)
        return None


def fetch_email_articles(html_body: str) -> str:
    """Fetch up to ARTICLE_SCRAPE_MAX_LINKS articles from an email's HTML body.

    Returns combined plain-text article content, or empty string if none
    could be fetched. Each article is prefixed with its anchor label.
    """
    if not html_body:
        return ""

    links = extract_article_links(html_body)[: config.ARTICLE_SCRAPE_MAX_LINKS]
    if not links:
        return ""

    parts: list[str] = []
    idx = 1
    for link in links:
        text = fetch_article(link["url"])
        if text:
            label = link["anchor"] or link["url"]
            capped = text[: config.ARTICLE_SCRAPE_MAX_CHARS]
            parts.append(
                f"<linked_content_{idx}>\n"
                f"Source: {label}\n"
                f"{capped}\n"
                f"</linked_content_{idx}>"
            )
            log.debug("Scraped %d chars from %s", len(text), link["url"])
            idx += 1

    return "\n\n".join(parts)


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text via html2text, falling back to tag stripping."""
    try:
        import html2text as h2t
        h = h2t.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        return h.handle(html)
    except ImportError:
        return re.sub(r"<[^>]+>", " ", html)
