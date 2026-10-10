"""Ignore a LinkedIn connection invitation through a logged-in browser profile.

LinkedIn has no public API for this, so ClearFeed drives a persistent Playwright
profile that you signed in to once: `ignore_invitation` opens the requester's
page (the "view profile" link from the notification email) and clicks the
Ignore button, then checks the button is gone.

Caveat: LinkedIn's terms prohibit automated access; use can get the account
restricted. Verified by hand against a real invitation page (the button is
labelled "Ignore <name>'s request to connect"), but the automated click has only
been exercised against a local stand-in page.

One-time sign-in (opens a visible browser; close it when logged in):
    clearfeed.bat linkedin-login
List a page's buttons and boxes step by step (types and clicks nothing):
    clearfeed.bat linkedin-inspect
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
    from playwright.sync_api import expect, sync_playwright

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
                # Confirm: the button should be gone once the request is ignored.
                try:
                    expect(target).to_be_hidden()
                except AssertionError as exc:
                    raise LinkedInError(
                        f"Clicked {click_text!r} but it is still on the page — not confirmed"
                    ) from exc
        finally:
            context.close()


_DUMP_JS = """
() => {
  const sel = 'button, [role=button], [role=menuitem], [role=option], [role=textbox], ' +
    '[contenteditable=true], textarea, input, a[href*="messaging"]';
  const counts = new Map();
  const visit = (root) => {
    for (const el of root.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;            // skip invisible
      let label = (el.getAttribute('aria-label') || el.innerText || el.placeholder ||
                   el.getAttribute('name') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
      const role = el.getAttribute('role') || el.tagName.toLowerCase() +
                   (el.tagName === 'INPUT' ? '[' + (el.type || 'text') + ']' : '');
      if (el.tagName === 'A') label += ' -> ' + (el.getAttribute('href') || '').split('?')[0].slice(0, 60);
      if (!label && role === 'div') continue;
      const row = (root === document ? '' : '[shadow] ') + role + ' | ' + label;
      counts.set(row, (counts.get(row) || 0) + 1);
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  };
  visit(document);
  return [...counts].map(([row, n]) => n > 1 ? row + '   (x' + n + ')' : row);
}
"""


def inspect_page(url: str, out_path=None) -> None:
    """Visible browser: list the page's visible controls, then again after each step
    the user performs by hand. Output also goes to `out_path`. Nothing is clicked or typed."""
    from playwright.sync_api import sync_playwright

    lines: list[str] = []

    def emit(text: str) -> None:
        enc = sys.stdout.encoding or "utf-8"  # Windows consoles can't print every character
        print(text.encode(enc, "replace").decode(enc))
        lines.append(text)

    def dump(page, title: str) -> None:
        emit(f"\n=== {title} === url: {page.url.split('?')[0]}")
        for row in page.evaluate(_DUMP_JS):
            emit("  " + row)

    with sync_playwright() as p:
        context = _launch(p, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("load")
            page.wait_for_timeout(3000)  # let LinkedIn's client-side render finish
            dump(page, "initial page")
            step = 0
            while True:
                answer = input(
                    "\nIn the browser do the NEXT step toward replying (do not send), then press "
                    "Enter to list the page. Type q + Enter when the message box is showing... "
                )
                if answer.strip().lower() == "q":
                    break
                step += 1
                page.wait_for_timeout(2000)
                dump(page, f"after step {step}")
        finally:
            context.close()
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nSaved to {out_path}")


def ignore_invitation(
    url: str,
    click_text: str | None = None,
    headless: bool | None = None,
    timeout_s: int | None = None,
) -> None:
    """Validate `url`, open it, and click `click_text` to ignore the invitation. Raises LinkedInError."""
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
    parser.add_argument(
        "--inspect",
        nargs="?",
        const="",
        metavar="URL",
        help="list the page's buttons/boxes before and after each step you do by hand "
        "(prompts for the link if omitted); clicks and types nothing",
    )
    args = parser.parse_args()
    if args.login:
        login()
    elif args.inspect is not None:
        inspect_url = args.inspect.strip() or input("Paste the invitation link, then press Enter: ").strip()
        inspect_page(validate_url(inspect_url), config.LOG_DIR / "linkedin_inspect.txt")
    else:
        parser.print_help()
