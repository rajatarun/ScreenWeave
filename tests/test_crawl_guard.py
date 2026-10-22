"""The crawler refuses any request to the instance's own network.

The Lambda refuses internal targets by literal address and name; a public
name can still resolve to one, and a page can redirect or load a subresource
from one. crawl.py checks each request after DNS. These tests run its guard
against real resolution of literal addresses and a stubbed resolver for names.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import socket
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Playwright exists only on the crawler instance.
_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

_spec = importlib.util.spec_from_file_location("crawl", ROOT / "src" / "crawler" / "crawl.py")
crawl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crawl)


@pytest.mark.parametrize("ip", [
    "169.254.169.254", "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.0.1", "100.64.1.1",
    "0.0.0.0", "::1", "fd00:ec2::254", "fe80::1", "::ffff:169.254.169.254", "224.0.0.1",
])
def test_internal_addresses(ip):
    assert crawl.is_internal_address(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses(ip):
    assert not crawl.is_internal_address(ip)


def _resolver(mapping):
    def getaddrinfo(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in mapping[host]]
    return getaddrinfo


def test_a_public_name_that_resolves_inside_is_refused(monkeypatch):
    monkeypatch.setattr(crawl.socket, "getaddrinfo", _resolver({
        "rebind.example.com": ["93.184.216.34", "169.254.169.254"],
        "example.com": ["93.184.216.34"],
    }))
    assert crawl.is_internal_host("rebind.example.com"), "any internal answer refuses"
    assert not crawl.is_internal_host("example.com")
    assert crawl.is_internal_host("nonexistent.invalid")
    assert crawl.is_internal_host("")


class _Route:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.outcome = None

    async def abort(self, reason):
        self.outcome = ("abort", reason)

    async def continue_(self):
        self.outcome = ("continue", None)


@pytest.mark.parametrize("url,outcome", [
    ("http://169.254.169.254/latest/meta-data/", "abort"),
    ("https://example.com/page", "continue"),
    ("https://rebind.example.com/", "abort"),
    ("data:text/plain,hi", "continue"),
])
def test_the_route_guard(monkeypatch, url, outcome):
    monkeypatch.setattr(crawl.socket, "getaddrinfo", _resolver({
        "example.com": ["93.184.216.34"], "rebind.example.com": ["10.0.0.1"],
        "169.254.169.254": ["169.254.169.254"],
    }))
    monkeypatch.setattr(crawl, "log", lambda *a, **k: None)
    route = _Route(url)
    asyncio.run(crawl.guard_request(route))
    assert route.outcome[0] == outcome


def test_every_browser_request_goes_through_the_guard():
    source = (ROOT / "src" / "crawler" / "crawl.py").read_text()
    assert 'await ctx.route("**/*", guard_request)' in source
    assert source.index('ctx.route("**/*"') < source.index("await crawl(page, BASE_URL, 0)")
