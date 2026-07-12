# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RT-5 mirror acceptance: the deploy-trigger producer fails closed on zero output.

The defect (same disease RT-5 killed in omnibase_infra, OMN-14467): with the
KAFKA/HMAC secrets absent, ``scripts/trigger_rebuild_on_merge.py`` printed
``"KAFKA_BOOTSTRAP_SERVERS is not set -- skipping publish"`` and exited 0 — GREEN
— even on a real runtime change. This suite pins the fix: when a runtime change
is detected and this is not a dry run, a missing publish precondition (the
exists-but-WRONG case) or a zero-delivery emit must go RED (non-zero exit).

The legit no-op paths — no runtime change, or ``--dry-run`` — still exit 0.

Ticket: OMN-14470 (RT-5 mirror); sibling OMN-14467; epic OMN-13674.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "trigger_rebuild_on_merge.py"

# Producers that MUST share the RT-5 fail-closed invariant.
_PRODUCER_SCRIPTS: list[Path] = [SCRIPT_PATH]

_BANNED_SILENT_SKIP_PHRASE = "skipping publish"

_RUNTIME_CHANGE_FILE = "src/omnimarket/nodes/node_runtime_sweep/handler.py"
_PUBLISH_CREDS_ENV = (
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "DEPLOY_AGENT_HMAC_SECRET",
)


def _load_trigger_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "trigger_rebuild_on_merge", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trigger_rebuild_on_merge"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trigger_module() -> Any:
    return _load_trigger_module()


def _operative_code(path: Path) -> str:
    """Return source with comment lines stripped (executable code only)."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _clear_publish_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PUBLISH_CREDS_ENV:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# The acceptance bar: RED reproduces against exists-but-WRONG                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_runtime_change_with_broker_unset_fails_closed(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker unset + real runtime change + not dry-run => RED (was exit 0 green)."""
    _clear_publish_env(monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        [
            "--changed-files",
            _RUNTIME_CHANGE_FILE,
            "--correlation-id",
            "corr-red",
        ],
    )

    assert result.exit_code != 0, result.output
    # It got PAST the trigger decision (was a producer expected to emit).
    assert "Rebuild triggered" in result.output
    # And it did NOT take the deleted silent-skip path.
    assert _BANNED_SILENT_SKIP_PHRASE not in result.output


@pytest.mark.unit
def test_zero_delivery_emit_fails_closed(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed publish that delivered zero events must go RED."""
    for name in _PUBLISH_CREDS_ENV:
        monkeypatch.setenv(name, "present")

    monkeypatch.setattr(trigger_module, "publish_rebuild_event", lambda **_kwargs: 0)

    result = CliRunner().invoke(
        trigger_module.main,
        ["--changed-files", _RUNTIME_CHANGE_FILE, "--correlation-id", "corr-zero"],
    )

    assert result.exit_code != 0, result.output
    assert "emitted 0" in result.output


# --------------------------------------------------------------------------- #
# Legit no-op paths still exit 0                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_runtime_change_exits_zero(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_publish_env(monkeypatch)

    result = CliRunner().invoke(
        trigger_module.main,
        ["--changed-files", "README.md,docs/thing.md"],
    )

    assert result.exit_code == 0, result.output
    assert "No rebuild trigger" in result.output


@pytest.mark.unit
def test_dry_run_with_runtime_change_exits_zero(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_publish_env(monkeypatch)

    def _explode(**_kwargs: Any) -> int:
        raise AssertionError("dry-run must not publish")

    monkeypatch.setattr(trigger_module, "publish_rebuild_event", _explode)

    result = CliRunner().invoke(
        trigger_module.main,
        ["--changed-files", _RUNTIME_CHANGE_FILE, "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


@pytest.mark.unit
def test_happy_path_publishes_and_exits_zero(
    trigger_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _PUBLISH_CREDS_ENV:
        monkeypatch.setenv(name, "present")

    monkeypatch.setattr(trigger_module, "publish_rebuild_event", lambda **_kwargs: 1)

    result = CliRunner().invoke(
        trigger_module.main,
        ["--changed-files", _RUNTIME_CHANGE_FILE, "--correlation-id", "corr-ok"],
    )

    assert result.exit_code == 0, result.output
    assert "Published rebuild-requested" in result.output
    assert "delivered=1" in result.output


# --------------------------------------------------------------------------- #
# Anti-regression policy gate (Rule 5: enforcement, not detection)            #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("script_path", _PRODUCER_SCRIPTS, ids=lambda p: p.name)
def test_producer_has_no_silent_skip_path(script_path: Path) -> None:
    """No producer may reintroduce a print-then-exit-0 silent skip in code."""
    code = _operative_code(script_path)
    assert _BANNED_SILENT_SKIP_PHRASE not in code, (
        f"{script_path.name} contains a silent-skip path "
        f"({_BANNED_SILENT_SKIP_PHRASE!r}); producers must fail closed on zero "
        f"output (RT-5), not skip green."
    )


@pytest.mark.unit
@pytest.mark.parametrize("script_path", _PRODUCER_SCRIPTS, ids=lambda p: p.name)
def test_producer_uses_shared_effect_assertion(script_path: Path) -> None:
    """Every producer must wire the shared RT-5 fail-closed assertion."""
    code = _operative_code(script_path)
    assert "require_producer_preconditions" in code, (
        f"{script_path.name} must gate its emit through "
        f"require_producer_preconditions (RT-5 shared invariant)."
    )
    assert "assert_producer_emitted" in code, (
        f"{script_path.name} must assert a non-zero emit count via "
        f"assert_producer_emitted (RT-5 shared invariant)."
    )
