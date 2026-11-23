"""Lightweight, zero-AWS single-tab local Playwright browser helper for edge/desktop hosts.

Designed for Raspberry Pi 5 / laptop usage where spinning up an EC2 crawler fleet
is unnecessary. Enforces strict URL scheme + SSRF host guards by default while
allowing explicit LAN host allowlisting when requested.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("LocalBrowserSession is not started; call await start() first")
        return self._page

    async def goto(self, url: str, *, timeout_ms: int = 15_000) -> dict[str, Any]:
        safe_url = validate_url(
            url, allow_lan_hosts=self.allow_lan_hosts, resolve_dns=self.resolve_dns
        )
        page = self._require_page()
        await page.goto(safe_url, timeout=timeout_ms)
        title = await page.title()
        return {"url": safe_url, "title": title}

    async def click(self, selector: str) -> None:
        await self._require_page().click(selector)

    async def fill(self, selector: str, value: str) -> None:
        await self._require_page().fill(selector, value)

    async def extract_text(self, selector: str = "body") -> str:
        return str(await self._require_page().inner_text(selector))

    async def screenshot(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        await self._require_page().screenshot(path=str(out))
        return out
