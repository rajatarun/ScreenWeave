"""Lightweight JSON-RPC 2.0 MCP adapter over LocalBrowserSession.

Exposes ``navigate``, ``extract_text``, ``click``, and ``close`` via both MCP
``tools/call`` and direct JSON-RPC method calls, enforcing ``validate_url``
SSRF protection and automatic idle-session cleanup.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    from .archive import PageArchiveStore
    from .local_browser import (
        LocalBrowserPolicyError,
        LocalBrowserSession,
        diff_snapshots,
        validate_url,
    )
except ImportError:
    from archive import PageArchiveStore
    from local_browser import (
        LocalBrowserPolicyError,
        LocalBrowserSession,
        diff_snapshots,
        validate_url,
    )

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_POLICY_FORBIDDEN = -32003
ERR_SESSION_NOT_FOUND = -32004

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Navigate a browser session to a URL after SSRF policy validation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target http/https URL."},
                "session_id": {"type": "string", "default": "default"},
                "timeout_ms": {"type": "integer", "default": 15000},
            },
            "required": ["url"],
        },
    },
    {
        "name": "extract_text",
        "description": "Extract visible text from a CSS selector in an active session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body"},
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "click",
        "description": "Click an element matching a CSS selector in an active session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "session_id": {"type": "string", "default": "default"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "close",
        "description": "Close an active browser session and release resources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_extract_links",
        "description": "Extract links from the active page with SSRF URL validation tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_links": {"type": "integer", "default": 50},
                "include_blocked": {"type": "boolean", "default": True},
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_fill_input",
        "description": "Fill an input matching a CSS selector in an active session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "redact_value": {"type": "boolean", "default": False},
                "session_id": {"type": "string", "default": "default"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser_snapshot_dom",
        "description": "Capture a compact, token-budgeted DOM summary for edge LLM consumption.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_chars": {"type": "integer", "default": 2000},
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_action_log",
        "description": "Return the sequential action audit log for an active browser session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_extract_tables",
        "description": "Extract compact structured HTML tables from the active browser session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_tables": {"type": "integer", "default": 10},
                "max_rows": {"type": "integer", "default": 25},
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_diff_snapshots",
        "description": "Compare two DOM summary snapshots and return structured URL, title, text, and interactive deltas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "before": {"type": "object", "description": "Earlier snapshot_dom_summary() output."},
                "after": {"type": "object", "description": "Later snapshot_dom_summary() output."},
            },
            "required": ["before", "after"],
        },
    },
    {
        "name": "browser_extract_forms",
        "description": "Discover compact form schemas (action, method, fields) from the active page without exposing secret values.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_forms": {"type": "integer", "default": 10},
                "max_fields": {"type": "integer", "default": 30},
                "session_id": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "browser_run_sequence",
        "description": "Execute an ordered sequence of browser actions (goto, fill, click, snapshot, extract_tables, extract_forms) with SSRF and redaction guards.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered list of step dicts with an 'action' key.",
                },
                "stop_on_error": {"type": "boolean", "default": True},
                "session_id": {"type": "string", "default": "default"},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "browser_check_regression",
        "description": "Check the current page DOM summary (or provided summary) against an archived baseline in PageArchiveStore.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_key": {"type": "string", "description": "Logical page key in PageArchiveStore."},
                "current_dom_summary": {
                    "type": "object",
                    "description": "Optional DOM summary dict; if omitted, snapshots the active session.",
                },
                "max_changed_ratio": {"type": "number", "default": 0.25},
                "baseline_label": {"type": "string", "default": "baseline"},
                "db_path": {
                    "type": "string",
                    "description": "Optional SQLite path when server has no default archive_store.",
                },
                "session_id": {"type": "string", "default": "default"},
            },
            "required": ["page_key"],
        },
    },
]


@dataclass
class _SessionEntry:
    session: LocalBrowserSession
    last_used: float


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}


@dataclass
class LocalBrowserMCPServer:
    """Manages LocalBrowserSession instances behind an MCP JSON-RPC handler."""

    headless: bool = True
    allow_lan_hosts: tuple[str, ...] = ()
    resolve_dns: bool = True
    idle_timeout_sec: float = 300.0
    page_factory: Callable[[], Any] | None = None
    archive_store: PageArchiveStore | None = None
    clock: Callable[[], float] = time.monotonic
    _sessions: dict[str, _SessionEntry] = field(default_factory=dict, repr=False)

    async def cleanup_idle_sessions(self) -> list[str]:
        """Close and evict sessions that have exceeded ``idle_timeout_sec``."""
        now = self.clock()
        expired = [
            sid
            for sid, entry in self._sessions.items()
            if (now - entry.last_used) >= self.idle_timeout_sec
        ]
        for sid in expired:
            entry = self._sessions.pop(sid)
            await entry.session.close()
        return expired

    async def close_all(self) -> list[str]:
        """Close every active session."""
        closed_ids = list(self._sessions.keys())
        for sid in closed_ids:
            entry = self._sessions.pop(sid)
            await entry.session.close()
        return closed_ids

    @property
    def active_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def _get_or_create_session(self, session_id: str) -> LocalBrowserSession:
        entry = self._sessions.get(session_id)
        if entry is not None:
            entry.last_used = self.clock()
            return entry.session

        session = LocalBrowserSession(
            headless=self.headless,
            allow_lan_hosts=self.allow_lan_hosts,
            resolve_dns=self.resolve_dns,
        )
        page_override = self.page_factory() if self.page_factory is not None else None
        await session.start(page_override=page_override)
        self._sessions[session_id] = _SessionEntry(session=session, last_used=self.clock())
        return session

    def _require_session(self, session_id: str) -> LocalBrowserSession:
        entry = self._sessions.get(session_id)
        if entry is None:
            raise KeyError(f"Browser session {session_id!r} does not exist; call navigate first")
        entry.last_used = self.clock()
        return entry.session

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id") or "default")

        if name == "navigate":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("navigate requires a non-empty 'url' string")
            timeout_ms = int(args.get("timeout_ms", 15_000))
            # Enforce SSRF guard before allocating or starting a browser session.
            validate_url(url, allow_lan_hosts=self.allow_lan_hosts, resolve_dns=self.resolve_dns)
            session = await self._get_or_create_session(session_id)
            nav = await session.goto(url, timeout_ms=timeout_ms)
            return {"session_id": session_id, "url": nav["url"], "title": nav["title"]}

        if name == "extract_text":
            selector = str(args.get("selector") or "body")
            session = self._require_session(session_id)
            text = await session.extract_text(selector)
            return {"session_id": session_id, "selector": selector, "text": text}

        if name == "click":
            selector = args.get("selector")
            if not isinstance(selector, str) or not selector.strip():
                raise ValueError("click requires a non-empty 'selector' string")
            session = self._require_session(session_id)
            await session.click(selector)
            return {"session_id": session_id, "selector": selector, "clicked": True}

        if name == "close":
            entry = self._sessions.pop(session_id, None)
            if entry is not None:
                await entry.session.close()
                return {"session_id": session_id, "closed": True}
            return {"session_id": session_id, "closed": False}

        if name == "browser_extract_links":
            max_links = int(args.get("max_links", 50))
            include_blocked = bool(args.get("include_blocked", True))
            session = self._require_session(session_id)
            links = await session.extract_links(max_links=max_links, include_blocked=include_blocked)
            return {"session_id": session_id, "links": links, "count": len(links)}

        if name == "browser_fill_input":
            selector = args.get("selector")
            value = args.get("value")
            if not isinstance(selector, str) or not selector.strip():
                raise ValueError("browser_fill_input requires a non-empty 'selector' string")
            if not isinstance(value, str):
                raise ValueError("browser_fill_input requires a string 'value'")
            redact_value = bool(args.get("redact_value", False))
            session = self._require_session(session_id)
            result = await session.fill_input(selector, value, redact_value=redact_value)
            return {"session_id": session_id, **result}

        if name == "browser_snapshot_dom":
            max_chars = int(args.get("max_chars", 2000))
            session = self._require_session(session_id)
            summary = await session.snapshot_dom_summary(max_chars=max_chars)
            return {"session_id": session_id, **summary}

        if name == "browser_action_log":
            session = self._require_session(session_id)
            actions = session.get_action_log()
            return {"session_id": session_id, "actions": actions, "count": len(actions)}

        if name == "browser_extract_tables":
            max_tables = int(args.get("max_tables", 10))
            max_rows = int(args.get("max_rows", 25))
            session = self._require_session(session_id)
            tables = await session.extract_tables(max_tables=max_tables, max_rows=max_rows)
            return {"session_id": session_id, "tables": tables, "count": len(tables)}

        if name == "browser_diff_snapshots":
            before = args.get("before")
            after = args.get("after")
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise ValueError("browser_diff_snapshots requires dict 'before' and 'after' arguments")
            return diff_snapshots(before, after)

        if name == "browser_extract_forms":
            max_forms = int(args.get("max_forms", 10))
            max_fields = int(args.get("max_fields", 30))
            session = self._require_session(session_id)
            forms = await session.extract_forms(max_forms=max_forms, max_fields=max_fields)
            return {"session_id": session_id, "forms": forms, "count": len(forms)}

        if name == "browser_run_sequence":
            steps = args.get("steps")
            if not isinstance(steps, list):
                raise ValueError("browser_run_sequence requires a list 'steps' argument")
            stop_on_error = bool(args.get("stop_on_error", True))
            if session_id in self._sessions:
                session = self._require_session(session_id)
            elif steps and isinstance(steps[0], dict) and steps[0].get("action") == "goto":
                session = await self._get_or_create_session(session_id)
            else:
                session = self._require_session(session_id)
            seq_result = await session.run_action_sequence(steps, stop_on_error=stop_on_error)
            return {"session_id": session_id, **seq_result}

        if name == "browser_check_regression":
            page_key = args.get("page_key")
            if not isinstance(page_key, str) or not page_key.strip():
                raise ValueError("browser_check_regression requires a non-empty 'page_key' string")
            max_changed_ratio = float(args.get("max_changed_ratio", 0.25))
            baseline_label = str(args.get("baseline_label") or "baseline")
            current_dom = args.get("current_dom_summary")
            if current_dom is None:
                session = self._require_session(session_id)
                current_dom = await session.snapshot_dom_summary()
            elif not isinstance(current_dom, dict):
                raise ValueError("browser_check_regression 'current_dom_summary' must be an object")

            if self.archive_store is not None:
                return self.archive_store.check_dom_regression(
                    page_key,
                    current_dom,
                    max_changed_ratio=max_changed_ratio,
                    baseline_label=baseline_label,
                )

            db_path = args.get("db_path")
            if isinstance(db_path, str) and db_path.strip():
                with PageArchiveStore(db_path.strip()) as temp_store:
                    return temp_store.check_dom_regression(
                        page_key,
                        current_dom,
                        max_changed_ratio=max_changed_ratio,
                        baseline_label=baseline_label,
                    )

            raise ValueError(
                "browser_check_regression requires an archive_store on the server or a 'db_path' argument"
            )

        raise LookupError(f"Unknown browser tool: {name!r}")

    async def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle a single JSON-RPC 2.0 / MCP request dictionary."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if not isinstance(method, str) or not method:
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, "Missing JSON-RPC 'method'")
        if not isinstance(params, dict):
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, "JSON-RPC 'params' must be an object")

        await self.cleanup_idle_sessions()

        if method == "initialize":
            return _jsonrpc_result(
                req_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "screenweave-local-browser", "version": "0.2.0"},
                },
            )

        if method == "tools/list":
            return _jsonrpc_result(req_id, {"tools": TOOL_DEFINITIONS})

        try:
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(tool_name, str) or not tool_name:
                    return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, "tools/call requires 'name'")
                if not isinstance(arguments, dict):
                    return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, "tools/call 'arguments' must be an object")
                payload = await self._dispatch_tool(tool_name, arguments)
                return _jsonrpc_result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                )

            direct_name = method.removeprefix("browser/")
            if direct_name in (
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
                "browser_check_regression",
            ):
                payload = await self._dispatch_tool(direct_name, params)
                return _jsonrpc_result(req_id, payload)

            return _jsonrpc_error(req_id, ERR_METHOD_NOT_FOUND, f"Unknown method: {method!r}")
        except LocalBrowserPolicyError as exc:
            return _jsonrpc_error(req_id, ERR_POLICY_FORBIDDEN, str(exc))
        except KeyError as exc:
            return _jsonrpc_error(req_id, ERR_SESSION_NOT_FOUND, str(exc.args[0]))
        except ValueError as exc:
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, str(exc))
        except LookupError as exc:
            return _jsonrpc_error(req_id, ERR_METHOD_NOT_FOUND, str(exc))


async def handle_rpc(
    request: dict[str, Any],
    *,
    server: LocalBrowserMCPServer | None = None,
    page_factory: Callable[[], Any] | None = None,
    allow_lan_hosts: tuple[str, ...] = (),
    resolve_dns: bool = True,
    idle_timeout_sec: float = 300.0,
) -> dict[str, Any]:
    """Dispatch a JSON-RPC MCP request against a provided or ephemeral server."""
    active_server = server or LocalBrowserMCPServer(
        allow_lan_hosts=allow_lan_hosts,
        resolve_dns=resolve_dns,
        idle_timeout_sec=idle_timeout_sec,
        page_factory=page_factory,
    )
    return await active_server.handle_rpc(request)
