"""Unit tests for ScreenWeave's optional zero-AWS LocalBrowserSession."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "crawler"))

from local_browser import LocalBrowserPolicyError, LocalBrowserSession, validate_url


class _FakePage:
    def __init__(self) -> None:
        self.visited: list[str] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.visited.append(url)

    async def title(self) -> str:
        return "Local Home Dashboard"

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def inner_text(self, selector: str) -> str:
        return f"text({selector})"

    async def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"PNG")


def test_validate_url_blocks_schemes_metadata_and_unlisted_lan() -> None:
    with pytest.raises(LocalBrowserPolicyError):
        validate_url("file:///etc/passwd", resolve_dns=False)

    with pytest.raises(LocalBrowserPolicyError):
        validate_url("http://169.254.169.254/latest/meta-data/", allow_lan_hosts=("169.254.169.254",), resolve_dns=False)

    with pytest.raises(LocalBrowserPolicyError):
        validate_url("http://192.168.1.50/admin", resolve_dns=False)

    assert (
        validate_url(
            "http://192.168.1.50/admin",
            allow_lan_hosts=("192.168.1.50",),
            resolve_dns=False,
        )
        == "http://192.168.1.50/admin"
    )


@pytest.mark.asyncio
async def test_local_browser_session_single_tab_lifecycle(tmp_path: Path) -> None:
    page = _FakePage()
    sess = LocalBrowserSession(allow_lan_hosts=("192.168.1.50",), resolve_dns=False)
    await sess.start(page_override=page)

    info = await sess.goto("http://192.168.1.50/dashboard")
    assert info["title"] == "Local Home Dashboard"
    await sess.fill("#search", "lights")
    await sess.click("#submit")
    assert await sess.extract_text("main") == "text(main)"
    shot = await sess.screenshot(tmp_path / "shot.png")
    assert shot.exists()

    with pytest.raises(LocalBrowserPolicyError):
        await sess.goto("http://10.0.0.1/internal")

    await sess.close()
