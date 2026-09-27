# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The autobind publisher groups OCC companions by ticket by default (OMN-16336).

omnimarket#2940 shipped the batch path behind the repository variable
``OMNI_OCC_COMPANION_BATCH_MODE`` with every default reading ``off``. Nobody set
the variable for about 23 hours after the build merged, so every product PR kept
minting its own companion. These tests fail the moment any seam that decides the
companion grouping falls back to one companion per product PR again:

* the publisher every repository's autobind workflow runs (payload, CLI default,
  and the retired environment switch, which now refuses rather than disables);
* the omnimarket inline workflow, which may no longer read the variable, and
  whose closed-unmerged producer runs unconditionally;
* the conflicted-companion re-mint, the one caller allowed to ask for the
  per-PR path, and only by the explicit ``--batch-mode off`` flag.
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

from omnimarket.events.occ_companion import BATCH_READY_REPOS, EnumOccBatchMode
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
def test_publisher_payload_defaults_to_ticket_batch() -> None:
    publisher = _load_publisher()
    payload = publisher.build_payload(_REPO, 42, _TICKET, str(uuid4()))
    assert payload["occ_batch_mode"] == "ticket"
    command = ModelPrLifecycleFixCommand.model_validate(json.loads(json.dumps(payload)))
    assert command.occ_batch_mode is EnumOccBatchMode.TICKET


@pytest.mark.unit
def test_publisher_names_the_per_pr_mode_on_the_wire() -> None:
    """An absent field means ticket, so the per-PR request must be explicit."""
    publisher = _load_publisher()
    payload = publisher.build_payload(
        _REPO, 42, _TICKET, str(uuid4()), batch_mode=EnumOccBatchMode.OFF
    )
    assert payload["occ_batch_mode"] == "off"


@pytest.mark.unit
def test_publisher_cli_defaults_to_ticket_with_no_flag_and_no_env() -> None:
    result = CliRunner().invoke(
        _load_publisher().main, ["--dry-run"], env=_ENV, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert '"occ_batch_mode": "ticket"' in result.output


@pytest.mark.unit
@pytest.mark.parametrize("value", ["off", "OFF", "per-pr"])
def test_retired_env_switch_refuses_instead_of_disabling(value: str) -> None:
    """The old variable must fail loud, never quietly restore per-PR companions."""
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run"],
        env={**_ENV, "OCC_COMPANION_BATCH_MODE": value},
    )
    assert result.exit_code != 0
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
def test_autobind_workflow_no_longer_reads_the_switch() -> None:
    text = _AUTOBIND_WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "OMNI_OCC_COMPANION_BATCH_MODE" not in code
    assert "OCC_COMPANION_BATCH_MODE" not in code
    assert "--batch-mode" not in code
    workflow = yaml.safe_load(text)
    assert "closed" in workflow[True]["pull_request"]["types"]
    gate = workflow["jobs"]["publish-occ-autobind"]["if"]
    assert "vars." not in gate
    assert "!github.event.pull_request.merged" in gate


@pytest.mark.unit
def test_only_the_conflicted_remint_asks_for_per_pr_and_by_flag() -> None:
    text = _REMINT.read_text(encoding="utf-8")
    assert '"OCC_COMPANION_BATCH_MODE": ' not in text
    assert re.search(r'"--batch-mode",\s*"off"', text) is not None


@pytest.mark.unit
def test_publisher_scope_mirrors_the_runtime_scope() -> None:
    assert _load_publisher()._BATCH_READY_REPOS == BATCH_READY_REPOS


@pytest.mark.unit
def test_a_repo_outside_the_scope_publishes_per_pr_and_says_why() -> None:
    """Every repo's reusable job runs this script; only batch-ready repos batch.

    Safe whatever runtime is deployed: a runtime from before the scope existed
    would otherwise batch a repository whose per-PR effect leg still mints.
    """
    result = CliRunner().invoke(
        _load_publisher().main,
        ["--dry-run"],
        env={**_ENV, "PR_REPO": "OmniNode-ai/omnibase_infra"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert '"occ_batch_mode": "off"' in result.output
    assert "::notice::" in result.output
    assert "BATCH_READY_REPOS" in result.output
    assert "::warning::" not in result.output
