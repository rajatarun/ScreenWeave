"""Every Python function in the template imports the way Lambda imports it.

A module that raises at import time fails every invocation before the handler
runs, and nothing in a unit test that imports the pieces separately notices.
ContextWeave lost /mcp, /health and /finance for three hours that way
(LOG_LEVEL=DEMO handed to logging.setLevel at import). This runs each
handler module in a fresh interpreter with the function's CodeUri as the
working directory and only the environment the template gives it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "main-stack.yaml"


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(loader, suffix, node):
    return "placeholder"


_Loader.add_multi_constructor("!", _intrinsic)


def _functions():
    doc = yaml.load(TEMPLATE.read_text(), Loader=_Loader)
    globals_ = (doc.get("Globals") or {}).get("Function") or {}
    for name, res in doc["Resources"].items():
        if res.get("Type") != "AWS::Serverless::Function":
            continue
        props = res["Properties"]
        runtime = props.get("Runtime", globals_.get("Runtime", ""))
        if not str(runtime).startswith("python"):
            continue
        env = dict(((globals_.get("Environment") or {}).get("Variables")) or {})
        env.update(((props.get("Environment") or {}).get("Variables")) or {})
        yield pytest.param(name, props["CodeUri"], props["Handler"], env, id=name)


FUNCTIONS = list(_functions())


def test_template_has_python_functions():
    assert len(FUNCTIONS) >= 2


@pytest.mark.parametrize("name,code_uri,handler,env", FUNCTIONS)
def test_handler_imports(name, code_uri, handler, env):
    code_dir = (TEMPLATE.parent / code_uri).resolve()
    module, _, attr = handler.rpartition(".")
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        # No credentials: an import that reaches AWS fails here, as it would
        # cost a cold start in Lambda.
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        **{k: str(v) for k, v in env.items()},
    }
    code = (
        "import importlib, sys; sys.path.insert(0, '.');"
        f"m = importlib.import_module({module!r});"
        f"assert callable(getattr(m, {attr!r})), 'handler is not callable'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=code_dir, env=child_env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{name} ({code_uri}{handler}) failed to import:\n{result.stderr}"
