# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: every ticket contract loads as omnibase_core's ModelTicketContract.

The same validation ci.yml "Contract Compliance Check" and contract-validation.yml
run, wired as the pre-commit hook ticket-contract-schema so a contract the schema
refuses (for example a dod_evidence id over 50 characters) fails at commit time
instead of after the push.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.errors.model_onex_error import ModelOnexError
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_ID = "ticket-contract-schema"
THIS_TEST = "tests/ci/test_ticket_contracts_schema_omn17427.py"


def _refusal(path: Path) -> str | None:
    """The schema refusal for one contract, collapsed to one line, or None."""
    try:
        ModelTicketContract.from_yaml(path.read_text(encoding="utf-8"))
    except (OSError, ModelOnexError) as exc:
        return " ".join(str(exc).split())
    return None


def test_every_ticket_contract_loads() -> None:
    paths = sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml"))
    assert paths, "contracts/ holds no OMN-*.yaml ticket contract"
    broken = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {reason}"
        for path in paths
        if (reason := _refusal(path)) is not None
    ]
    assert not broken, (
        f"{len(broken)} of {len(paths)} ticket contract(s) fail omnibase_core's "
        "ModelTicketContract:\n" + "\n".join(broken)
    )


def test_id_over_50_chars_is_refused(tmp_path: Path) -> None:
    text = (REPO_ROOT / "contracts/OMN-17427.yaml").read_text(encoding="utf-8")
    text, replacements = re.subn(
        r'(?m)^(  - id: )"dod-[^"]+"$',
        lambda match: f'{match[1]}"{"x" * 51}"',
        text,
        count=1,
    )
    assert replacements == 1
    path = tmp_path / "OMN-17427.yaml"
    path.write_text(text, encoding="utf-8")

    reason = _refusal(path)
    assert reason is not None
    assert "at most 50 characters" in reason


def test_precommit_hook_wired() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == HOOK_ID
    )
    assert THIS_TEST in hook["entry"]
    assert hook["pass_filenames"] is False
    assert re.search(hook["files"], "contracts/OMN-17427.yaml")
    assert re.search(hook["files"], THIS_TEST)
    assert not re.search(hook["files"], "contracts/README.md")
