"""Lightweight, zero-AWS single-tab local Playwright browser helper for edge/desktop hosts.

Designed for Raspberry Pi 5 / laptop usage where spinning up an EC2 crawler fleet
is unnecessary. Enforces strict URL scheme + SSRF host guards by default while
allowing explicit LAN host allowlisting when requested.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

_EXTRACT_LINKS_JS = """() => {
  return Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.getAttribute('href') || a.href || '',
    text: (a.innerText || a.textContent || '').trim()
  }));
}"""

_INTERACTIVE_SUMMARY_JS = """() => {
  const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]').length;
  const inputs = document.querySelectorAll('input, textarea, select').length;
  const links = document.querySelectorAll('a[href]').length;
  const samples = Array.from(document.querySelectorAll('button, a[href], input, select, textarea'))
    .slice(0, 10)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('placeholder') || '').trim().slice(0, 80)
    }));
  return { total: buttons + inputs + links, buttons, inputs, links, samples };
}"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LocalBrowserPolicyError(PermissionError):
    """Raised when a target URL violates scheme or SSRF host policy."""


def _is_internal_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not addr.is_global or addr.is_multicast


def validate_url(
    url: str,
    *,
    allow_lan_hosts: tuple[str, ...] = (),
    resolve_dns: bool = True,
) -> str:
    """Validate URL scheme (http/https only) and block SSRF targets unless allowlisted."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LocalBrowserPolicyError(f"Unsupported URL scheme {parsed.scheme!r}; only http/https allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise LocalBrowserPolicyError("URL must include a hostname")

    # Always block cloud metadata IPs even if LAN hosts are allowlisted.
    if host in ("169.254.169.254", "metadata.google.internal", "fd00:ec2::254"):
        raise LocalBrowserPolicyError(f"Metadata host {host!r} is strictly forbidden")

    allow_set = {h.strip().lower() for h in allow_lan_hosts if h.strip()}
    if host in allow_set:
        return url

    # Check literal IP first.
    try:
        if _is_internal_ip(host):
            raise LocalBrowserPolicyError(f"Internal IP {host!r} is blocked (not in allow_lan_hosts)")
        return url
    except ValueError:
        pass

    if host in ("localhost", "localhost.localdomain"):
        raise LocalBrowserPolicyError("localhost is blocked unless explicitly allowlisted")

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError) as exc:
            raise LocalBrowserPolicyError(f"Could not resolve host {host!r}") from exc
        for info in infos:
            ip_str = info[4][0].split("%")[0]
            if _is_internal_ip(ip_str):
                raise LocalBrowserPolicyError(
                    f"Host {host!r} resolves to internal IP {ip_str!r} (not in allow_lan_hosts)"
                )
    return url


@dataclass
class LocalBrowserSession:
    """Single-tab local browser wrapper with SSRF guard and injectable page backend."""

    headless: bool = True
    allow_lan_hosts: tuple[str, ...] = ()
    resolve_dns: bool = True
    _playwright_ctx: Any = field(default=None, repr=False)
    _browser: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)
    _current_url: str = field(default="", repr=False)
    _action_log: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def _record_action(
        self,
        action: str,
        target: str,
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "seq": len(self._action_log) + 1,
            "action": action,
            "target": target,
            "status": status,
            "timestamp": _utc_now(),
        }
        if extra:
            entry.update(extra)
        self._action_log.append(entry)
        return entry

    def get_action_log(self) -> list[dict[str, Any]]:
        """Return a copy of the sequential action log recorded for this session."""
        return [dict(item) for item in self._action_log]

    def _resolve_current_url(self, page: Any) -> str:
        page_url = getattr(page, "url", None)
        if isinstance(page_url, str) and page_url.strip():
            return page_url.strip()
        return self._current_url

    async def start(self, *, page_override: Any | None = None) -> None:
        if page_override is not None:
            self._page = page_override
            return
        from playwright.async_api import async_playwright

        self._playwright_ctx = await async_playwright().start()
        self._browser = await self._playwright_ctx.chromium.launch(headless=self.headless)
        self._page = await self._browser.new_page()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright_ctx is not None:
            await self._playwright_ctx.stop()
            self._playwright_ctx = None
        self._page = None
        self._record_action("close", self._current_url or "session", "ok")

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("LocalBrowserSession is not started; call await start() first")
        return self._page

    async def goto(self, url: str, *, timeout_ms: int = 15_000) -> dict[str, Any]:
        try:
            safe_url = validate_url(
                url, allow_lan_hosts=self.allow_lan_hosts, resolve_dns=self.resolve_dns
            )
        except LocalBrowserPolicyError:
            self._record_action("goto", url, "blocked")
            raise
        page = self._require_page()
        try:
            await page.goto(safe_url, timeout=timeout_ms)
            title = await page.title()
        except Exception:
            self._record_action("goto", safe_url, "error")
            raise
        self._current_url = safe_url
        self._record_action("goto", safe_url, "ok")
        return {"url": safe_url, "title": title}

    async def click(self, selector: str) -> None:
        page = self._require_page()
        try:
            await page.click(selector)
        except Exception:
            self._record_action("click", selector, "error")
            raise
        self._record_action("click", selector, "ok")

    async def fill(self, selector: str, value: str) -> None:
        page = self._require_page()
        try:
            await page.fill(selector, value)
        except Exception:
            self._record_action("fill", selector, "error")
            raise
        self._record_action("fill", selector, "ok")

    async def fill_input(
        self,
        selector: str,
        value: str,
        *,
        redact_value: bool = False,
    ) -> dict[str, Any]:
        """Fill a form input element and record the action (redacting secrets when requested)."""
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("fill_input requires a non-empty 'selector' string")
        page = self._require_page()
        try:
            await page.fill(selector, value)
        except Exception:
            self._record_action("fill_input", selector, "error", redacted=redact_value)
            raise
        logged_value = "[REDACTED]" if redact_value else value
        self._record_action(
            "fill_input",
            selector,
            "ok",
            redacted=redact_value,
            value=logged_value,
        )
        return {
            "selector": selector,
            "filled": True,
            "redacted": redact_value,
            "value": logged_value,
        }

    async def extract_text(self, selector: str = "body") -> str:
        page = self._require_page()
        try:
            text = str(await page.inner_text(selector))
        except Exception:
            self._record_action("extract_text", selector, "error")
            raise
        self._record_action("extract_text", selector, "ok")
        return text

    async def extract_links(
        self,
        max_links: int = 50,
        *,
        include_blocked: bool = True,
    ) -> list[dict[str, Any]]:
        """Extract links from the active page, classifying each through ``validate_url``."""
        if max_links < 0:
            raise ValueError("max_links must be >= 0")
        page = self._require_page()
        base_url = self._resolve_current_url(page)

        raw_links: list[dict[str, Any]] = []
        try:
            if hasattr(page, "evaluate") and callable(page.evaluate):
                evaluated = await page.evaluate(_EXTRACT_LINKS_JS)
                if isinstance(evaluated, list):
                    raw_links = [item for item in evaluated if isinstance(item, dict)]
            elif hasattr(page, "query_selector_all") and callable(page.query_selector_all):
                elements = await page.query_selector_all("a[href]")
                for el in elements:
                    href = await el.get_attribute("href")
                    text = await el.inner_text()
                    raw_links.append({"href": href or "", "text": (text or "").strip()})
        except Exception:
            self._record_action("extract_links", base_url or "page", "error")
            raise

        results: list[dict[str, Any]] = []
        for item in raw_links:
            if len(results) >= max_links:
                break
            raw_href = str(item.get("href") or "").strip()
            if not raw_href:
                continue
            resolved_href = urljoin(base_url, raw_href) if base_url else raw_href
            text = str(item.get("text") or "").strip()
            try:
                validate_url(
                    resolved_href,
                    allow_lan_hosts=self.allow_lan_hosts,
                    resolve_dns=self.resolve_dns,
                )
                allowed = True
                blocked_reason: str | None = None
            except LocalBrowserPolicyError as exc:
                allowed = False
                blocked_reason = str(exc)

            if not allowed and not include_blocked:
                continue

            results.append(
                {
                    "href": resolved_href,
                    "text": text,
                    "allowed": allowed,
                    "blocked_reason": blocked_reason,
                }
            )

        self._record_action("extract_links", base_url or "page", "ok", count=len(results))
        return results

    async def snapshot_dom_summary(self, max_chars: int = 2000) -> dict[str, Any]:
        """Return a compact, token-budgeted DOM summary for edge LLM consumption."""
        if max_chars < 0:
            raise ValueError("max_chars must be >= 0")
        page = self._require_page()
        url = self._resolve_current_url(page)
        try:
            title = str(await page.title())
            raw_text = str(await page.inner_text("body"))
            interactive: dict[str, Any] = {
                "total": 0,
                "buttons": 0,
                "inputs": 0,
                "links": 0,
                "samples": [],
            }
            if hasattr(page, "evaluate") and callable(page.evaluate):
                evaluated = await page.evaluate(_INTERACTIVE_SUMMARY_JS)
                if isinstance(evaluated, dict):
                    interactive = {
                        "total": int(evaluated.get("total", 0)),
                        "buttons": int(evaluated.get("buttons", 0)),
                        "inputs": int(evaluated.get("inputs", 0)),
                        "links": int(evaluated.get("links", 0)),
                        "samples": list(evaluated.get("samples") or []),
                    }
        except Exception:
            self._record_action("snapshot_dom_summary", url or "page", "error")
            raise

        truncated = len(raw_text) > max_chars
        compact_text = raw_text[:max_chars] if truncated else raw_text

        self._record_action("snapshot_dom_summary", url or "page", "ok")
        return {
            "title": title,
            "url": url,
            "text": compact_text,
            "text_length": len(raw_text),
            "truncated": truncated,
            "max_chars": max_chars,
            "interactive_count": interactive["total"],
            "interactive": interactive,
        }

    async def screenshot(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._require_page().screenshot(path=str(out))
        except Exception:
            self._record_action("screenshot", str(out), "error")
            raise
        self._record_action("screenshot", str(out), "ok")
        return out
