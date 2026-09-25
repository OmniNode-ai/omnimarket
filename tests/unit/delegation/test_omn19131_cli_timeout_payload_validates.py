# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegate --timeout` builds a payload the installed request model accepts (OMN-19131).

**The measured defect.** Between 2026-09-21 and 2026-09-22 every
`onex delegate --timeout` run failed, 48 of 48, before anything was published.
The CLI in `omnibase_infra` wrote `requested_timeout_seconds` into the request
payload, and the request model this repository shipped declared
`extra="forbid"` and no such field, so `RuntimeLocal._build_initial_payload`
refused the payload with `extra_forbidden`. Each side was tested on its own
and each side's tests were green: the CLI's tests asserted the key was written,
and this repository's tests asserted the model's own field set. Nothing tested
the seam, so the two drifted apart and every flagged run died at it.

**What this test pins.** The seam itself, as the runtime crosses it:

1. the CLI's own payload writer (`omnibase_infra.cli.cli_delegate._write_payload`,
   the function `onex delegate` calls), asked for the payload of a run passing
   `--timeout 300`;
2. the request model resolved from THIS repository's packaged
   `node_delegate_skill_orchestrator/contract.yaml`, the file the CLI resolves
   and the runtime reads, never a model imported by hand;
3. `RuntimeLocal._build_initial_payload`, the exact call that raised on the
   failing runs, validating (1) against (2).

**Falsifier (AC5).** Remove `requested_timeout_seconds` from
`ModelDelegateSkillRequest` and this test fails with the runtime's own
`does not validate against` refusal. That was run against this branch before it
was pushed; the pull request body carries the output.

The positive control is the writer's own output: the test first asserts the key
IS in the written payload, so a CLI that silently stopped writing it cannot make
this test pass by skipping the field it exists to check.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import yaml
from omnibase_infra.cli.cli_delegate import _write_payload
from pydantic import BaseModel

import omnimarket
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

_DELEGATE_CONTRACT = (
    Path(omnimarket.__file__).resolve().parent
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

_REQUESTED_TIMEOUT_SECONDS = 300


def _cli_timeout_payload(tmp_path: Path) -> Path:
    """The payload file `onex delegate ... --timeout 300` writes."""
    return _write_payload(
        prompt="Reply with exactly the word READY",
        task_type="summarization",
        source="claude-code",
        max_tokens=None,
        state_root=tmp_path,
        run_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        requested_timeout_seconds=_REQUESTED_TIMEOUT_SECONDS,
    )


def test_the_cli_writes_the_timeout_field(tmp_path: Path) -> None:
    """Positive control: the path under test really carries the field."""
    payload = json.loads(_cli_timeout_payload(tmp_path).read_text(encoding="utf-8"))

    assert payload["requested_timeout_seconds"] == _REQUESTED_TIMEOUT_SECONDS


def test_the_installed_request_model_accepts_the_cli_timeout_payload(
    tmp_path: Path,
) -> None:
    """AC5: the runtime's own pre-publish validation accepts a `--timeout` payload."""
    payload_path = _cli_timeout_payload(tmp_path)
    contract = yaml.safe_load(_DELEGATE_CONTRACT.read_text(encoding="utf-8"))
    spec = RuntimeLocal._coerce_model_spec(contract["input_model"])
    assert spec is not None, "the delegate contract names no input_model"

    runtime = RuntimeLocal(
        workflow_path=_DELEGATE_CONTRACT,
        state_root=tmp_path / "state",
        input_path=payload_path,
    )
    request = runtime._build_initial_payload(spec)

    assert isinstance(request, BaseModel)
    assert type(request).__name__ == str(spec["class"])
    assert request.model_dump()["requested_timeout_seconds"] == (
        _REQUESTED_TIMEOUT_SECONDS
    )
