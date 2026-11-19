# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production collection must distinguish translated/virtual/unknown hosts.

Failures caught: supplied test facts masking absent production probes, unknown
sysctl readings becoming false, VM facts imposing a blanket resource gate, and
commands running on unsupported platforms.
"""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_preflight as preflight,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("translated", "virtual"),
    [("1", "0"), ("0", "1"), ("0", "0"), (None, None), ("unexpected", "unexpected")],
)
def test_production_machine_probes_preserve_unknown_and_report_real_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    translated: str | None,
    virtual: str | None,
) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(preflight.platform, "mac_ver", lambda: ("26.1", (), ""))
    calls = []

    def run(self, request):
        calls.append(request)
        value = {"sysctl.proc_translated": translated, "kern.hv_vmm_present": virtual}[
            request.argv[-1]
        ]
        return ModelSetupCommandResult(
            argv=request.argv,
            returncode=0 if value is not None else 1,
            stdout=value or "",
            stderr="",
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", run)
    result = preflight.collect_machine_facts(tmp_path)
    assert result.rosetta is ({"1": True, "0": False}.get(translated))
    assert result.virtual_machine is ({"1": True, "0": False}.get(virtual))
    assert len(calls) == 2
    assert all(
        c.argv[:2] == ("/usr/sbin/sysctl", "-n") and c.timeout_seconds <= 5
        for c in calls
    )
    observed = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert observed.status == ("action_required" if translated == "1" else "success")
    assert observed.evidence["rosetta"] == {"1": "true", "0": "false"}.get(
        translated, "unknown"
    )
    assert observed.evidence["virtual_machine"] == {"1": "true", "0": "false"}.get(
        virtual, "unknown"
    )


def test_non_mac_observation_never_invokes_mac_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")

    def forbidden(*args):
        raise AssertionError("macOS command on non-macOS host")

    monkeypatch.setattr(HandlerSetupCommand, "handle", forbidden)
    result = preflight.collect_machine_facts(tmp_path)
    assert result.rosetta is None
    assert result.virtual_machine is None
