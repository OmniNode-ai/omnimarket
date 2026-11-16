# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review key credit probe is scheduled by a declared launchd unit of this repository (OMN-24656).

The probe (``onex models review-gateway-key-probe``) reads each review key bound to the Mac with one token and
appends a ledger row when a key's credit state flips. Its schedule had no home outside the frozen internal
repository. It is now the host installer's ``review-key-credit-probe`` registry unit, shaped like the Claude
token pool rotation tick; its settings (tenant, model, host) are the node's overlay, never the unit's. These
tests read the declaration, the unit it renders, and one tick of the module the unit runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_claude_review_gateway_effect import (
    runtime_review_key_credit_probe_tick as tick,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_review_key_probe_overlay import (
    ModelReviewKeyProbeOverlay,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    Recorder,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

UNIT = "review-key-credit-probe"
LABEL = "ai.omninode.review-key-credit-probe"
NODE = "node_claude_review_gateway_effect"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
BREW_PYTHON = "/opt/homebrew/bin/python3.13"
ROOT = Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == BREW_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


# --- the schedule is declared -----------------------------------------------------------------


def test_the_schedule_is_declared_as_a_launchd_tick_with_a_bounded_run() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["kind"] == "registry_module_tick"
    assert unit["label"] == LABEL
    assert unit["schedule"]["start_interval"] > 0
    # A hung probe ends before the next tick is due.
    assert 0 < unit["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert (ROOT / unit["entry_file"]).is_file()
    assert unit["module"] == unit["entry_file"].removeprefix("src/").removesuffix(
        ".py"
    ).replace("/", ".")
    assert unit["module"].startswith(f"omnimarket.nodes.{NODE}.")
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment_path_overrides"] == ["ONEX_SKILL_OVERLAY_ROOTS"]


def test_the_unit_holds_none_of_the_nodes_settings_and_no_credential() -> None:
    text = json.dumps(load_units()[UNIT])
    for setting in ("tenant", "claude-", "api_key", "secret", "password"):
        assert setting not in text.lower()


def test_the_schedule_matches_the_pool_rotation_tick_exemplar() -> None:
    units = load_units()
    ours, exemplar = units[UNIT], units["claude-pool-rotation-tick"]
    for key in (
        "platform",
        "kind",
        "template",
        "registry_project",
        "tick_no_sync",
        "interpreter_candidates",
        "project_path_override",
    ):
        assert ours[key] == exemplar[key], key


def test_the_rendered_unit_runs_the_tick_module_on_the_declared_interval(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        )
    )
    assert result.status == "success", result.stderr
    assert f"<string>{LABEL}</string>" in result.rendered
    assert f"<string>{unit['module']}</string>" in result.rendered
    assert f"<integer>{unit['schedule']['start_interval']}</integer>" in result.rendered
    assert "ONEX_SKILL_OVERLAY_ROOTS" in result.rendered


# --- one tick ---------------------------------------------------------------------------------


def _overlay_root(tmp_path: Path, document: dict[str, str]) -> Path:
    home = tmp_path / "overlays" / NODE
    home.mkdir(parents=True)
    (home / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return tmp_path / "overlays"


class _Runner:
    def __init__(self, code: int = 0) -> None:
        self.argv: list[str] = []
        self.code = code

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        return subprocess.CompletedProcess(
            argv, self.code, "review-key-probe: 0 flip(s) written\n", ""
        )


def test_a_tick_runs_the_probe_command_with_the_overlay_and_writes_its_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _overlay_root(
        tmp_path, {"tenant_id": "tenant-1", "model": "model-1", "host": "host-1"}
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    runner, state = _Runner(), tmp_path / "state"
    assert (
        tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)], run=runner)
        == 0
    )
    assert runner.argv[:3] == ["onex", "models", "review-gateway-key-probe"]
    for pair in (
        ["--tenant", "tenant-1"],
        ["--model", "model-1"],
        ["--host", "host-1"],
    ):
        assert any(
            runner.argv[i : i + 2] == pair for i in range(len(runner.argv) - 1)
        ), pair
    assert "--ledger" not in runner.argv
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 0
    assert "tenant-1" not in json.dumps(evidence)


def test_a_tick_whose_probe_fails_exits_nonzero_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _overlay_root(tmp_path, {"tenant_id": "tenant-1", "model": "model-1"})
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    state = tmp_path / "state"
    assert (
        tick.main(
            ["--omni-home", str(tmp_path), "--state-dir", str(state)], run=_Runner(1)
        )
        == 1
    )
    assert (
        json.loads((state / "last-run.json").read_text(encoding="utf-8"))["exit_code"]
        == 1
    )


def test_a_tick_with_no_overlay_runs_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    runner, state = _Runner(), tmp_path / "state"
    assert (
        tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)], run=runner)
        == 3
    )
    assert runner.argv == []
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 3


def test_the_probe_argument_does_a_pass_of_nothing(tmp_path: Path) -> None:
    runner = _Runner()
    assert tick.main(["--probe"], run=runner) == 0
    assert runner.argv == []


def test_the_overlay_refuses_a_missing_tenant_or_model() -> None:
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate({"model": "model-1"})
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate({"tenant_id": "tenant-1"})
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate(
            {"tenant_id": "t", "model": "m", "extra": "x"}
        )


def test_the_overlay_example_loads_through_the_model() -> None:
    document = yaml.safe_load(
        (NODE_DIR / "review_key_probe.overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelReviewKeyProbeOverlay.model_validate(document).model
