# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-brief check's key is filled from the secret store, never from a file or the unit (OMN-24239).

The seam is store entry -> the node's overlay -> the preparer's environment -> the ticket-text read of ``lane_brief.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    LaneBriefPreparer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.store_environment import (
    StoreEnvironmentFiller,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.handlers.handler_host_drift_owner_dispatch import (
    render_brief,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelDriftOwnerOverlay,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    BriefEnvironmentAbsentError,
    DriftStateUnreadableError,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.runtime_host_drift_owner_dispatch import (
    build_handler,
    main,
)

from .support import NODE, item, overlay

pytestmark = pytest.mark.unit

ENV = "EXAMPLE_LINEAR_KEY"
FOLDER = "/example/lane-env"
PLANTED = "planted-store-value-1"
CAUSE = "host-drift:h-example:env-file:host-env"

# A stand-in for lane_brief.py whose ticket-text read needs the key in its environment, as the real read needs the Linear key.
LANE_BRIEF = f"""
import os, sys
args = sys.argv[1:]
sub = args[args.index("--ledger") + 2]
if sub == "ticket-text":
    if not os.environ.get("{ENV}"):
        print("ticket-text read needs the Linear key", file=sys.stderr)
        sys.exit(1)
    print("ticket-text: OMN-1 host drift")
elif sub == "constraints":
    print("## 3a. Constraints")
"""
RULES = "print('# Standing rules for every dispatched lane')"


class FakeStore:
    """The store seam: values by entry name, every read recorded, optionally a read that fails with the value in its text."""

    def __init__(self, values: dict[str, str] | None = None, *, fail: bool = False):
        self.values = values or {}
        self.fail = fail
        self.reads: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.reads.append((folder, name))
        if self.fail:
            raise RuntimeError(f"store refused {PLANTED}")
        value = self.values.get(name)
        return SecretStr(value) if value is not None else None


def _filler(store: FakeStore) -> StoreEnvironmentFiller:
    return StoreEnvironmentFiller(overlay().brief_environment, store)


def test_a_name_the_unit_lacks_is_filled_from_the_store_in_a_copy_only() -> None:
    environ = {"HOME": "/example/home"}
    filled = _filler(FakeStore({ENV: PLANTED})).fill(environ)
    assert filled[ENV] == PLANTED
    assert filled["HOME"] == "/example/home"
    assert ENV not in environ
    assert ENV not in os.environ


def test_a_name_the_unit_holds_is_kept_and_the_store_is_not_asked() -> None:
    store = FakeStore({ENV: PLANTED})
    filled = _filler(store).fill({ENV: "from-the-unit"})
    assert filled[ENV] == "from-the-unit"
    assert store.reads == []


def test_the_store_is_asked_for_the_overlays_folder_and_entry() -> None:
    store = FakeStore({ENV: PLANTED})
    _filler(store).fill({})
    assert store.reads == [(FOLDER, ENV)]


def test_an_entry_the_store_lacks_is_refused_naming_the_name_and_folder() -> None:
    with pytest.raises(BriefEnvironmentAbsentError) as refused:
        _filler(FakeStore({})).fill({})
    assert ENV in str(refused.value)
    assert FOLDER in str(refused.value)
    assert "absent" in str(refused.value)


def test_a_blank_entry_is_refused_like_an_absent_one() -> None:
    with pytest.raises(BriefEnvironmentAbsentError):
        _filler(FakeStore({ENV: "  "})).fill({})


def test_a_failing_store_read_is_refused_naming_the_error_class_and_never_its_text() -> (
    None
):
    with pytest.raises(BriefEnvironmentAbsentError) as refused:
        _filler(FakeStore({ENV: PLANTED}, fail=True)).fill({})
    assert "RuntimeError" in str(refused.value)
    assert PLANTED not in str(refused.value)


def _plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "skills/lane-brief/scripts").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "skills/lane-brief/scripts/lane_brief.py").write_text(LANE_BRIEF)
    (root / "scripts/lane_rules_block.py").write_text(RULES)
    return root


def _prepare(tmp_path: Path, environ: dict[str, str]) -> tuple[bool, str]:
    preparer = LaneBriefPreparer(
        tmp_path / "briefs",
        _plugin(tmp_path),
        overlay().brief,
        model="sonnet",
        ticket="OMN-1",
        environ={**environ, "EXAMPLE_LEDGER_PATH": str(tmp_path / "ledger.md")},
    )
    task = render_brief(
        item(),
        CAUSE,
        "host-drift-x-20261113T1000Z",
        "2026-11-13T16:00:00Z",
        overlay().brief.sections,
    )
    done = preparer.prepare(lane="host-drift-x", name="x-brief.md", text=task)
    return done.passed, done.detail


def test_seam_the_ticket_text_read_passes_with_the_filled_environment(
    tmp_path: Path,
) -> None:
    filled = _filler(FakeStore({ENV: PLANTED})).fill({})
    passed, detail = _prepare(tmp_path, filled)
    assert passed, detail
    text = (tmp_path / "briefs/x-brief.md").read_text(encoding="utf-8")
    assert "ticket-text: OMN-1 host drift" in text


def test_seam_failing_control_the_unfilled_environment_does_not_pass_the_ticket_text_read(
    tmp_path: Path,
) -> None:
    passed, detail = _prepare(tmp_path, {})
    assert not passed
    assert "ticket-text" in detail
    assert not (tmp_path / "briefs/x-brief.md").exists()


def _runtime(tmp_path: Path) -> tuple[ModelDriftOwnerOverlay, dict[str, str]]:
    runner = tmp_path / "plugin/skills/remote-lane/scripts/onex_remote_lane.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("")
    environ = {
        "EXAMPLE_REMOTE_LANE_RUNNER": str(runner),
        "EXAMPLE_LANE_CLAUDE_DIR": str(tmp_path / "lane-claude"),
        "EXAMPLE_LEDGER_PATH": str(tmp_path / "ledger.md"),
    }
    return overlay().model_copy(update={"backend": "runtime"}), environ


def test_the_runtime_backend_hands_the_preparer_the_filled_environment(
    tmp_path: Path,
) -> None:
    changed, environ = _runtime(tmp_path)
    handler = build_handler(
        changed,
        root=tmp_path,
        environ=environ,
        store=FakeStore({ENV: PLANTED}),
    )
    preparer = handler._briefs
    assert isinstance(preparer, LaneBriefPreparer)
    assert preparer._environ[ENV] == PLANTED
    assert ENV not in environ


def test_the_runtime_backend_with_no_store_entry_is_refused_before_any_item_is_read(
    tmp_path: Path,
) -> None:
    changed, environ = _runtime(tmp_path)
    with pytest.raises(BriefEnvironmentAbsentError) as refused:
        build_handler(changed, root=tmp_path, environ=environ, store=FakeStore({}))
    assert ENV in str(refused.value)


def test_the_inmemory_backend_never_reads_the_store(tmp_path: Path) -> None:
    store = FakeStore({})
    build_handler(overlay(), root=tmp_path, environ={}, store=store)
    assert store.reads == []


def test_a_pass_with_the_store_entry_absent_ends_unreadable_and_prints_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, environ = _runtime(tmp_path)
    data = yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    data["backend"] = "runtime"
    root = tmp_path / "overlays/node_host_drift_owner_dispatch_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_drift_owner_dispatch_effect.runtime_host_drift_owner_dispatch._default_store",
        lambda _overlay, _environ: FakeStore({}),
    )
    code = main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")])
    said = capsys.readouterr().out
    assert code == 3
    assert "UNREADABLE" in said
    assert ENV in said


@pytest.mark.parametrize("missing", ["secret_store", "brief_environment"])
def test_an_overlay_without_the_store_blocks_is_refused_and_names_the_key(
    missing: str,
) -> None:
    data = yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del data[missing]
    with pytest.raises(ValidationError) as refused:
        ModelDriftOwnerOverlay.model_validate(data)
    assert missing in str(refused.value)


def test_the_runtime_backend_of_an_overlay_naming_no_claude_dir_variable_builds_without_one(
    tmp_path: Path,
) -> None:
    changed, environ = _runtime(tmp_path)
    del environ["EXAMPLE_LANE_CLAUDE_DIR"]
    named = changed.dispatch.model_copy(update={"claude_dir_env": "", "plugins": ()})
    handler = build_handler(
        changed.model_copy(update={"dispatch": named}),
        root=tmp_path,
        environ=environ,
        store=FakeStore({ENV: PLANTED}),
    )
    assert handler._claude_dir == ""
    # failing control: the overlay that names the variable, with it unset, is refused
    with pytest.raises(DriftStateUnreadableError, match="EXAMPLE_LANE_CLAUDE_DIR"):
        build_handler(
            changed, root=tmp_path, environ=environ, store=FakeStore({ENV: PLANTED})
        )
