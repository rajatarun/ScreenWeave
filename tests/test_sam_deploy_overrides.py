"""Deploy parameter override names must match CloudFormation (alphanumeric only)."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "main-stack.yaml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yaml"

_CFN_PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def test_screen_weave_decisions_sam_parameter_name():
    text = TEMPLATE.read_text()
    params_block = re.search(r"^Parameters:\n(.*?)^Conditions:", text, re.M | re.S)
    assert params_block is not None
    block = params_block.group(1)
    assert re.search(r"^\s+ScreenWeaveDecisions:\s*$", block, re.M)
    assert not re.search(r"^\s+SCREENWEAVE_DECISIONS:\s*$", block, re.M)
    assert _CFN_PARAM_RE.match("ScreenWeaveDecisions")


def test_worker_env_maps_screen_weave_decisions_parameter():
    template_text = TEMPLATE.read_text()
    assert "SCREENWEAVE_DECISIONS: !Ref ScreenWeaveDecisions" in template_text


def test_deploy_workflow_sam_override_format():
    workflow = DEPLOY_WORKFLOW.read_text()
    assert '"ScreenWeaveDecisions=$SCREENWEAVE_DECISIONS"' in workflow
    assert '"SCREENWEAVE_DECISIONS=$SCREENWEAVE_DECISIONS"' not in workflow
    assert "vars.SCREENWEAVE_DECISIONS || '0'" in workflow