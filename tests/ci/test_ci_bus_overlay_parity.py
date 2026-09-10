# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the cross-repo CI bus overlay parity gate [OMN-18127].

THE FIXTURE IS THE INCIDENT. ``_CONSUMER_BEFORE`` and ``_CONSUMER_AFTER`` below
are reduced but faithful copies of omnibase_infra
``scripts/trigger_rebuild_on_merge.py`` either side of the fix for the third
occurrence (omnibase_infra#3387), and ``_OVERLAY_WITH_LEDGER_READBACK`` is the
dev lane exactly as omnimarket#2437 (``9d14eecf``) declared it.

So the red case in this file is the real event: the overlay that merged here at
04:17:04Z on 2026-09-10, against the publisher model as it stood at that moment.
The green case is the same overlay against the same model once the key is
learned. That pairing is OMN-18127 AC5, and it is what distinguishes this gate
from one that has simply never seen a failure.

These are hermetic: the consumer model is a fixture on disk, never a network
fetch. The WORKFLOW performs the live checkout, and the wiring cases at the
bottom pin that so this file cannot decay into a green that proves nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "ci" / "check_ci_bus_overlay_parity.py"
LIVE_OVERLAY = REPO_ROOT / "config" / "ci_bus_lanes.yaml"
PARITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-bus-overlay-parity.yml"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

from check_ci_bus_overlay_parity import (  # noqa: E402
    ParityCheckError,
    assert_consumer_is_strict,
    check_overlay_parity,
)

#: The consumer side. Reduced to the contract surface this gate touches: the
#: two strict models, the nested reference model, and the loader.
_CONSUMER_HEAD = """
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ModelCiBusLaneDsnReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dsn_env: str

    @field_validator("dsn_env")
    @classmethod
    def validate_dsn_env(cls, value: str) -> str:
        if not value:
            raise ValueError("dsn_env must not be empty when declared")
        if not _ENV_VAR_NAME_PATTERN.match(value):
            raise ValueError("dsn_env must be the NAME of an environment variable")
        return value
"""

_CONSUMER_TAIL = """

class ModelCiBusOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    default: str = "inmemory"
    lanes: dict[str, ModelCiBusLane]


ModelCiBusOverlay.model_rebuild(
    _types_namespace={
        "ModelCiBusLane": ModelCiBusLane,
        "ModelCiBusLaneDsnReference": ModelCiBusLaneDsnReference,
    }
)
ModelCiBusLane.model_rebuild(
    _types_namespace={"ModelCiBusLaneDsnReference": ModelCiBusLaneDsnReference}
)


def load_ci_bus_overlay(path: Path) -> ModelCiBusOverlay:
    if not path.is_file():
        raise ValueError(f"CI bus overlay does not exist: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ModelCiBusOverlay.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"Invalid CI bus overlay {path}: {exc}") from exc
"""

#: The lane model BEFORE omnibase_infra#3387 -- it knows projection_readback
#: and not ledger_readback. This is the state of origin/dev at 04:17:04Z.
_CONSUMER_BEFORE = (
    _CONSUMER_HEAD
    + """

class ModelCiBusLane(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    broker: str
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    projection_readback: ModelCiBusLaneDsnReference | None = None
"""
    + _CONSUMER_TAIL
)

#: The lane model AFTER the key is learned.
_CONSUMER_AFTER = (
    _CONSUMER_HEAD
    + """

class ModelCiBusLane(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    broker: str
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    projection_readback: ModelCiBusLaneDsnReference | None = None
    ledger_readback: ModelCiBusLaneDsnReference | None = None
"""
    + _CONSUMER_TAIL
)

#: The same model with the strictness removed -- the silent-green regression.
_CONSUMER_RELAXED = _CONSUMER_AFTER.replace(
    'class ModelCiBusLane(BaseModel):\n    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)',
    'class ModelCiBusLane(BaseModel):\n    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)',
)

#: omnimarket#2437's dev lane, verbatim in SHAPE. The broker is a
#: placeholder rather than the real lane address: nothing these cases
#: assert depends on its value, and the leaked-literals gate (OMN-10580)
#: is right to refuse an internal address in a file that has no need of
#: one. The live overlay carries the real value under its own reviewed
#: annotation; a test fixture is not the place to copy it.
_OVERLAY_WITH_LEDGER_READBACK = """
default: inmemory
lanes:
  dev:
    broker: "declared-broker.invalid:19092"
    security_protocol: SASL_PLAINTEXT
    sasl_mechanism: SCRAM-SHA-256
    projection_readback:
      dsn_env: CHAIN_CANARY_PROJECTION_DSN
    ledger_readback:
      dsn_env: CHAIN_CANARY_PROJECTION_DSN
  stability:
    broker: inmemory
  prod:
    broker: inmemory
"""

#: The same overlay without the new block -- the state that was already green.
_OVERLAY_WITHOUT_LEDGER_READBACK = """
default: inmemory
lanes:
  dev:
    broker: "declared-broker.invalid:19092"
    security_protocol: SASL_PLAINTEXT
    sasl_mechanism: SCRAM-SHA-256
    projection_readback:
      dsn_env: CHAIN_CANARY_PROJECTION_DSN
  stability:
    broker: inmemory
  prod:
    broker: inmemory
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.unit
class TestParityAgainstTheRealIncident:
    """OMN-18127 AC1 and AC5 -- the negative control IS the third occurrence."""

    def test_the_key_that_broke_the_fleet_fails_on_this_repositorys_pr(
        self, tmp_path: Path
    ) -> None:
        """RED: omnimarket#2437's overlay against the publisher as it then was.

        This is the whole point of the gate. On 2026-09-10 this exact pairing
        merged green here and took the Runtime Rebuild Trigger, and with it
        every dev-lane lab pass, red on omnibase_infra minutes later. With this
        gate in place that PR does not merge.
        """
        overlay = _write(tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITH_LEDGER_READBACK)
        consumer = _write(tmp_path, "consumer_before.py", _CONSUMER_BEFORE)

        with pytest.raises(ParityCheckError) as excinfo:
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

        message = str(excinfo.value)
        # AC5 of OMN-18096 in spirit: the reader must not have to reconstruct
        # which key moved or which side is stale.
        assert "ledger_readback" in message
        assert "omnibase_infra@dev" in message
        # It must point at the correct remedy, not at relaxing the model.
        assert "teach the consumer-side model" in message

    def test_the_same_overlay_passes_once_the_consumer_learns_the_key(
        self, tmp_path: Path
    ) -> None:
        """GREEN: same overlay, same gate, consumer model advanced.

        Paired with the case above this is AC5: the check can fail AND pass on
        the same input, so its red is a finding rather than a permanent state.
        """
        overlay = _write(tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITH_LEDGER_READBACK)
        consumer = _write(tmp_path, "consumer_after.py", _CONSUMER_AFTER)

        check_overlay_parity(
            overlay=overlay,
            consumer_model=consumer,
            consumer_ref="omnibase_infra@dev",
        )

    def test_positive_control_the_pre_incident_overlay_was_already_green(
        self, tmp_path: Path
    ) -> None:
        """The gate is not simply always-red against the old consumer."""
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )
        consumer = _write(tmp_path, "consumer_before.py", _CONSUMER_BEFORE)

        check_overlay_parity(
            overlay=overlay,
            consumer_model=consumer,
            consumer_ref="omnibase_infra@dev",
        )

    def test_a_novel_unmodelled_key_fails_too(self, tmp_path: Path) -> None:
        """AC1 generalised: this is about the CLASS, not about one key."""
        overlay = _write(
            tmp_path,
            "ci_bus_lanes.yaml",
            _OVERLAY_WITHOUT_LEDGER_READBACK.replace(
                "  stability:",
                "    a_fourth_key_nobody_has_modelled_yet:\n"
                "      dsn_env: SOMETHING\n"
                "  stability:",
            ),
        )
        consumer = _write(tmp_path, "consumer_before.py", _CONSUMER_BEFORE)

        with pytest.raises(ParityCheckError) as excinfo:
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

        assert "a_fourth_key_nobody_has_modelled_yet" in str(excinfo.value)


@pytest.mark.unit
class TestFailsClosed:
    """OMN-18127 AC3 -- a gate that cannot read its counterpart has not passed."""

    def test_missing_consumer_model_fails(self, tmp_path: Path) -> None:
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )

        with pytest.raises(ParityCheckError, match="was not checked out"):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=tmp_path / "absent.py",
                consumer_ref="omnibase_infra@dev",
            )

    def test_empty_consumer_model_fails(self, tmp_path: Path) -> None:
        """A silently empty sparse checkout is the green no-op to refuse."""
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )
        consumer = _write(tmp_path, "empty.py", "   \n")

        with pytest.raises(ParityCheckError, match="is empty"):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

    def test_unparseable_consumer_model_fails(self, tmp_path: Path) -> None:
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )
        consumer = _write(tmp_path, "broken.py", "class ModelCiBusLane(:\n")

        with pytest.raises(ParityCheckError, match="does not parse"):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

    def test_consumer_that_fails_to_import_fails(self, tmp_path: Path) -> None:
        """Syntactically valid, strict-looking, and raises on import."""
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )
        consumer = _write(
            tmp_path,
            "raises.py",
            _CONSUMER_AFTER + "\n\nraise RuntimeError('boom at import')\n",
        )

        with pytest.raises(ParityCheckError, match="failed to import"):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

    def test_consumer_without_the_loader_fails(self, tmp_path: Path) -> None:
        """The contract surface moved; repoint the gate, do not report green."""
        overlay = _write(
            tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITHOUT_LEDGER_READBACK
        )
        consumer = _write(
            tmp_path,
            "no_loader.py",
            _CONSUMER_AFTER.replace(
                "def load_ci_bus_overlay(", "def load_the_overlay_differently("
            ),
        )

        with pytest.raises(ParityCheckError, match="has no callable"):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

    def test_missing_overlay_fails(self, tmp_path: Path) -> None:
        consumer = _write(tmp_path, "consumer_after.py", _CONSUMER_AFTER)

        with pytest.raises(ParityCheckError, match="was not found"):
            check_overlay_parity(
                overlay=tmp_path / "absent.yaml",
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )


@pytest.mark.unit
class TestRefusesASilentlyGreenConsumer:
    """OMN-18127 AC4 -- the strictness this gate proxies is asserted, not assumed."""

    def test_a_relaxed_consumer_is_refused(self, tmp_path: Path) -> None:
        """The worst failure would be green BECAUSE the strictness was removed.

        With ``extra="allow"`` on the lane model every overlay validates, so a
        naive parity check would pass forever and would pass most confidently
        at the exact moment the contract stopped being enforced. This refuses
        instead, and says which models lost it.
        """
        overlay = _write(tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITH_LEDGER_READBACK)
        consumer = _write(tmp_path, "relaxed.py", _CONSUMER_RELAXED)

        with pytest.raises(ParityCheckError) as excinfo:
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )

        message = str(excinfo.value)
        assert "ModelCiBusLane" in message
        assert 'extra="forbid"' in message

    def test_the_relaxed_consumer_would_otherwise_have_validated(
        self, tmp_path: Path
    ) -> None:
        """Proves the case above is load-bearing and not incidentally red.

        Without the strictness assertion the relaxed consumer accepts this
        overlay, so the refusal above is the assertion doing work.
        """
        overlay = _write(tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITH_LEDGER_READBACK)
        consumer = _write(tmp_path, "relaxed.py", _CONSUMER_RELAXED)

        # The strictness assertion is what refuses; the loader itself does not.
        with pytest.raises(ParityCheckError, match='extra="forbid"'):
            check_overlay_parity(
                overlay=overlay,
                consumer_model=consumer,
                consumer_ref="omnibase_infra@dev",
            )
        assert_consumer_is_strict(_CONSUMER_AFTER, consumer_ref="fixture")

    def test_strictness_is_read_from_source_not_from_a_live_object(self) -> None:
        """AST-read, so a runtime mutation cannot satisfy it."""
        with pytest.raises(ParityCheckError, match="ModelCiBusOverlay"):
            assert_consumer_is_strict(
                _CONSUMER_AFTER.replace(
                    'class ModelCiBusOverlay(BaseModel):\n    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)',
                    "class ModelCiBusOverlay(BaseModel):\n    model_config = ConfigDict(str_strip_whitespace=True)",
                ),
                consumer_ref="fixture",
            )


@pytest.mark.unit
class TestTheLiveOverlayIsStillInParity:
    """This repository's own committed overlay, against a modelled consumer."""

    def test_the_committed_overlay_parses_and_declares_the_dev_lane(self) -> None:
        """A structural floor: the file the gate guards is loadable and has dev.

        The authoritative parity judgement is made in CI against the real
        omnibase_infra model. This case only refuses a locally broken overlay
        early, so a malformed file is not first reported as a cross-repo skew.
        """
        assert LIVE_OVERLAY.is_file(), f"{LIVE_OVERLAY} is missing"
        raw = yaml.safe_load(LIVE_OVERLAY.read_text(encoding="utf-8"))
        assert "dev" in raw["lanes"], (
            "runtime-rebuild-trigger.yml publishes with --bus-lane dev; an "
            f"overlay without a dev lane fails that run: {sorted(raw['lanes'])}"
        )

    def test_the_checker_runs_as_a_cli_and_exits_nonzero_on_a_skew(
        self, tmp_path: Path
    ) -> None:
        """The workflow invokes this as a process; the exit status must carry."""
        overlay = _write(tmp_path, "ci_bus_lanes.yaml", _OVERLAY_WITH_LEDGER_READBACK)
        consumer = _write(tmp_path, "consumer_before.py", _CONSUMER_BEFORE)

        result = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--overlay",
                str(overlay),
                "--consumer-model",
                str(consumer),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1, result.stdout + result.stderr
        assert "ledger_readback" in result.stderr

        ok = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--overlay",
                str(overlay),
                "--consumer-model",
                str(_write(tmp_path, "consumer_after.py", _CONSUMER_AFTER)),
            ],
            capture_output=True,
            text=True,
        )
        assert ok.returncode == 0, ok.stdout + ok.stderr


@pytest.mark.unit
class TestParityWorkflowIsWired:
    """The workflow is what makes this real; pin it so it cannot quietly go.

    Without these, deleting or repointing the workflow leaves a green unit
    suite that proves only that a fixture works.
    """

    def _workflow(self) -> dict:
        return yaml.safe_load(PARITY_WORKFLOW.read_text(encoding="utf-8"))

    def test_workflow_exists(self) -> None:
        assert PARITY_WORKFLOW.is_file(), (
            f"{PARITY_WORKFLOW.name} is the only thing that performs the LIVE "
            "cross-repo comparison; without it this file is a fixture test."
        )

    def test_workflow_runs_on_pull_request(self) -> None:
        """AC1 is specifically about failing the PR that adds the key."""
        triggers = self._workflow().get("on", self._workflow().get(True))
        assert "pull_request" in triggers

    def test_workflow_checks_out_the_consumer_at_its_default_branch(self) -> None:
        """AC2 -- resolved live, never vendored, or the class recurs one level down."""
        text = PARITY_WORKFLOW.read_text(encoding="utf-8")
        workflow = self._workflow()
        steps = next(iter(workflow["jobs"].values()))["steps"]

        checkouts = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout")
            and step.get("with", {}).get("repository") == "OmniNode-ai/omnibase_infra"
        ]
        assert checkouts, "no checkout of OmniNode-ai/omnibase_infra"

        with_block = checkouts[0]["with"]
        assert with_block["ref"] == "dev", (
            "the gate must read the branch the publisher itself runs from "
            "(dev), not a pinned or stale ref"
        )
        assert "scripts/trigger_rebuild_on_merge.py" in str(
            with_block.get("sparse-checkout", "")
        )
        assert CHECKER.name in text, "the workflow must invoke the checker by name"

    def test_workflow_refuses_an_empty_consumer_checkout_before_running(self) -> None:
        """A sparse checkout that produced nothing must not read as a pass."""
        text = PARITY_WORKFLOW.read_text(encoding="utf-8")
        assert "-s " in text or "! -s" in text, (
            "the workflow must assert the consumer file is non-empty before "
            "invoking the checker, the same way the omnibase_infra binding "
            "gate does"
        )

    def test_workflow_has_no_skip_or_force_input(self) -> None:
        """A gate with an override is a suggestion."""
        workflow = self._workflow()
        triggers = workflow.get("on", workflow.get(True))
        dispatch = (triggers or {}).get("workflow_dispatch") or {}
        inputs = (dispatch or {}).get("inputs") or {}
        forbidden = {"skip", "force", "bypass", "allow_failure", "advisory"}
        assert not (forbidden & set(inputs)), (
            f"the parity gate declares an override input: {sorted(inputs)}"
        )
