"""Unit tests for LocalBrowserSession snapshots, link extraction, form fill, action log, and MCP exposure."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))

from local_browser import LocalBrowserPolicyError, LocalBrowserSession  # noqa: E402
from local_mcp import (  # noqa: E402
    ERR_INVALID_PARAMS,
    ERR_POLICY_FORBIDDEN,
    LocalBrowserMCPServer,
)


class _RichFakePage:
    """Fake Playwright-compatible page supporting links, forms, and DOM summary evaluation."""

    def __init__(
        self,
        *,
        title: str = "Weave Portal",
        body_text: str = "Hello world from Weave Portal.",
        links: list[dict[str, str]] | None = None,
        interactive: dict[str, Any] | None = None,
    ) -> None:
        self.url: str = ""
        self._title = title
        self._body_text = body_text
        self._links = links if links is not None else [
            {"href": "/docs/getting-started", "text": "Docs"},
            {"href": "https://93.184.216.34/api", "text": "Public API"},
            {"href": "http://127.0.0.1:8080/internal", "text": "Local Admin"},
            {"href": "http://169.254.169.254/latest/meta-data/", "text": "Metadata"},
            {"href": "javascript:void(0)", "text": "JS Void"},
        ]
        self._interactive = interactive if interactive is not None else {
            "total": 4,
            "buttons": 1,
            "inputs": 2,
            "links": 1,
            "samples": [
                {"tag": "input", "text": "username"},
                {"tag": "button", "text": "Sign in"},
            ],
        }
        self.visited: list[str] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.visited.append(url)
        self.url = url

    async def title(self) -> str:
        return self._title

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def inner_text(self, selector: str) -> str:
        if selector == "body":
            return self._body_text
        return f"text({selector})"

    async def evaluate(self, script: str) -> Any:
        if "a[href]" in script and "buttons" not in script:
            return list(self._links)
        return dict(self._interactive)


def test_snapshot_dom_summary_respects_max_chars_budget() -> None:
    async def _run() -> None:
        long_text = "A" * 500 + "B" * 500
        page = _RichFakePage(title="Long Page", body_text=long_text)
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/article")

        snap = await sess.snapshot_dom_summary(max_chars=120)
        assert snap["title"] == "Long Page"
        assert snap["url"] == "https://93.184.216.34/article"
        assert len(snap["text"]) == 120
        assert snap["text"] == "A" * 120
        assert snap["text_length"] == 1000
        assert snap["truncated"] is True
        assert snap["max_chars"] == 120
        assert snap["interactive_count"] == 4
        assert snap["interactive"]["buttons"] == 1
        assert snap["interactive"]["inputs"] == 2

        full_snap = await sess.snapshot_dom_summary(max_chars=2000)
        assert full_snap["truncated"] is False
        assert len(full_snap["text"]) == 1000

        with pytest.raises(ValueError, match="max_chars"):
            await sess.snapshot_dom_summary(max_chars=-1)

        await sess.close()

    asyncio.run(_run())


def test_extract_links_returns_structured_links_and_ssrf_tags() -> None:
    async def _run() -> None:
        page = _RichFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/home")

        all_links = await sess.extract_links(max_links=50)
        assert len(all_links) == 5

        by_text = {link["text"]: link for link in all_links}
        # Relative path resolves against current public URL and is allowed.
        assert by_text["Docs"]["href"] == "https://93.184.216.34/docs/getting-started"
        assert by_text["Docs"]["allowed"] is True
        assert by_text["Docs"]["blocked_reason"] is None

        # Public IP URL is allowed.
        assert by_text["Public API"]["allowed"] is True

        # Internal, metadata, and non-http schemes are tagged blocked.
        assert by_text["Local Admin"]["allowed"] is False
        assert by_text["Local Admin"]["blocked_reason"]
        assert by_text["Metadata"]["allowed"] is False
        assert by_text["JS Void"]["allowed"] is False

        # include_blocked=False filters out disallowed targets.
        safe_only = await sess.extract_links(max_links=50, include_blocked=False)
        assert [link["text"] for link in safe_only] == ["Docs", "Public API"]

        # max_links caps returned entries.
        capped = await sess.extract_links(max_links=2)
        assert len(capped) == 2

        await sess.close()

    asyncio.run(_run())


def test_fill_input_invokes_backend_and_redacts_secrets() -> None:
    async def _run() -> None:
        page = _RichFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/login")

        plain_res = await sess.fill_input("#username", "alice@example.com", redact_value=False)
        assert plain_res == {
            "selector": "#username",
            "filled": True,
            "redacted": False,
            "value": "alice@example.com",
        }

        secret_value = "s3cr3t-passw0rd-999!"
        secret_res = await sess.fill_input("#password", secret_value, redact_value=True)
        assert secret_res == {
            "selector": "#password",
            "filled": True,
            "redacted": True,
            "value": "[REDACTED]",
        }

        # Page backend still receives the actual value for both fields.
        assert page.fills == [
            ("#username", "alice@example.com"),
            ("#password", secret_value),
        ]

        # Secret must never appear anywhere in the action log.
        log = sess.get_action_log()
        serialized = json.dumps(log)
        assert secret_value not in serialized
        assert "alice@example.com" in serialized

        pwd_entry = [e for e in log if e["target"] == "#password"][0]
        assert pwd_entry["action"] == "fill_input"
        assert pwd_entry["status"] == "ok"
        assert pwd_entry["redacted"] is True
        assert pwd_entry["value"] == "[REDACTED]"

        await sess.close()

    asyncio.run(_run())


def test_get_action_log_records_sequential_actions_including_blocked_ssrf() -> None:
    async def _run() -> None:
        page = _RichFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)

        await sess.goto("https://93.184.216.34/")
        await sess.fill_input("#q", "weave")
        await sess.click("#go")
        await sess.extract_links(max_links=10)
        await sess.snapshot_dom_summary(max_chars=100)

        with pytest.raises(LocalBrowserPolicyError):
            await sess.goto("http://169.254.169.254/latest/meta-data/")

        log = sess.get_action_log()
        assert [e["seq"] for e in log] == [1, 2, 3, 4, 5, 6]
        assert [e["action"] for e in log] == [
            "goto",
            "fill_input",
            "click",
            "extract_links",
            "snapshot_dom_summary",
            "goto",
        ]
        assert [e["status"] for e in log] == ["ok", "ok", "ok", "ok", "ok", "blocked"]
        for entry in log:
            assert set(entry.keys()) >= {"seq", "action", "target", "status", "timestamp"}
            assert isinstance(entry["timestamp"], str) and entry["timestamp"]

        # Mutating returned copy must not affect internal log.
        log[0]["status"] = "tampered"
        assert sess.get_action_log()[0]["status"] == "ok"

        await sess.close()

    asyncio.run(_run())


def test_mcp_server_dispatches_all_four_new_tools_and_blocks_ssrf() -> None:
    async def _run() -> None:
        pages: list[_RichFakePage] = []

        def _make_page() -> _RichFakePage:
            p = _RichFakePage(body_text="0123456789" * 30)
            pages.append(p)
            return p

        server = LocalBrowserMCPServer(resolve_dns=False, page_factory=_make_page)

        # tools/list advertises all 4 new tools.
        list_resp = await server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        assert {
            "browser_extract_links",
            "browser_fill_input",
            "browser_snapshot_dom",
            "browser_action_log",
        }.issubset(tool_names)

        # SSRF is still blocked before creating any session.
        ssrf_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "navigate", "arguments": {"url": "http://127.0.0.1/secret", "session_id": "s1"}},
            }
        )
        assert ssrf_resp["error"]["code"] == ERR_POLICY_FORBIDDEN
        assert pages == []

        # Navigate to allowed URL.
        nav_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "navigate", "arguments": {"url": "https://93.184.216.34/", "session_id": "s1"}},
            }
        )
        assert "error" not in nav_resp

        # 1. browser_extract_links
        links_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "browser_extract_links",
                    "arguments": {"session_id": "s1", "max_links": 10},
                },
            }
        )
        links_payload = links_resp["result"]["structuredContent"]
        assert links_payload["count"] == 5
        assert any(link["allowed"] is False for link in links_payload["links"])

        # 2. browser_fill_input with redact_value=True
        fill_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "browser_fill_input",
                    "arguments": {
                        "session_id": "s1",
                        "selector": "#api-key",
                        "value": "top-secret-token",
                        "redact_value": True,
                    },
                },
            }
        )
        fill_payload = fill_resp["result"]["structuredContent"]
        assert fill_payload["filled"] is True
        assert fill_payload["value"] == "[REDACTED]"
        assert pages[0].fills == [("#api-key", "top-secret-token")]

        # Invalid browser_fill_input params return ERR_INVALID_PARAMS.
        bad_fill = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "browser_fill_input", "arguments": {"session_id": "s1", "selector": ""}},
            }
        )
        assert bad_fill["error"]["code"] == ERR_INVALID_PARAMS

        # 3. browser_snapshot_dom
        snap_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "browser_snapshot_dom",
                    "arguments": {"session_id": "s1", "max_chars": 50},
                },
            }
        )
        snap_payload = snap_resp["result"]["structuredContent"]
        assert len(snap_payload["text"]) == 50
        assert snap_payload["truncated"] is True
        assert snap_payload["interactive_count"] == 4

        # 4. browser_action_log
        log_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "browser_action_log", "arguments": {"session_id": "s1"}},
            }
        )
        log_payload = log_resp["result"]["structuredContent"]
        assert log_payload["count"] == 4
        assert [a["action"] for a in log_payload["actions"]] == [
            "goto",
            "extract_links",
            "fill_input",
            "snapshot_dom_summary",
        ]
        assert "top-secret-token" not in json.dumps(log_payload)

        await server.close_all()

    asyncio.run(_run())
