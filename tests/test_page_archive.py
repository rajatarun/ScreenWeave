"""Unit and MCP integration tests for ScreenWeave PageArchiveStore."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src" / "crawler") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "crawler"))

from archive import PageArchiveStore  # noqa: E402
from local_mcp import LocalBrowserMCPServer, handle_rpc  # noqa: E402


def _sample_dom(
    *,
    url: str = "https://93.184.216.34/checkout",
    title: str = "Checkout",
    text: str = "Order summary\nSubtotal $42.00\nShipping Free\nTotal $42.00",
    samples: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    el_samples = (
        samples
        if samples is not None
        else [
            {"tag": "input", "text": "email", "selector": "#email"},
            {"tag": "button", "text": "Pay Now", "selector": "#pay-btn"},
            {"tag": "a", "text": "Cancel", "selector": "#cancel-link"},
        ]
    )
    return {
        "url": url,
        "title": title,
        "text": text,
        "interactive_count": len(el_samples),
        "interactive": {
            "total": len(el_samples),
            "buttons": sum(1 for s in el_samples if s.get("tag") == "button"),
            "inputs": sum(1 for s in el_samples if s.get("tag") == "input"),
            "links": sum(1 for s in el_samples if s.get("tag") == "a"),
            "samples": el_samples,
        },
    }


def test_save_and_get_latest_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with PageArchiveStore(db) as store:
        dom_v1 = _sample_dom(title="Checkout v1")
        snap1 = store.save_snapshot("checkout", dom_v1["url"], dom_v1, label="baseline")
        assert snap1.startswith("snap-")

        dom_v2 = _sample_dom(title="Checkout v2")
        snap2 = store.save_snapshot("checkout", dom_v2["url"], dom_v2, label="baseline")
        assert snap2 != snap1

        store.save_snapshot("checkout", dom_v1["url"], dom_v1, label="canary")

        latest_baseline = store.get_latest("checkout", label="baseline")
        assert latest_baseline is not None
        assert latest_baseline["snapshot_id"] == snap2
        assert latest_baseline["dom_summary"]["title"] == "Checkout v2"
        assert len(latest_baseline["sha256"]) == 64

        latest_any = store.get_latest("checkout")
        assert latest_any is not None
        assert latest_any["label"] == "canary"

        assert store.get_latest("nonexistent") is None


def test_zero_drift_passes_regression_gate(tmp_path: Path) -> None:
    with PageArchiveStore(tmp_path / "archive.db") as store:
        baseline = _sample_dom()
        store.save_snapshot("checkout", baseline["url"], baseline)

        verdict = store.check_dom_regression("checkout", _sample_dom(), max_changed_ratio=0.25)
        assert verdict["passed"] is True
        assert verdict["has_baseline"] is True
        assert verdict["changed_ratio"] == 0.0
        assert verdict["missing_selectors"] == []
        assert verdict["added_elements"] == []
        assert verdict["reason"] == "ok"


def test_major_structural_change_fails_regression_gate(tmp_path: Path) -> None:
    with PageArchiveStore(tmp_path / "archive.db") as store:
        baseline = _sample_dom()
        store.save_snapshot("checkout", baseline["url"], baseline)

        broken_dom = _sample_dom(
            title="503 Maintenance",
            text="Service temporarily unavailable. Please try again later.",
            samples=[
                {"tag": "a", "text": "Status Page", "selector": "#status-link"},
            ],
        )
        verdict = store.check_dom_regression("checkout", broken_dom, max_changed_ratio=0.25)
        assert verdict["passed"] is False
        assert verdict["has_baseline"] is True
        assert verdict["changed_ratio"] > 0.25
        assert "#pay-btn" in verdict["missing_selectors"]
        assert "#email" in verdict["missing_selectors"]
        assert "#status-link" in verdict["added_elements"]
        assert verdict["diff"]["title_changed"] is True
        assert verdict["diff"]["text_changed"] is True
        assert verdict["reason"].startswith("dom_regression_exceeded:")


def test_missing_baseline_fails_closed(tmp_path: Path) -> None:
    with PageArchiveStore(tmp_path / "archive.db") as store:
        verdict = store.check_dom_regression("unknown-page", _sample_dom())
        assert verdict["passed"] is False
        assert verdict["has_baseline"] is False
        assert verdict["changed_ratio"] == 1.0
        assert "missing_baseline" in verdict["reason"]

        with pytest.raises(ValueError, match="max_changed_ratio"):
            store.check_dom_regression("unknown-page", _sample_dom(), max_changed_ratio=1.5)


def test_mcp_browser_check_regression_tool(tmp_path: Path) -> None:
    async def _run() -> None:
        with PageArchiveStore(tmp_path / "mcp_archive.db") as store:
            baseline = _sample_dom()
            store.save_snapshot("checkout", baseline["url"], baseline)

            server = LocalBrowserMCPServer(resolve_dns=False, archive_store=store)

            # Identical DOM passes via MCP tools/call
            ok_resp = await handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "browser_check_regression",
                        "arguments": {
                            "page_key": "checkout",
                            "current_dom_summary": _sample_dom(),
                            "max_changed_ratio": 0.2,
                        },
                    },
                },
                server=server,
            )
            sc_ok = ok_resp["result"]["structuredContent"]
            assert sc_ok["passed"] is True
            assert sc_ok["changed_ratio"] == 0.0

            # Regressed DOM fails cleanly via direct RPC method
            reg_resp = await handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "browser/browser_check_regression",
                    "params": {
                        "page_key": "checkout",
                        "current_dom_summary": _sample_dom(
                            title="Broken",
                            text="Completely different error page",
                            samples=[],
                        ),
                        "max_changed_ratio": 0.2,
                    },
                },
                server=server,
            )
            assert reg_resp["result"]["passed"] is False
            assert reg_resp["result"]["changed_ratio"] > 0.2

    asyncio.run(_run())
