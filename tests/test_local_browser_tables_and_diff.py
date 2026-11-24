"""Unit tests for LocalBrowserSession table extraction, snapshot diffing, and MCP tool exposure."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))

from local_browser import LocalBrowserSession, diff_snapshots  # noqa: E402
from local_mcp import (  # noqa: E402
    ERR_INVALID_PARAMS,
    ERR_SESSION_NOT_FOUND,
    LocalBrowserMCPServer,
)

SAMPLE_HTML_TABLES = """
<html>
  <body>
    <table id="pricing">
      <thead>
        <tr><th>Plan</th><th>Price</th><th>Requests / mo</th></tr>
      </thead>
      <tbody>
        <tr><td>Starter</td><td>$0</td><td>1,000</td></tr>
        <tr><td>Pro</td><td>$29</td><td>50,000</td></tr>
        <tr><td>Team</td><td>$99</td><td>250,000</td></tr>
        <tr><td>Enterprise</td><td>Custom</td><td>Unlimited</td></tr>
      </tbody>
    </table>

    <table id="status">
      <tr><th>Service</th><th>Region</th><th>Status</th></tr>
      <tr><td>API   Gateway</td><td>us-east-1</td><td>Operational</td></tr>
      <tr><td>Edge   Nodes</td><td>eu-west-1</td><td>Degraded</td></tr>
    </table>

    <table id="metrics">
      <tr><td>p50</td><td>12ms</td></tr>
      <tr><td>p95</td><td>45ms</td></tr>
    </table>
  </body>
</html>
"""


class _EvalTableFakePage:
    """Fake Playwright-compatible page returning structured tables via evaluate()."""

    def __init__(
        self,
        *,
        tables: list[dict[str, Any]] | None = None,
        title: str = "Pricing",
        body_text: str = "Line 1\nLine 2",
        interactive_total: int = 3,
    ) -> None:
        self.url: str = ""
        self._title = title
        self._body_text = body_text
        self._interactive_total = interactive_total
        self._tables = tables if tables is not None else [
            {
                "table_index": 0,
                "headers": ["Model", "Latency", "Cost"],
                "rows": [
                    ["llama-3b", "18ms", "$0.00"],
                    ["qwen-7b", "34ms", "$0.00"],
                    ["claude-sonnet", "420ms", "$0.015"],
                ],
            },
            {
                "table_index": 1,
                "headers": ["Node", "Temp"],
                "rows": [
                    ["pi-kitchen", "44C"],
                    ["pi-office", "47C"],
                ],
            },
        ]

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.url = url

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return self._body_text

    async def evaluate(self, script: str) -> Any:
        if "querySelectorAll('table')" in script:
            return list(self._tables)
        return {
            "total": self._interactive_total,
            "buttons": 1,
            "inputs": 1,
            "links": max(0, self._interactive_total - 2),
            "samples": [],
        }


class _HtmlContentFakePage:
    """Fake page exposing raw HTML via content() (no evaluate()) to exercise HTMLParser fallback."""

    def __init__(self, html: str, *, title: str = "HTML Tables") -> None:
        self.url: str = ""
        self._html = html
        self._title = title

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.url = url

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return "Table page"

    async def content(self) -> str:
        return self._html


def test_extract_tables_from_evaluated_page_and_respects_limits() -> None:
    async def _run() -> None:
        page = _EvalTableFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/pricing")

        tables = await sess.extract_tables(max_tables=10, max_rows=25)
        assert len(tables) == 2
        assert tables[0] == {
            "table_index": 0,
            "headers": ["Model", "Latency", "Cost"],
            "rows": [
                ["llama-3b", "18ms", "$0.00"],
                ["qwen-7b", "34ms", "$0.00"],
                ["claude-sonnet", "420ms", "$0.015"],
            ],
            "truncated": False,
        }
        assert tables[1]["table_index"] == 1
        assert tables[1]["truncated"] is False

        # Respect max_rows truncation and flag truncated=True.
        capped_rows = await sess.extract_tables(max_tables=10, max_rows=2)
        assert len(capped_rows[0]["rows"]) == 2
        assert capped_rows[0]["truncated"] is True
        assert len(capped_rows[1]["rows"]) == 2
        assert capped_rows[1]["truncated"] is False

        # Respect max_tables limit.
        capped_tables = await sess.extract_tables(max_tables=1, max_rows=25)
        assert len(capped_tables) == 1
        assert capped_tables[0]["table_index"] == 0

        # Negative bounds rejected.
        with pytest.raises(ValueError, match="max_tables"):
            await sess.extract_tables(max_tables=-1)
        with pytest.raises(ValueError, match="max_rows"):
            await sess.extract_tables(max_rows=-1)

        # Action log records extract_tables calls.
        table_actions = [a for a in sess.get_action_log() if a["action"] == "extract_tables"]
        assert len(table_actions) == 3
        assert all(a["status"] == "ok" for a in table_actions)

        await sess.close()

    asyncio.run(_run())


def test_extract_tables_parses_real_html_fallback_and_truncates() -> None:
    async def _run() -> None:
        page = _HtmlContentFakePage(SAMPLE_HTML_TABLES)
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/status")

        tables = await sess.extract_tables(max_tables=2, max_rows=2)
        assert len(tables) == 2

        # First table has <thead> headers and 4 rows -> truncated to 2.
        assert tables[0] == {
            "table_index": 0,
            "headers": ["Plan", "Price", "Requests / mo"],
            "rows": [
                ["Starter", "$0", "1,000"],
                ["Pro", "$29", "50,000"],
            ],
            "truncated": True,
        }

        # Second table infers headers from first <th> row and collapses cell whitespace.
        assert tables[1] == {
            "table_index": 1,
            "headers": ["Service", "Region", "Status"],
            "rows": [
                ["API Gateway", "us-east-1", "Operational"],
                ["Edge Nodes", "eu-west-1", "Degraded"],
            ],
            "truncated": False,
        }

        # Third table (headerless) is included when max_tables >= 3.
        all_tables = await sess.extract_tables(max_tables=5, max_rows=10)
        assert len(all_tables) == 3
        assert all_tables[2] == {
            "table_index": 2,
            "headers": [],
            "rows": [["p50", "12ms"], ["p95", "45ms"]],
            "truncated": False,
        }

        await sess.close()

    asyncio.run(_run())


def test_diff_snapshots_identical_and_changed() -> None:
    snap_before = {
        "url": "https://93.184.216.34/cart",
        "title": "Cart (1)",
        "text": "Header\nItem A - $10\nTotal: $10",
        "interactive_count": 5,
    }

    # Identical snapshots produce no changes.
    same = diff_snapshots(snap_before, dict(snap_before))
    assert same == {
        "url_changed": False,
        "title_changed": False,
        "text_changed": False,
        "added_lines": [],
        "removed_lines": [],
        "interactive_delta": 0,
    }

    # Changed URL, title, text lines, and interactive element count.
    snap_after = {
        "url": "https://93.184.216.34/checkout",
        "title": "Checkout",
        "text": "Header\nItem A - $10\nItem B - $15\nTotal: $25",
        "interactive_count": 8,
    }
    delta = diff_snapshots(snap_before, snap_after)
    assert delta["url_changed"] is True
    assert delta["title_changed"] is True
    assert delta["text_changed"] is True
    assert delta["added_lines"] == ["Item B - $15", "Total: $25"]
    assert delta["removed_lines"] == ["Total: $10"]
    assert delta["interactive_delta"] == 3

    # Fallback to nested interactive["total"] when interactive_count is omitted.
    nested_before = {"url": "https://a.test", "title": "A", "text": "x", "interactive": {"total": 7}}
    nested_after = {"url": "https://a.test", "title": "A", "text": "x", "interactive": {"total": 4}}
    nested_diff = diff_snapshots(nested_before, nested_after)
    assert nested_diff["interactive_delta"] == -3
    assert nested_diff["text_changed"] is False

    with pytest.raises(ValueError, match="dict"):
        diff_snapshots("not-a-dict", snap_after)  # type: ignore[arg-type]


def test_diff_snapshots_with_session_dom_summaries() -> None:
    async def _run() -> None:
        page = _EvalTableFakePage(
            title="Step 1",
            body_text="Welcome\nStatus: Pending",
            interactive_total=4,
        )
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/flow")

        before = await sess.snapshot_dom_summary(max_chars=500)

        # Simulate page transition after form submit.
        page.url = "https://93.184.216.34/flow/done"
        page._title = "Step 2"
        page._body_text = "Welcome\nStatus: Confirmed\nReceipt #42"
        page._interactive_total = 2

        after = await sess.snapshot_dom_summary(max_chars=500)
        delta = diff_snapshots(before, after)
        assert delta == {
            "url_changed": True,
            "title_changed": True,
            "text_changed": True,
            "added_lines": ["Status: Confirmed", "Receipt #42"],
            "removed_lines": ["Status: Pending"],
            "interactive_delta": -2,
        }

        await sess.close()

    asyncio.run(_run())


def test_mcp_server_exposes_and_dispatches_extract_tables_and_diff_snapshots() -> None:
    async def _run() -> None:
        server = LocalBrowserMCPServer(
            resolve_dns=False,
            page_factory=lambda: _HtmlContentFakePage(SAMPLE_HTML_TABLES),
        )

        # tools/list includes both Cycle 3 tools.
        list_resp = await server.handle_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        assert {"browser_extract_tables", "browser_diff_snapshots"}.issubset(tool_names)

        # browser_extract_tables without an active session returns ERR_SESSION_NOT_FOUND.
        missing_sess = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "browser_extract_tables", "arguments": {"session_id": "missing"}},
            }
        )
        assert missing_sess["error"]["code"] == ERR_SESSION_NOT_FOUND

        # Navigate and extract tables via tools/call.
        nav_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "navigate",
                    "arguments": {"url": "https://93.184.216.34/plans", "session_id": "s1"},
                },
            }
        )
        assert "error" not in nav_resp

        tables_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "browser_extract_tables",
                    "arguments": {"session_id": "s1", "max_tables": 2, "max_rows": 3},
                },
            }
        )
        assert "error" not in tables_resp
        payload = tables_resp["result"]["structuredContent"]
        assert payload["session_id"] == "s1"
        assert payload["count"] == 2
        assert payload["tables"][0]["headers"] == ["Plan", "Price", "Requests / mo"]
        assert len(payload["tables"][0]["rows"]) == 3
        assert payload["tables"][0]["truncated"] is True
        # Ensure text content is valid JSON matching structuredContent.
        assert json.loads(tables_resp["result"]["content"][0]["text"]) == payload

        # browser_diff_snapshots via tools/call (pure diff, no session required).
        diff_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "browser_diff_snapshots",
                    "arguments": {
                        "before": {
                            "url": "https://93.184.216.34/a",
                            "title": "A",
                            "text": "one\ntwo",
                            "interactive_count": 2,
                        },
                        "after": {
                            "url": "https://93.184.216.34/b",
                            "title": "A",
                            "text": "one\nthree",
                            "interactive_count": 5,
                        },
                    },
                },
            }
        )
        assert "error" not in diff_resp
        diff_payload = diff_resp["result"]["structuredContent"]
        assert diff_payload == {
            "url_changed": True,
            "title_changed": False,
            "text_changed": True,
            "added_lines": ["three"],
            "removed_lines": ["two"],
            "interactive_delta": 3,
        }

        # Direct JSON-RPC method dispatch also works for browser_diff_snapshots.
        direct_diff = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "browser_diff_snapshots",
                "params": {
                    "before": {"url": "u", "title": "t", "text": "same", "interactive_count": 1},
                    "after": {"url": "u", "title": "t", "text": "same", "interactive_count": 1},
                },
            }
        )
        assert direct_diff["result"]["text_changed"] is False
        assert direct_diff["result"]["interactive_delta"] == 0

        # Missing required args returns ERR_INVALID_PARAMS.
        bad_diff = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "browser_diff_snapshots", "arguments": {"before": {}}},
            }
        )
        assert bad_diff["error"]["code"] == ERR_INVALID_PARAMS

        await server.close_all()

    asyncio.run(_run())
