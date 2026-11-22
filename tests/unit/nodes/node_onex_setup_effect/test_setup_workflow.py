# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure controls for setup reconciliation; these are not product E2E proof.

Failure modes: stale success, no-op provisioning, interrupted effects, concurrent
runs, unreadable state, and secrets in diagnostic/state surfaces. Each effect
below owns a real marker file so replay is judged by observed state, not a
recorded call count alone. Defined before the workflow implementation.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


class MarkerStep:
    """A recoverable file effect with optional faults, never a model/API stand-in."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.marker = root / name
        self.observed = 0
        self.applied = 0
        self.noop = False
        self.failure: str | None = None
        self.pending = False

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        self.observed += 1
        if self.pending:
            return ModelSetupObservation(
                step=self.name, status="action_required", fix="Complete sign-in."
            )
        expected = plan.model_dump_json()
        ready = self.marker.is_file() and self.marker.read_text() == expected
        return ModelSetupObservation(
            step=self.name, status="success" if ready else "unavailable"
        )

    def apply(self, plan: ModelSetupPlan) -> None:
        self.applied += 1
        if self.failure:
            raise RuntimeError(self.failure)
        if not self.noop:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(plan.model_dump_json())


def plan_at(tmp_path: Path, **kwargs: object) -> ModelSetupPlan:
    return ModelSetupPlan(workspace=tmp_path / "workspace with spaces", **kwargs)


def test_replay_reobserves_effect_without_duplicate_mutation(tmp_path: Path) -> None:
    plan = plan_at(tmp_path)
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    engine.handle(plan)
    before = step.observed
    result = engine.handle(plan)
    assert result.observations[-1].status == "success"
    assert step.observed > before
    assert step.applied == 1
    step.marker.unlink()
    engine.handle(plan)
    assert step.marker.is_file(), "saved success must not conceal a missing effect"
    assert step.applied == 2


def test_unimplemented_component_is_refused_before_state_or_machine_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
        HandlerSetupPreflight,
    )

    monkeypatch.setattr(
        HandlerSetupPreflight,
        "observe",
        lambda _self, _plan: ModelSetupObservation(
            step="preflight", status="action_required", fix="Isolated machine boundary."
        ),
    )
    home = tmp_path / "onex"
    engine = HandlerSetupWorkflow(onex_home=home)
    with pytest.raises(ValueError, match="docker"):
        engine.handle(plan_at(tmp_path, components=("docker",)))
    assert not home.exists()


def test_changed_provider_endpoint_reconciles_healthy_old_effect(
    tmp_path: Path,
) -> None:
    step = MarkerStep("models", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    first = engine.handle(
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:18001",
                },
            ),
        )
    )
    second = engine.handle(
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:18002",
                },
            ),
        )
    )
    assert first.desired_digest != second.desired_digest
    assert step.applied == 2
    assert "18002" in step.marker.read_text()


def test_verify_observes_without_creating_state_or_effects(tmp_path: Path) -> None:
    home = tmp_path / "absent"
    step = MarkerStep("workspace", tmp_path / "effects")
    result = HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(
        plan_at(tmp_path, action="verify")
    )
    assert result.observations[-1].status == "unavailable"
    assert step.observed == 1
    assert step.applied == 0
    assert not home.exists()
    assert not step.marker.parent.exists()


def test_noop_effect_never_becomes_success(tmp_path: Path) -> None:
    step = MarkerStep("models", tmp_path / "effects")
    step.noop = True
    later = MarkerStep("dashboard", tmp_path / "effects")
    result = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex", steps=(step, later)
    ).handle(plan_at(tmp_path))
    assert result.observations[-1].status == "unavailable"
    assert step.applied == 1
    assert later.observed == 0


def test_interrupted_effect_resumes_without_replaying_proven_step(
    tmp_path: Path,
) -> None:
    home = tmp_path / "onex"
    early = MarkerStep("workspace", tmp_path / "effects")
    broken = MarkerStep("models", tmp_path / "effects")
    broken.failure = "SECRET_SENTINEL_71"
    later = MarkerStep("dashboard", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=home, steps=(early, broken, later))
    first = engine.handle(plan_at(tmp_path))
    assert [o.status for o in first.observations] == ["success", "failure"]
    assert later.observed == 0
    assert "SECRET_SENTINEL_71" not in first.model_dump_json()
    assert "SECRET_SENTINEL_71" not in (home / "setup.json").read_text()
    broken.failure = None
    resumed = engine.handle(plan_at(tmp_path))
    assert all(o.status == "success" for o in resumed.observations)
    assert early.applied == 1
    assert broken.applied == 2
    assert later.applied == 1


def test_action_required_preserves_human_boundary(tmp_path: Path) -> None:
    step = MarkerStep("claude", tmp_path / "effects")
    step.pending = True
    result = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan_at(tmp_path)
    )
    assert result.observations[-1].status == "action_required"
    assert step.applied == 0


def test_competing_run_refused_before_observation(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    step = MarkerStep("workspace", tmp_path / "effects")
    with (home / "setup.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="concurrent"):
            HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(
                plan_at(tmp_path)
            )
    assert step.observed == 0


def test_legacy_state_imported_once_and_never_used_as_proof(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    (home / "setup.json").write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace with spaces"),
                "install_path": "local",
            }
        )
    )
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=home, steps=(step,))
    migrated = engine.handle(plan_at(tmp_path))
    assert migrated.imported_legacy
    assert migrated.schema_version == 2
    assert step.applied == 1
    again = engine.handle(plan_at(tmp_path))
    assert not again.imported_legacy
    assert step.applied == 1


@pytest.mark.parametrize(
    "raw",
    [
        "broken json",
        '{"schema_version":999}',
        '{"workspace":3}',
        '{"workspace":"/valid","install_path":"local","api_key":"SECRET_SENTINEL_71"}',
    ],
)
def test_unreadable_state_preserved_before_effects(tmp_path: Path, raw: str) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    state = home / "setup.json"
    state.write_text(raw)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(ValueError, match="state"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert state.read_text() == raw
    assert step.observed == 0


def test_atomic_write_failure_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    state = home / "setup.json"
    original = json.dumps(
        {"workspace": str(tmp_path / "workspace"), "install_path": "local"}
    )
    state.write_text(original)

    def refuse_replace(*args: object, **kwargs: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr("os.replace", refuse_replace)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(OSError, match="injected replacement failure"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert state.read_text() == original
    assert sorted(p.name for p in home.iterdir()) == ["setup.json", "setup.lock"]


def test_plan_rejects_secrets_before_effects(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        plan_at(tmp_path, api_key="SECRET_SENTINEL_71")
    with pytest.raises(ValidationError):
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "openai",
                    "models": ("m1",),
                    "api_key": "SECRET_SENTINEL_71",
                },
            ),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:SECRET_SENTINEL_71@localhost:8000",
        "https://localhost:8000/?api_key=SECRET_SENTINEL_71",
        "https://localhost:8000/#SECRET_SENTINEL_71",
    ],
)
def test_plan_rejects_credentials_in_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValidationError):
        plan_at(
            tmp_path, providers=({"provider": "local", "models": ("m1",), "url": url},)
        )


def test_plan_rejects_incomplete_and_conflicting_choices(tmp_path: Path) -> None:
    for fields in (
        {"providers": ({"provider": "local", "models": ("m1",)},)},
        {"providers": ({"provider": "openai", "models": ()},)},
        {"components": (" ",)},
        {"components": ("delegation", "delegation")},
    ):
        with pytest.raises(ValidationError):
            plan_at(tmp_path, **fields)


def test_relative_workspace_and_model_order_are_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = ModelSetupPlan(workspace=Path("relative folder"))
    assert plan.workspace == tmp_path / "relative folder"
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    a = plan_at(tmp_path, providers=({"provider": "openai", "models": ("m1", "m2")},))
    b = plan_at(tmp_path, providers=({"provider": "openai", "models": ("m2", "m1")},))
    assert engine.handle(a).desired_digest == engine.handle(a).desired_digest
    assert engine.handle(a).desired_digest != engine.handle(b).desired_digest


@pytest.mark.parametrize("target", ["setup.json", "setup.lock"])
def test_symlink_state_targets_refused_without_touching_foreign_file(
    tmp_path: Path, target: str
) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_text("preserve me")
    (home / target).symlink_to(foreign)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(ValueError, match="state"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert foreign.read_text() == "preserve me"
    assert step.observed == 0


def test_partial_failure_record_does_not_claim_no_change(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    step.failure = "possibly after mutation"
    result = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan_at(tmp_path)
    )
    assert result.observations[-1].changed


def test_docker_and_native_configuration_distinct(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    native = engine.handle(plan_at(tmp_path))
    docker = engine.handle(plan_at(tmp_path, components=("delegation", "docker")))
    assert native.install_path == "local"
    assert docker.install_path == "docker"
    assert native.desired_digest != docker.desired_digest


def test_no_empty_or_ambiguous_workflow(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    for steps in ((), (step, step), (MarkerStep("", tmp_path),)):
        with pytest.raises(ValueError, match="Setup requires"):
            HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=steps)
    assert not (tmp_path / "onex").exists()


def test_a_refusal_shows_its_own_reason_and_other_errors_stay_generic(
    tmp_path: Path,
) -> None:
    """OMN-23413, seen 2026-11-13 in /delegation: the workspace step refused the
    operator's work folder and the TUI said only "Check the component". A step's
    SetupStepRefusedError is shown; any other exception stays generic (its text
    may carry secrets, see SECRET_SENTINEL_71 above)."""
    from omnimarket.nodes.node_onex_setup_effect.protocols.protocol_setup_step import (
        SetupStepRefusedError,
    )

    class Refusing(MarkerStep):
        def apply(self, plan: ModelSetupPlan) -> None:
            raise SetupStepRefusedError("Use an empty folder for the workspace.")

    step = Refusing("workspace", tmp_path / "effects")
    result = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan_at(tmp_path)
    )
    assert result.observations[-1].status == "failure"
    assert result.observations[-1].fix == "Use an empty folder for the workspace."


def test_setup_in_an_existing_project_preserves_customer_files(tmp_path: Path) -> None:
    """A normal project may contain files before ONEX is installed."""
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    folder = tmp_path / "work root"
    folder.mkdir()
    note = folder / "notes.txt"
    note.write_bytes(b"my project\r\n")
    engine = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex",
        steps=(HandlerSetupEnvironment(name="workspace"),),
    )
    plan = ModelSetupPlan(workspace=folder)
    for changed in (True, False):
        outcome = engine.handle(plan).observations[-1]
        assert outcome.status == "success", outcome.fix
        assert outcome.changed is changed
        assert note.read_bytes() == b"my project\r\n"
        assert (folder / ".onex-setup-workspace").read_text() == "onex setup\n"


@pytest.mark.parametrize(
    "collision", ["workspace_marker", "runtime_config", "config_directory"]
)
def test_setup_refuses_a_collision_in_a_customer_project(
    tmp_path: Path, collision: str
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
        RUNTIME_CONFIG_RELPATH,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    folder = tmp_path / "project"
    folder.mkdir()
    marker = folder / ".onex-setup-workspace"
    runtime = folder / RUNTIME_CONFIG_RELPATH
    user_path = {
        "workspace_marker": marker,
        "runtime_config": runtime,
        "config_directory": folder / RUNTIME_CONFIG_RELPATH.parts[0],
    }[collision]
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_bytes(b"owned by the customer\n")
    result = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex",
        steps=(
            HandlerSetupEnvironment(name="workspace"),
            HandlerSetupEnvironment(name="runtime_config"),
        ),
    ).handle(ModelSetupPlan(workspace=folder))
    outcome = result.observations[-1]
    assert outcome.status in ("failure", "action_required")
    assert user_path.read_bytes() == b"owned by the customer\n"
    if user_path != marker:
        assert not marker.exists()
        # Refused for the collision itself, not because the folder holds files.
        assert str(user_path) in (outcome.fix or "")


def test_repair_does_not_follow_a_link_in_an_existing_workspace(tmp_path: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
        RUNTIME_CONFIG_RELPATH,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    plan = plan_at(tmp_path, action="repair")
    plan.workspace.mkdir()
    (plan.workspace / ".onex-setup-workspace").write_text("onex setup\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    link = plan.workspace / RUNTIME_CONFIG_RELPATH.parts[0]
    link.symlink_to(shared, target_is_directory=True)
    engine = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex",
        steps=(
            HandlerSetupEnvironment(name="workspace"),
            HandlerSetupEnvironment(name="runtime_config"),
        ),
    )
    outcome = engine.handle(plan).observations[-1]
    assert outcome.status == "action_required"
    assert str(link) in (outcome.fix or "")
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize(
    "edited", [False, True], ids=["clean-generated", "customer-edited"]
)
def test_switch_install_path_preserves_customer_edits(
    tmp_path: Path, edited: bool
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
        ensure_runtime_config,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    plan = plan_at(tmp_path)
    config, _ = ensure_runtime_config(plan.workspace, "docker")
    if edited:
        with config.open("a") as stream:
            stream.write("# customer addition made after setup\n")
    before = config.read_bytes()
    outcome = (
        HandlerSetupWorkflow(
            onex_home=tmp_path / "onex",
            steps=(
                HandlerSetupEnvironment(name="workspace"),
                HandlerSetupEnvironment(name="runtime_config"),
            ),
        )
        .handle(plan)
        .observations[-1]
    )
    if edited:
        assert config.read_bytes() == before
        assert outcome.status in ("action_required", "failure")
        assert str(config) in (outcome.fix or "")
    else:
        assert outcome.status == "success", outcome.fix
        assert b"# install-path: local" in config.read_bytes()


def test_a_folder_swapped_for_a_link_after_the_check_is_never_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime config is written into the folder setup checked, not a link's target."""
    from omnimarket.nodes.node_onex_setup_effect.handlers import handler_onex_setup
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    plan = plan_at(tmp_path)
    plan.workspace.mkdir()
    (plan.workspace / ".onex-setup-workspace").write_text("onex setup\n")
    relative = handler_onex_setup.RUNTIME_CONFIG_RELPATH
    top = relative.parts[0]
    (plan.workspace / relative.parent).mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / relative.parent.relative_to(top)).mkdir(parents=True)
    generate = handler_onex_setup._generated_runtime_config

    def swap_then_generate(*args: str) -> str:
        # Runs after setup has checked the folder and before it writes.
        (plan.workspace / top).rename(plan.workspace / "checked")
        (plan.workspace / top).symlink_to(outside, target_is_directory=True)
        return generate(*args)

    monkeypatch.setattr(
        handler_onex_setup, "_generated_runtime_config", swap_then_generate
    )
    HandlerSetupWorkflow(
        onex_home=tmp_path / "onex",
        steps=(HandlerSetupEnvironment(name="runtime_config"),),
    ).handle(plan)
    assert [p for p in outside.rglob("*") if p.is_file()] == []
    checked = plan.workspace / "checked" / relative.relative_to(top)
    assert b"# install-path: local" in checked.read_bytes()


def test_an_edit_saved_after_the_check_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers import handler_onex_setup

    config, _ = handler_onex_setup.ensure_runtime_config(tmp_path, "docker")
    can_switch = handler_onex_setup.runtime_config_can_switch

    def edit_after_check(content: str) -> bool:
        allowed = can_switch(content)
        with config.open("a") as stream:
            stream.write("# customer edit saved after the check\n")
        return allowed

    monkeypatch.setattr(
        handler_onex_setup, "runtime_config_can_switch", edit_after_check
    )
    with pytest.raises(handler_onex_setup.StepFailedError):
        handler_onex_setup.ensure_runtime_config(tmp_path, "local")
    assert config.read_text().endswith("# customer edit saved after the check\n")
    assert "# install-path: docker" in config.read_text()


def _install_sh_runtime_config(path_choice: str) -> str:
    """What the captured install.sh writes, rendered as its shell heredoc would."""
    script = (
        Path(__file__).parents[3]
        / "fixtures/onex_setup/omnibase_install_sh_7e5af9e6.txt"
    ).read_text(encoding="utf-8")
    writer = script.split("write_runtime_config() {", 1)[1]
    body = writer.split('cat > "$tmp" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0] + "\n"
    description = writer.split(f"{path_choice})", 1)[1].split('description="', 1)[1]
    bus_type = writer.split(f"{path_choice})", 1)[1].split('bus_type="', 1)[1]
    values = {
        "$RUNTIME_CONFIG_MARKER": "# generated-by: omnibase install.sh",
        "$path_choice": path_choice,
        "$PYTHON_BIN": "/opt/py/bin/python3.12",
        "$description": description.split('"', 1)[0],
        "$bus_type": bus_type.split('"', 1)[0],
    }
    for name, value in values.items():
        body = body.replace(name, value)
    assert "$" not in body
    return body


@pytest.mark.parametrize(
    "edited", [False, True], ids=["unedited-install-sh", "edited-install-sh"]
)
def test_an_install_sh_runtime_config_switches_unless_edited(
    tmp_path: Path, edited: bool
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
        RUNTIME_CONFIG_RELPATH,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    plan = plan_at(tmp_path)
    plan.workspace.mkdir()
    (plan.workspace / ".onex-setup-workspace").write_text("onex setup\n")
    config = plan.workspace / RUNTIME_CONFIG_RELPATH
    config.parent.mkdir(parents=True)
    written = _install_sh_runtime_config("docker")
    if edited:
        written += "# customer addition\n"
    config.write_text(written, encoding="utf-8")
    outcome = (
        HandlerSetupWorkflow(
            onex_home=tmp_path / "onex",
            steps=(
                HandlerSetupEnvironment(name="workspace"),
                HandlerSetupEnvironment(name="runtime_config"),
            ),
        )
        .handle(plan)
        .observations[-1]
    )
    if edited:
        assert config.read_text(encoding="utf-8") == written
        assert outcome.status == "action_required"
    else:
        assert outcome.status == "success", outcome.fix
        assert "# install-path: local" in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("shape", ["truncated", "hand-edited"])
def test_a_runtime_config_with_no_runtime_definition_needs_attention(
    tmp_path: Path, shape: str
) -> None:
    """Two setup header comments alone are not a runtime config setup can adopt."""
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
        RUNTIME_CONFIG_RELPATH,
        SETUP_MARKER,
        ensure_runtime_config,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
        HandlerSetupEnvironment,
    )

    plan = plan_at(tmp_path)
    config = plan.workspace / RUNTIME_CONFIG_RELPATH
    if shape == "truncated":
        config.parent.mkdir(parents=True)
        config.write_text(f"{SETUP_MARKER}\n# install-path: local\n")
    else:
        ensure_runtime_config(plan.workspace, "local")
        config.write_text(
            config.read_text().replace("max_history: 1000", "max_history: 50")
        )
    before = config.read_bytes()
    step = HandlerSetupEnvironment(name="runtime_config", onex_home=tmp_path / "onex")
    outcome = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan
    )
    assert config.read_bytes() == before
    if shape == "truncated":
        assert step.observe(plan).status == "action_required"
        assert outcome.observations[-1].status == "action_required"
        assert str(config) in (outcome.observations[-1].fix or "")
    else:
        assert outcome.observations[-1].status == "success"
