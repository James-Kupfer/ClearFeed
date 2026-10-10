"""Ignore a LinkedIn connection invitation through a logged-in browser profile.

LinkedIn has no public API for this, so ClearFeed opens the invitation's own
"ignore" link (taken from the notification email) in a persistent Playwright
profile that you signed in to once, and optionally clicks a button on the page.

Caveat: LinkedIn's terms prohibit automated access; use can get the account
restricted. The page behind the link has not been verified against a live
account — if clicking does not archive the request, set `click_text` in the
profile's `linkedin_ignore:` block to the button's visible label.

One-time sign-in (opens a visible browser; close it when logged in):
    clearfeed.bat linkedin-login
"""

import argparse
import logging
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ("linkedin.com",)
_LOGIN_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/")


class LinkedInError(Exception):
    """Raised when the invitation could not be ignored."""


def validate_url(url: str, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS) -> str:
    """Return the URL if it is https on an allowed host (or subdomain); else raise.

    The URL is LLM-extracted from an email body, and the browser profile holds a
    live LinkedIn session, so only linkedin.com is ever opened.
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == h or host.endswith("." + h) for h in allowed_hosts
    ):
        raise LinkedInError(f"Refusing to open non-LinkedIn URL: {url!r}")
    return parsed.geturl()


def _launch(p, headless: bool):
    """Open the persistent context (honors PLAYWRIGHT_CHROMIUM_EXECUTABLE if set)."""
    kwargs: dict = {"headless": headless}
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if exe:
        kwargs["executable_path"] = exe
    return p.chromium.launch_persistent_context(str(config.LINKEDIN_BROWSER_PROFILE_DIR), **kwargs)


def _visit(url: str, click_text: str | None, headless: bool, timeout_s: int) -> None:
    """Open `url` in the persistent profile; click `click_text` if given.

    Raises LinkedInError when the session is signed out or the button is absent.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout_s * 1000
    with sync_playwright() as p:
        context = _launch(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_load_state("load")
            except PlaywrightError as exc:
                raise LinkedInError(f"Could not load page: {exc}") from exc

            if any(m in page.url for m in _LOGIN_MARKERS):
                raise LinkedInError(
                    "LinkedIn session expired — run `clearfeed.bat linkedin-login`"
                )

            if click_text:
                target = page.get_by_role("button", name=click_text).or_(
                    page.get_by_role("link", name=click_text)
                ).first
                try:
                    target.click()
                    page.wait_for_load_state("load")
                except PlaywrightError as exc:
                    raise LinkedInError(f"Could not click {click_text!r}: {exc}") from exc
        finally:
            context.close()


def ignore_invitation(
    url: str,
    click_text: str | None = None,
    headless: bool | None = None,
    timeout_s: int | None = None,
) -> None:
    """Validate `url`, then ignore the invitation it points at. Raises LinkedInError."""
    safe_url = validate_url(url)
    _visit(
        safe_url,
        click_text,
        config.LINKEDIN_HEADLESS if headless is None else headless,
        timeout_s or config.LINKEDIN_NAV_TIMEOUT_SECONDS,
    )
    log.info("[linkedin] opened ignore link%s", f" and clicked {click_text!r}" if click_text else "")


def login() -> None:
    """Open a visible browser on the persistent profile so you can sign in."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/login")
        input("Sign in to LinkedIn in the browser window, then press Enter here to save and exit... ")
        context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LinkedIn browser-session helper.")
    parser.add_argument("--login", action="store_true", help="sign in once (visible browser)")
    args = parser.parse_args()
    if args.login:
        login()
    else:
        parser.print_help()
