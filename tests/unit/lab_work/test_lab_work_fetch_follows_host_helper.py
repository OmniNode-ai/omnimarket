"""The lab work unit fetch follows the host's own git credential helper (OMN-22478).

The node-issued git token route replaces the stored gh login per host through the host's
credential helper, so the private-repository fetch forces no helper of its own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_shell_work_runner import (
    LocalShellLabWorkExecutor,
)


class _RecordingExecutor(LocalShellLabWorkExecutor):
    def __init__(self, work_root: Path) -> None:
        super().__init__(work_root, url_base="https://example.invalid")
        self.calls: list[tuple[str, ...]] = []

    def _git(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        failed = "fetch" in args or "cat-file" in args
        return subprocess.CompletedProcess(["git", *args], 1 if failed else 0, "", "")


@pytest.mark.unit
def test_the_private_fetch_forces_no_credential_helper(tmp_path: Path) -> None:
    executor = _RecordingExecutor(tmp_path)
    request = ModelLabWorkUnitRequest(
        work_unit_id="unit-0001",
        target_host="h051",
        repo="OmniNode-ai/omnimarket",
        commit_sha="a" * 40,
        argv=["true"],
        lane="lane-a",
    )
    executor.run(request, "h051")
    fetches = [call for call in executor.calls if "fetch" in call]
    assert len(fetches) == 1
    assert not any(word.startswith("credential.helper") for word in fetches[0])
    assert "-c" not in fetches[0]
