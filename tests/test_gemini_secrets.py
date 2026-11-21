"""Unit tests for Gemini API key resolution (mocked Secrets Manager)."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

WORKER = pathlib.Path(__file__).resolve().parents[1] / "src" / "lambda" / "visualQAWorker"
sys.path.insert(0, str(WORKER))

import gemini_secrets as gs  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    gs.reset_gemini_key_cache_for_tests()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_SECRET_NAME", raising=False)
    monkeypatch.delenv("GEMINI_SECRET_REGION", raising=False)


def test_env_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    def _boom(*_a, **_k):
        raise AssertionError("Secrets Manager must not be called when env is set")

    monkeypatch.setattr("boto3.client", _boom)
    assert gs.get_gemini_api_key() == "from-env"


def test_plain_string_secret(monkeypatch):
    class _Sm:
        def get_secret_value(self, SecretId):  # noqa: N803
            assert SecretId == "gemini/api_key"
            return {"SecretString": "sk-plain"}

    class _Boto3:
        @staticmethod
        def client(service, region_name):
            assert service == "secretsmanager"
            assert region_name == "us-east-1"
            return _Sm()

    monkeypatch.setattr("boto3.client", _Boto3.client)
    assert gs.get_gemini_api_key() == "sk-plain"
    assert gs.get_gemini_api_key() == "sk-plain"  # cached


def test_json_secret_api_key_field(monkeypatch):
    payload = json.dumps({"api_key": "sk-json"})

    class _Sm:
        def get_secret_value(self, **_kwargs):
            return {"SecretString": payload}

    monkeypatch.setattr(
        "boto3.client",
        lambda service, region_name: _Sm(),
    )
    assert gs.get_gemini_api_key() == "sk-json"


def test_secret_failure_returns_none(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("denied")

    monkeypatch.setattr("boto3.client", _boom)
    assert gs.get_gemini_api_key() is None
    assert gs.get_gemini_api_key() is None


def test_create_genai_client_uses_secret(monkeypatch):
    import screen_decisions as sd

    monkeypatch.setenv(sd.FLAG_ENV, "1")
    monkeypatch.setattr(gs, "get_gemini_api_key", lambda: "sk-test")

    captured: dict = {}

    class _FakeGenai:
        @staticmethod
        def Client(**kwargs):
            captured.update(kwargs)
            return object()

    import sys as _sys

    fake_mod = type(_sys)("google.genai")
    fake_mod.Client = _FakeGenai.Client
    fake_google = type(_sys)("google")
    fake_google.genai = fake_mod
    monkeypatch.setitem(_sys.modules, "google", fake_google)
    monkeypatch.setitem(_sys.modules, "google.genai", fake_mod)

    client = sd.create_genai_client()
    assert client is not None
    assert captured.get("api_key") == "sk-test"
    assert captured.get("http_options") == {"api_version": "v1beta"}