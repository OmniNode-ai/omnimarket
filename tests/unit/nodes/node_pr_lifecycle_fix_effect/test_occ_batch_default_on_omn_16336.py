# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The autobind publisher batches OCC companions by default (OMN-16336).

omnimarket#2940 shipped the batch path behind the repository variable
``OMNI_OCC_COMPANION_BATCH_MODE`` with every default reading ``off``. Nobody set
the variable for about 23 hours after the build merged, so every product PR kept
minting its own companion. These tests fail the moment any seam that decides the
companion grouping falls back to one companion per product PR by default again:

* the publisher every repository's autobind workflow runs (payload, CLI
  default, and the repository flag, which can now only turn batching off, and
  loudly, and refuses any value that is not a batching setting);
* the omnimarket inline workflow, which hands the flag through with no default
  of its own, and whose closed-unmerged producer runs unconditionally;
* the conflicted-companion re-mint, which asks for the per-PR path only by the
  explicit ``--batch-mode off`` flag.

The default grouping is the repository batch window since the cross-ticket
window build (operator ruling 2026-09-27T10:32:50Z).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.events.occ_companion import EnumOccBatchMode
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)

_ROOT = Path(__file__).resolve().parents[4]
_PUBLISHER = _ROOT / "scripts" / "publish_occ_autobind_command.py"
_REMINT = _ROOT / "scripts" / "ci" / "occ_conflicted_companion_remint.py"
_AUTOBIND_WORKFLOW = _ROOT / ".github" / "workflows" / "call-occ-autobind.yml"
_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-16336"
_ENV = {
    "PR_REPO": _REPO,
    "PR_NUMBER": "42",
    "PR_HEAD_SHA": "a" * 40,
    "PR_TITLE": f"feat({_TICKET}): batch by default",
}


def _load_publisher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("occ_batch_default_pub", _PUBLISHER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_publisher_payload_defaults_to_the_window_batch() -> None:
    publisher = _load_publisher()
    payload = publisher.build_payload(_REPO, 42, _TICKET, str(uuid4()))
    # Left off the wire so a runtime from before window mode still validates.
    assert "occ_batch_mode" not in payload
    command = ModelPrLifecycleFixCommand.model_validate(json.loads(json.dumps(payload)))
    assert command.occ_batch_mode is EnumOccBatchMode.WINDOW


@pytest.mark.unit
def test_publisher_names_the_per_pr_mode_on_the_wire() -> None:
    """An absent field means window, so the per-PR request must be explicit."""
    publisher = _load_publisher()
    payload = publisher.build_payload(
        _REPO, 42, _TICKET, str(uuid4()), batch_mode=EnumOccBatchMode.OFF
    )
    assert payload["occ_batch_mode"] == "off"


@pytest.mark.unit
@pytest.mark.parametrize("repo", [_REPO, "OmniNode-ai/omnibase_infra"])
def test_publisher_cli_batches_every_repo_with_no_flag_and_no_env(repo: str) -> None:
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run"],
        env={**_ENV, "PR_REPO": repo},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert '"occ_batch_mode"' not in result.output
    assert "::warning::" not in result.output


@pytest.mark.unit
@pytest.mark.parametrize("value", ["off", "OFF"])
def test_repo_flag_turns_batching_off_only_loudly(value: str) -> None:
    """Off is the one thing the flag can do, and it is never quiet."""
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run"],
        env={**_ENV, "OCC_COMPANION_BATCH_MODE": value},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "::warning::" in result.output
    assert "Queue health reports" in result.output
    assert '"occ_batch_mode": "off"' in result.output


@pytest.mark.unit
@pytest.mark.parametrize("value", ["per-pr", "on", "tickets"])
def test_repo_flag_refuses_a_value_that_is_not_a_setting(value: str) -> None:
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run"],
        env={**_ENV, "OCC_COMPANION_BATCH_MODE": value},
    )
    assert result.exit_code == 2
    assert "OCC_COMPANION_BATCH_MODE" in result.output
    assert '"occ_batch_mode"' not in result.output


@pytest.mark.unit
def test_explicit_off_flag_is_loud() -> None:
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run", "--batch-mode", "off"],
        env=_ENV,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "::warning::" in result.output
    assert '"occ_batch_mode": "off"' in result.output


@pytest.mark.unit
def test_autobind_workflow_hands_the_flag_through_with_no_default() -> None:
    text = _AUTOBIND_WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "OMNI_OCC_COMPANION_BATCH_MODE ||" not in code
    assert "--batch-mode" not in code
    workflow = yaml.safe_load(text)
    publish_env = next(
        step["env"]
        for step in workflow["jobs"]["publish-occ-autobind"]["steps"]
        if "OCC_COMPANION_BATCH_MODE" in (step.get("env") or {})
    )
    assert publish_env["OCC_COMPANION_BATCH_MODE"] == (
        "${{ vars.OMNI_OCC_COMPANION_BATCH_MODE }}"
    )
    assert "closed" in workflow[True]["pull_request"]["types"]
    gate = workflow["jobs"]["publish-occ-autobind"]["if"]
    assert "vars." not in gate
    assert "!github.event.pull_request.merged" in gate


@pytest.mark.unit
def test_only_the_conflicted_remint_asks_for_per_pr_and_by_flag() -> None:
    text = _REMINT.read_text(encoding="utf-8")
    assert '"OCC_COMPANION_BATCH_MODE": ' not in text
    assert re.search(r'"--batch-mode",\s*"off"', text) is not None
