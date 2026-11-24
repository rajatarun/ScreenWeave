"""Unit tests for ScreenWeave crawl.py lazy init and LocalBrowserMCPServer."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))

import local_mcp  # noqa: E402
from local_mcp import (  # noqa: E402
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    ERR_POLICY_FORBIDDEN,
    ERR_SESSION_NOT_FOUND,
    LocalBrowserMCPServer,
)


class _FakePage:
    def __init__(self) -> None:
        self.visited: list[str] = []
        self.clicks: list[str] = []
        self.text_by_selector: dict[str, str] = {
            "body": "Welcome to Example",
            "h1": "Example Domain",
        }

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.visited.append(url)

    async def title(self) -> str:
        return "Example Domain"

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)

    async def inner_text(self, selector: str) -> str:
        return self.text_by_selector.get(selector, f"text:{selector}")


def test_crawl_imports_cleanly_without_touching_opt_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Importing crawl.py must not create /opt/output or any output directory at import time."""
    target_out = tmp_path / "never-created-on-import"
    monkeypatch.setenv("SCREENWEAVE_OUT_DIR", str(target_out))

    # Stub playwright only if absent so import works offline.
    if "playwright" not in sys.modules:
        pw = types.ModuleType("playwright")
        pw_async = types.ModuleType("playwright.async_api")
        pw_async.async_playwright = None
        monkeypatch.setitem(sys.modules, "playwright", pw)
        monkeypatch.setitem(sys.modules, "playwright.async_api", pw_async)

    spec = importlib.util.spec_from_file_location("crawl_lazy_test", ROOT / "src" / "crawler" / "crawl.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.OUT_DIR == str(target_out)
    assert not target_out.exists(), "OUT_DIR must not be created at module import time"

    # Calling ensure_output_dirs() creates screenshots dir lazily.
    mod.ensure_output_dirs()
    assert (target_out / "screenshots").is_dir()


def test_crawl_default_out_dir_uses_tempdir_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCREENWEAVE_OUT_DIR", raising=False)
    if "playwright" not in sys.modules:
        pw = types.ModuleType("playwright")
        pw_async = types.ModuleType("playwright.async_api")
        pw_async.async_playwright = None
        monkeypatch.setitem(sys.modules, "playwright", pw)
        monkeypatch.setitem(sys.modules, "playwright.async_api", pw_async)

    spec = importlib.util.spec_from_file_location("crawl_default_dir_test", ROOT / "src" / "crawler" / "crawl.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    expected = str(Path(tempfile.gettempdir()) / "screenweave-output")
    assert mod.OUT_DIR == expected
    assert mod.OUT_DIR != "/opt/output"


def test_mcp_initialize_and_tools_list() -> None:
    async def _run() -> None:
        server = LocalBrowserMCPServer(resolve_dns=False)
        init_resp = await local_mcp.handle_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            server=server,
        )
        assert init_resp["result"]["serverInfo"]["name"] == "screenweave-local-browser"

        list_resp = await local_mcp.handle_rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            server=server,
        )
        names = [t["name"] for t in list_resp["result"]["tools"]]
        assert names == [
            "navigate",
            "extract_text",
            "click",
            "close",
            "browser_extract_links",
            "browser_fill_input",
            "browser_snapshot_dom",
            "browser_action_log",
            "browser_extract_tables",
            "browser_diff_snapshots",
            "browser_extract_forms",
            "browser_run_sequence",
        ]

    asyncio.run(_run())


def test_mcp_navigate_extract_click_and_close_with_mock_page() -> None:
    async def _run() -> None:
        pages: list[_FakePage] = []

        def _make_page() -> _FakePage:
            p = _FakePage()
            pages.append(p)
            return p

        server = LocalBrowserMCPServer(resolve_dns=False, page_factory=_make_page)

        nav = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "navigate",
                    "arguments": {"url": "https://93.184.216.34/docs", "session_id": "s1"},
                },
            },
            server=server,
        )
        assert "error" not in nav
        structured = nav["result"]["structuredContent"]
        assert structured["url"] == "https://93.184.216.34/docs"
        assert structured["title"] == "Example Domain"
        assert json.loads(nav["result"]["content"][0]["text"])["session_id"] == "s1"
        assert pages[0].visited == ["https://93.184.216.34/docs"]

        txt = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "extract_text",
                    "arguments": {"selector": "h1", "session_id": "s1"},
                },
            },
            server=server,
        )
        assert txt["result"]["structuredContent"]["text"] == "Example Domain"

        clk = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "browser/click",
                "params": {"selector": "#cta", "session_id": "s1"},
            },
            server=server,
        )
        assert clk["result"]["clicked"] is True
        assert pages[0].clicks == ["#cta"]

        cls = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "close", "arguments": {"session_id": "s1"}},
            },
            server=server,
        )
        assert cls["result"]["structuredContent"]["closed"] is True
        assert server.active_session_ids == []

        # Extracting text after close returns session-not-found error.
        after = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {"name": "extract_text", "arguments": {"session_id": "s1"}},
            },
            server=server,
        )
        assert after["error"]["code"] == ERR_SESSION_NOT_FOUND

    asyncio.run(_run())


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "http://localhost:8080/internal",
        "file:///etc/passwd",
    ],
)
def test_mcp_blocks_ssrf_targets(blocked_url: str) -> None:
    async def _run() -> None:
        created = 0

        def _make_page() -> _FakePage:
            nonlocal created
            created += 1
            return _FakePage()

        server = LocalBrowserMCPServer(resolve_dns=False, page_factory=_make_page)
        resp = await local_mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "navigate", "arguments": {"url": blocked_url}},
            },
            server=server,
        )
        assert "error" in resp
        assert resp["error"]["code"] == ERR_POLICY_FORBIDDEN
        assert created == 0, "Blocked SSRF request must not instantiate a browser session"
        assert server.active_session_ids == []

    asyncio.run(_run())


def test_mcp_idle_session_cleanup_evicts_expired_sessions() -> None:
    async def _run() -> None:
        now = [100.0]
        server = LocalBrowserMCPServer(
            resolve_dns=False,
            idle_timeout_sec=60.0,
            page_factory=_FakePage,
            clock=lambda: now[0],
        )

        resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "navigate",
                "params": {"url": "https://93.184.216.34/", "session_id": "idle-1"},
            }
        )
        assert "result" in resp
        assert server.active_session_ids == ["idle-1"]

        # Advance past idle timeout; next RPC call automatically evicts idle-1.
        now[0] = 165.0
        resp2 = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "extract_text",
                "params": {"session_id": "idle-1"},
            }
        )
        assert resp2["error"]["code"] == ERR_SESSION_NOT_FOUND
        assert server.active_session_ids == []

    asyncio.run(_run())


def test_mcp_invalid_method_and_params_errors() -> None:
    async def _run() -> None:
        server = LocalBrowserMCPServer(resolve_dns=False)
        unknown = await server.handle_rpc({"jsonrpc": "2.0", "id": 40, "method": "nope", "params": {}})
        assert unknown["error"]["code"] == ERR_METHOD_NOT_FOUND

        bad_nav = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {"name": "navigate", "arguments": {}},
            }
        )
        assert bad_nav["error"]["code"] == ERR_INVALID_PARAMS

    asyncio.run(_run())
