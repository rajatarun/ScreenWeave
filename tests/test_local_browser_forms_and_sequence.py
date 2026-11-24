"""Unit tests for LocalBrowserSession form discovery, action sequence runner, and MCP tools."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))

from local_browser import LocalBrowserSession  # noqa: E402
from local_mcp import (  # noqa: E402
    ERR_INVALID_PARAMS,
    ERR_SESSION_NOT_FOUND,
    LocalBrowserMCPServer,
)

SAMPLE_HTML_PAGE = """
<html>
  <body>
    <form id="login-form" action="/auth/login" method="post">
      <label for="user-email">Work Email</label>
      <input id="user-email" name="email" type="email" value="alice@example.com" required />

      <label for="user-pass">Password</label>
      <input id="user-pass" name="password" type="password" value="UltraSecretPass!99" required />

      <input type="hidden" name="csrf_token" value="csrf_secret_token_xyz" />

      <label>Remember me <input name="remember" type="checkbox" /></label>
      <input type="submit" value="Sign In" />
    </form>

    <form id="profile-form" action="/settings/profile">
      <select name="region" aria-label="Edge Region" required>
        <option value="us">US</option>
        <option value="eu">EU</option>
      </select>
      <textarea name="bio" placeholder="Short bio"></textarea>
    </form>

    <table>
      <thead><tr><th>Metric</th><th>Value</th></tr></thead>
      <tbody><tr><td>Latency</td><td>14ms</td></tr></tbody>
    </table>
  </body>
</html>
"""


class _EvalFormsAndFlowFakePage:
    """Fake Playwright-compatible page supporting evaluate(), fill(), click(), and navigation."""

    def __init__(self, *, forms: list[dict[str, Any]] | None = None) -> None:
        self.url: str = ""
        self._title = "Login Portal"
        self._body_text = "Sign in to Weave"
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self._forms = (
            forms
            if forms is not None
            else [
                {
                    "form_index": 0,
                    "action": "/login",
                    "method": "post",
                    "fields": [
                        {
                            "name": "username",
                            "field_type": "text",
                            "label": "Username",
                            "required": True,
                            "value": "alice",
                        },
                        {
                            "name": "password",
                            "field_type": "password",
                            "label": "Password",
                            "required": True,
                            "value": "LEAKED_PASSWORD_SHOULD_BE_DROPPED",
                        },
                        {
                            "name": "otp",
                            "field_type": "text",
                            "label": "2FA Code",
                            "required": False,
                        },
                    ],
                },
                {
                    "form_index": 1,
                    "action": "/search",
                    "method": "GET",
                    "fields": [
                        {
                            "name": "q",
                            "field_type": "search",
                            "label": "Search",
                            "required": False,
                        }
                    ],
                },
            ]
        )

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.url = url

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return self._body_text

    async def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)
        if selector == "button[type=submit]":
            self._title = "Dashboard"
            self._body_text = "Welcome back, alice"

    async def evaluate(self, script: str) -> Any:
        if "querySelectorAll('form')" in script:
            return list(self._forms)
        if "querySelectorAll('table')" in script:
            return [{"table_index": 0, "headers": ["Service", "Status"], "rows": [["Edge", "OK"]]}]
        return {"total": 4, "buttons": 1, "inputs": 2, "links": 1, "samples": []}


class _HtmlContentFlowFakePage:
    """Fake page exposing raw HTML via content() (no evaluate()) for HTMLParser fallback tests."""

    def __init__(self, html: str = SAMPLE_HTML_PAGE) -> None:
        self.url: str = ""
        self._html = html
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    async def goto(self, url: str, timeout: int = 15000) -> None:
        self.url = url

    async def title(self) -> str:
        return "HTML Form Page"

    async def inner_text(self, selector: str) -> str:
        return "Form page body"

    async def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def content(self) -> str:
        return self._html


def test_extract_forms_from_evaluated_page_drops_secrets_and_enforces_limits() -> None:
    async def _run() -> None:
        page = _EvalFormsAndFlowFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/login")

        forms = await sess.extract_forms(max_forms=10, max_fields=30)
        assert len(forms) == 2
        assert forms[0] == {
            "form_index": 0,
            "action": "/login",
            "method": "POST",
            "fields": [
                {"name": "username", "field_type": "text", "label": "Username", "required": True},
                {"name": "password", "field_type": "password", "label": "Password", "required": True},
                {"name": "otp", "field_type": "text", "label": "2FA Code", "required": False},
            ],
        }
        # Never expose current or default values from evaluated DOM objects.
        serialized = json.dumps(forms)
        assert "LEAKED_PASSWORD_SHOULD_BE_DROPPED" not in serialized
        for form in forms:
            for fld in form["fields"]:
                assert set(fld.keys()) == {"name", "field_type", "label", "required"}

        # Enforce max_forms and max_fields limits.
        capped = await sess.extract_forms(max_forms=1, max_fields=2)
        assert len(capped) == 1
        assert len(capped[0]["fields"]) == 2

        with pytest.raises(ValueError, match="max_forms"):
            await sess.extract_forms(max_forms=-1)
        with pytest.raises(ValueError, match="max_fields"):
            await sess.extract_forms(max_fields=-1)

        form_logs = [e for e in sess.get_action_log() if e["action"] == "extract_forms"]
        assert len(form_logs) == 2
        assert all(e["status"] == "ok" for e in form_logs)

        await sess.close()

    asyncio.run(_run())


def test_extract_forms_html_parser_fallback_never_leaks_password_or_hidden_values() -> None:
    async def _run() -> None:
        page = _HtmlContentFlowFakePage(SAMPLE_HTML_PAGE)
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)
        await sess.goto("https://93.184.216.34/auth")

        forms = await sess.extract_forms()
        assert len(forms) == 2

        login_form = forms[0]
        assert login_form["form_index"] == 0
        assert login_form["action"] == "/auth/login"
        assert login_form["method"] == "POST"
        assert login_form["fields"] == [
            {"name": "email", "field_type": "email", "label": "Work Email", "required": True},
            {"name": "password", "field_type": "password", "label": "Password", "required": True},
            {"name": "csrf_token", "field_type": "hidden", "label": "", "required": False},
            {"name": "remember", "field_type": "checkbox", "label": "Remember me", "required": False},
        ]

        profile_form = forms[1]
        assert profile_form["form_index"] == 1
        assert profile_form["action"] == "/settings/profile"
        assert profile_form["method"] == "GET"
        assert profile_form["fields"] == [
            {"name": "region", "field_type": "select", "label": "Edge Region", "required": True},
            {"name": "bio", "field_type": "textarea", "label": "Short bio", "required": False},
        ]

        serialized = json.dumps(forms)
        assert "UltraSecretPass!99" not in serialized
        assert "csrf_secret_token_xyz" not in serialized
        assert "alice@example.com" not in serialized

        await sess.close()

    asyncio.run(_run())


def test_run_action_sequence_executes_multistep_flow_and_redacts_secrets() -> None:
    async def _run() -> None:
        page = _EvalFormsAndFlowFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)

        seq = await sess.run_action_sequence(
            [
                {"action": "goto", "url": "https://93.184.216.34/login"},
                {"action": "extract_forms", "max_forms": 5},
                {"action": "fill", "selector": "#username", "value": "alice"},
                {
                    "action": "fill",
                    "selector": "#password",
                    "value": "MyTopSecret#2026",
                    "redact_value": True,
                },
                {"action": "fill", "selector": "input[name=api_token]", "value": "tok_live_999"},
                {"action": "click", "selector": "button[type=submit]"},
                {"action": "extract_tables", "max_tables": 2},
                {"action": "snapshot", "max_chars": 500},
            ]
        )

        assert seq["ok"] is True
        assert seq["completed_steps"] == 8
        assert seq["total_steps"] == 8
        assert len(seq["results"]) == 8
        assert all(r["ok"] is True for r in seq["results"])

        # Page actually received the real values during fill.
        assert page.filled == [
            ("#username", "alice"),
            ("#password", "MyTopSecret#2026"),
            ("input[name=api_token]", "tok_live_999"),
        ]
        assert page.clicked == ["button[type=submit]"]

        # Secret fill values are redacted in BOTH sequence output and action log.
        assert seq["results"][2]["output"]["value"] == "alice"
        assert seq["results"][3]["output"]["value"] == "[REDACTED]"
        assert seq["results"][3]["output"]["redacted"] is True
        assert seq["results"][4]["output"]["value"] == "[REDACTED]"
        assert seq["results"][4]["output"]["redacted"] is True

        seq_json = json.dumps(seq)
        log_json = json.dumps(sess.get_action_log())
        assert "MyTopSecret#2026" not in seq_json
        assert "MyTopSecret#2026" not in log_json
        assert "tok_live_999" not in seq_json
        assert "tok_live_999" not in log_json

        # Snapshot reflects post-click state.
        assert seq["results"][7]["output"]["title"] == "Dashboard"
        assert seq["results"][6]["output"]["count"] == 1
        assert seq["results"][1]["output"]["count"] == 2

        await sess.close()

    asyncio.run(_run())


def test_run_action_sequence_halts_on_ssrf_or_unknown_action_and_supports_continue() -> None:
    async def _run() -> None:
        page = _EvalFormsAndFlowFakePage()
        sess = LocalBrowserSession(resolve_dns=False)
        await sess.start(page_override=page)

        # Halts on SSRF-blocked metadata URL when stop_on_error=True.
        ssrf_res = await sess.run_action_sequence(
            [
                {"action": "goto", "url": "https://93.184.216.34/start"},
                {"action": "goto", "url": "http://169.254.169.254/latest/meta-data/"},
                {"action": "click", "selector": "#should-not-run"},
            ],
            stop_on_error=True,
        )
        assert ssrf_res["ok"] is False
        assert ssrf_res["completed_steps"] == 1
        assert ssrf_res["total_steps"] == 3
        assert len(ssrf_res["results"]) == 2
        assert ssrf_res["results"][1]["ok"] is False
        assert ssrf_res["results"][1]["error_type"] == "LocalBrowserPolicyError"
        assert "#should-not-run" not in page.clicked

        # Halts on unknown action when stop_on_error=True.
        unknown_res = await sess.run_action_sequence(
            [
                {"action": "snapshot"},
                {"action": "execute_arbitrary_js"},
                {"action": "click", "selector": "#never-clicked"},
            ],
            stop_on_error=True,
        )
        assert unknown_res["ok"] is False
        assert unknown_res["completed_steps"] == 1
        assert unknown_res["total_steps"] == 3
        assert len(unknown_res["results"]) == 2
        assert "Unsupported action" in unknown_res["results"][1]["error"]
        assert "#never-clicked" not in page.clicked

        # Continues past errors when stop_on_error=False.
        cont_res = await sess.run_action_sequence(
            [
                {"action": "goto", "url": "http://127.0.0.1/internal"},
                {"action": "click", "selector": "#still-runs"},
            ],
            stop_on_error=False,
        )
        assert cont_res["ok"] is False
        assert cont_res["completed_steps"] == 1
        assert cont_res["total_steps"] == 2
        assert len(cont_res["results"]) == 2
        assert cont_res["results"][0]["ok"] is False
        assert cont_res["results"][1]["ok"] is True
        assert "#still-runs" in page.clicked

        with pytest.raises(ValueError, match="list"):
            await sess.run_action_sequence("not-a-list")  # type: ignore[arg-type]

        await sess.close()

    asyncio.run(_run())


def test_mcp_server_exposes_and_dispatches_extract_forms_and_run_sequence() -> None:
    async def _run() -> None:
        server = LocalBrowserMCPServer(
            resolve_dns=False,
            page_factory=lambda: _HtmlContentFlowFakePage(SAMPLE_HTML_PAGE),
        )

        # tools/list includes both Cycle 4 tools.
        list_resp = await server.handle_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        assert {"browser_extract_forms", "browser_run_sequence"}.issubset(tool_names)

        # browser_extract_forms without active session returns ERR_SESSION_NOT_FOUND.
        missing_forms = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "browser_extract_forms", "arguments": {"session_id": "none"}},
            }
        )
        assert missing_forms["error"]["code"] == ERR_SESSION_NOT_FOUND

        # browser_run_sequence without session and non-goto first step returns ERR_SESSION_NOT_FOUND.
        missing_seq = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "browser_run_sequence",
                    "arguments": {"session_id": "none", "steps": [{"action": "snapshot"}]},
                },
            }
        )
        assert missing_seq["error"]["code"] == ERR_SESSION_NOT_FOUND

        # Single-round-trip browser_run_sequence starting with goto auto-creates the session.
        seq_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "browser_run_sequence",
                    "arguments": {
                        "session_id": "flow1",
                        "steps": [
                            {"action": "goto", "url": "https://93.184.216.34/auth"},
                            {"action": "extract_forms"},
                            {
                                "action": "fill",
                                "selector": "#user-pass",
                                "value": "McpSecret!42",
                                "redact_value": True,
                            },
                            {"action": "click", "selector": "input[type=submit]"},
                            {"action": "snapshot", "max_chars": 200},
                        ],
                    },
                },
            }
        )
        assert "error" not in seq_resp
        seq_payload = seq_resp["result"]["structuredContent"]
        assert seq_payload["session_id"] == "flow1"
        assert seq_payload["ok"] is True
        assert seq_payload["completed_steps"] == 5
        assert seq_payload["total_steps"] == 5
        assert seq_payload["results"][1]["output"]["count"] == 2
        assert seq_payload["results"][2]["output"]["value"] == "[REDACTED]"
        assert "McpSecret!42" not in seq_resp["result"]["content"][0]["text"]
        assert json.loads(seq_resp["result"]["content"][0]["text"]) == seq_payload

        # browser_extract_forms via tools/call on active session.
        forms_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "browser_extract_forms",
                    "arguments": {"session_id": "flow1", "max_forms": 1, "max_fields": 2},
                },
            }
        )
        assert "error" not in forms_resp
        forms_payload = forms_resp["result"]["structuredContent"]
        assert forms_payload["session_id"] == "flow1"
        assert forms_payload["count"] == 1
        assert len(forms_payload["forms"][0]["fields"]) == 2
        assert "UltraSecretPass!99" not in forms_resp["result"]["content"][0]["text"]

        # Action log for flow1 also never contains the secret.
        log_resp = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "browser_action_log", "arguments": {"session_id": "flow1"}},
            }
        )
        assert "McpSecret!42" not in log_resp["result"]["content"][0]["text"]

        # Direct JSON-RPC method dispatch works for browser_extract_forms.
        direct_forms = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "browser_extract_forms",
                "params": {"session_id": "flow1"},
            }
        )
        assert direct_forms["result"]["count"] == 2

        # Invalid steps argument returns ERR_INVALID_PARAMS.
        bad_seq = await server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "browser_run_sequence",
                    "arguments": {"session_id": "flow1", "steps": "bad"},
                },
            }
        )
        assert bad_seq["error"]["code"] == ERR_INVALID_PARAMS

        await server.close_all()

    asyncio.run(_run())
