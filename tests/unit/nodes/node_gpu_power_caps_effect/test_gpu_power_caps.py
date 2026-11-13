# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GPU power caps effect (OMN-24042): caps read from the settings, applied by card name, read back; both backends."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.backend_in_memory import (
    BackendGpuPowerCapsInMemory,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    caps_of_unit,
    select_backend,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsCardAction as Action,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsOutcome as Outcome,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsRefusal as Refusal,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    ModelGpuCard,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitDeclaration,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import (
    BIG,
    NODE_DIR,
    SIM_A,
    SMALL,
    build,
    caps_document,
    fake_smi,
    in_memory_overlay,
    state_limits,
    subprocess_overlay,
    unit_entry,
    values_of,
)

pytestmark = pytest.mark.unit

UNIT_A = "gpu-power-caps-sim-a"


def ask(handler: HandlerGpuPowerCaps, host: str = SIM_A) -> ModelGpuPowerCapsResult:
    return handler.handle(ModelGpuPowerCapsRequest(host=host, requested_by="lane-a"))


def actions(result: ModelGpuPowerCapsResult) -> list[Action | None]:
    return [row.action for row in result.cards]


def test_golden_each_card_found_by_name_is_applied_and_read_back() -> None:
    handler, backend = build(caps_document())
    result = ask(handler)
    assert result.outcome is Outcome.OK
    assert [(r.name, r.before_watts, r.after_watts) for r in result.cards] == [
        (BIG, 600, 500),
        (SMALL, 450, 300),
    ]
    assert actions(result) == [Action.APPLIED, Action.APPLIED]
    assert len(backend.set_calls) == 2


def test_idempotent_a_second_run_changes_nothing() -> None:
    handler, backend = build(caps_document())
    ask(handler)
    written = len(backend.set_calls)
    again = ask(handler)
    assert again.outcome is Outcome.OK
    assert actions(again) == [Action.UNCHANGED, Action.UNCHANGED]
    assert len(backend.set_calls) == written


def test_a_changed_value_applies_without_a_restart() -> None:
    doc = caps_document()
    handler, backend = build(doc)
    ask(handler)
    values_of(doc, UNIT_A)["WATTS_1"] = "350"
    result = ask(handler)
    assert actions(result) == [Action.UNCHANGED, Action.APPLIED]
    assert result.cards[1].after_watts == 350
    assert backend.set_calls[-1] == (SIM_A, 1, 350)


def test_a_wrong_card_name_refuses_and_nothing_is_written() -> None:
    doc = caps_document()
    values_of(doc, UNIT_A)["GPU_NAME_0"] = "No Such Card"
    handler, backend = build(doc)
    result = ask(handler)
    assert result.outcome is Outcome.REFUSED
    assert result.refusal is Refusal.CARD_NOT_FOUND
    assert all(row.action is None for row in result.cards)
    assert backend.set_calls == []


def test_a_duplicate_card_name_on_the_host_refuses() -> None:
    overlay = in_memory_overlay()
    host = overlay.hosts[0]
    twin = host.simulated_cards[1].model_copy(update={"name": BIG, "index": 1})
    hosts = (
        host.model_copy(update={"simulated_cards": (host.simulated_cards[0], twin)}),
        *overlay.hosts[1:],
    )
    handler, backend = build(
        caps_document(), overlay.model_copy(update={"hosts": hosts})
    )
    result = ask(handler)
    assert (result.outcome, result.refusal) == (Outcome.REFUSED, Refusal.CARD_AMBIGUOUS)
    assert backend.set_calls == []


def test_a_value_outside_the_card_range_refuses() -> None:
    doc = caps_document()
    values_of(doc, UNIT_A)["WATTS_0"] = "700"
    handler, backend = build(doc)
    result = ask(handler)
    assert (result.outcome, result.refusal) == (
        Outcome.REFUSED,
        Refusal.WATTS_OUT_OF_RANGE,
    )
    assert backend.set_calls == []


def test_an_unknown_host_refuses() -> None:
    handler, _ = build(caps_document())
    result = ask(handler, "nowhere")
    assert (result.outcome, result.refusal) == (Outcome.REFUSED, Refusal.HOST_UNKNOWN)


def _no_unit(doc: dict[str, Any]) -> None:
    del doc["units"][UNIT_A]


def _other_host(doc: dict[str, Any]) -> None:
    doc["units"][UNIT_A]["hosts"] = ["sim-host-b"]


def _watts_missing(doc: dict[str, Any]) -> None:
    del values_of(doc, UNIT_A)["WATTS_1"]


def _watts_not_a_number(doc: dict[str, Any]) -> None:
    values_of(doc, UNIT_A)["WATTS_1"] = "abc"


def _repeated_name(doc: dict[str, Any]) -> None:
    values_of(doc, UNIT_A)["GPU_NAME_1"] = BIG


def _name_key_renamed(doc: dict[str, Any]) -> None:
    values_of(doc, UNIT_A)["GPU_NAME__0"] = values_of(doc, UNIT_A).pop("GPU_NAME_0")


@pytest.mark.parametrize(
    "break_it",
    [
        _no_unit,
        _other_host,
        _watts_missing,
        _watts_not_a_number,
        _repeated_name,
        _name_key_renamed,
    ],
)
def test_unreadable_caps_refuse_and_nothing_is_written(break_it: Any) -> None:
    doc = caps_document()
    break_it(doc)
    handler, backend = build(doc)
    result = ask(handler)
    assert (result.outcome, result.refusal) == (
        Outcome.REFUSED,
        Refusal.CAPS_UNREADABLE,
    )
    assert backend.set_calls == []


def test_a_settings_document_that_cannot_be_loaded_refuses() -> None:
    def broken() -> Any:
        raise NodeOverlayError("gpu_power_caps: the overlay file is not configured")

    handler = HandlerGpuPowerCaps(
        in_memory_overlay(), select_backend(in_memory_overlay()), caps_loader=broken
    )
    assert ask(handler).refusal is Refusal.CAPS_UNREADABLE


def test_unreadable_cards_refuse() -> None:
    handler, backend = build(caps_document())
    backend.unreadable.add(SIM_A)
    result = ask(handler)
    assert (result.outcome, result.refusal) == (
        Outcome.REFUSED,
        Refusal.GPUS_UNREADABLE,
    )


def test_a_failed_write_is_reported_and_the_other_card_still_applies() -> None:
    handler, backend = build(caps_document())
    backend.failing_indexes.add((SIM_A, 0))
    result = ask(handler)
    assert result.outcome is Outcome.FAILED
    assert actions(result) == [Action.FAILED, Action.APPLIED]
    assert "exited 1" in (result.cards[0].detail or "")


def test_a_write_accepted_but_not_applied_is_caught_by_the_read_back() -> None:
    handler, backend = build(caps_document())
    backend.ignoring.add((SIM_A, 0))
    result = ask(handler)
    assert result.outcome is Outcome.FAILED
    assert (result.cards[0].action, result.cards[0].after_watts) == (Action.FAILED, 600)


def test_the_caps_settings_are_read_on_every_request() -> None:
    reads = []
    overlay = in_memory_overlay()
    doc = caps_document()

    def loader() -> Any:
        from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
            ModelNetUnitsOverlay,
        )

        reads.append(1)
        return ModelNetUnitsOverlay.model_validate(doc)

    handler = HandlerGpuPowerCaps(overlay, select_backend(overlay), caps_loader=loader)
    ask(handler)
    ask(handler)
    assert len(reads) == 2


def test_caps_of_unit_returns_the_pairs_in_order_and_never_echoes_a_value() -> None:
    unit = ModelNetUnitDeclaration.model_validate(
        unit_entry(
            "h",
            {"GPU_NAME_0": "A", "WATTS_0": "450", "GPU_NAME_1": "B", "WATTS_1": "300"},
        )
    )
    assert [(c.name, c.watts) for c in caps_of_unit(unit, "GPU_NAME_", "WATTS_")] == [
        ("A", 450),
        ("B", 300),
    ]
    bad = ModelNetUnitDeclaration.model_validate(
        unit_entry("h", {"GPU_NAME_0": "A", "WATTS_0": "-77"})
    )
    with pytest.raises(ValueError, match="positive base-10") as caught:
        caps_of_unit(bad, "GPU_NAME_", "WATTS_")
    assert "77" not in str(caught.value)


def test_the_shipped_overlays_validate() -> None:
    for name in ("in_memory", "subprocess.example"):
        text = (NODE_DIR / "overlays" / f"{name}.yaml").read_text(encoding="utf-8")
        ModelGpuPowerCapsOverlay.model_validate(yaml.safe_load(text))
    assert isinstance(select_backend(in_memory_overlay()), BackendGpuPowerCapsInMemory)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["hosts"].append(dict(d["hosts"][0])),
        lambda d: d.update(set_argv=["nvidia-smi", "-pl", "{watts}"]),
        lambda d: d.update(set_argv=["nvidia-smi", "-i", "{index}", "-pl", "300"]),
        lambda d: d.update(
            read_argv=[
                "nvidia-smi",
                "--query-gpu=index,name",
                "--format=csv,noheader,nounits",
            ]
        ),
        lambda d: d.update(unexpected_key=1),
    ],
)
def test_overlay_refusals(mutate: Any) -> None:
    raw = yaml.safe_load(
        (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
    )
    mutate(raw)
    with pytest.raises(ValidationError):
        ModelGpuPowerCapsOverlay.model_validate(raw)


def test_subprocess_backend_golden_and_idempotent_against_a_fake_nvidia_smi(
    tmp_path: Path,
) -> None:
    script = fake_smi(tmp_path)
    overlay = subprocess_overlay(script)
    handler, _ = build(caps_document(), overlay)
    first = ask(handler)
    assert first.outcome is Outcome.OK
    assert actions(first) == [Action.APPLIED, Action.APPLIED]
    assert state_limits(script) == {0: 500.0, 1: 300.0}
    assert actions(ask(handler)) == [Action.UNCHANGED, Action.UNCHANGED]


def test_subprocess_backend_refuses_a_wrong_card_name_and_writes_nothing(
    tmp_path: Path,
) -> None:
    script = fake_smi(tmp_path)
    doc = caps_document()
    values_of(doc, UNIT_A)["GPU_NAME_1"] = "No Such Card"
    handler, _ = build(doc, subprocess_overlay(script))
    assert ask(handler).refusal is Refusal.CARD_NOT_FOUND
    assert state_limits(script) == {0: 600.0, 1: 450.0}


def test_subprocess_backend_unreadable_reads_refuse(tmp_path: Path) -> None:
    script = fake_smi(tmp_path)
    exits = [sys.executable, "-c", "import sys; sys.exit(3)"]
    handler, _ = build(caps_document(), subprocess_overlay(script, read_program=exits))
    assert ask(handler).refusal is Refusal.GPUS_UNREADABLE
    short = [sys.executable, "-c", "print('0, Sim Card Big, 600.00, 100.00')"]
    handler, _ = build(caps_document(), subprocess_overlay(script, read_program=short))
    assert ask(handler).refusal is Refusal.GPUS_UNREADABLE


def test_subprocess_backend_a_failing_set_is_a_failed_row(tmp_path: Path) -> None:
    script = fake_smi(tmp_path)
    failing = [sys.executable, "-c", "import sys; sys.exit(1)"]
    handler, _ = build(caps_document(), subprocess_overlay(script, set_program=failing))
    result = ask(handler)
    assert result.outcome is Outcome.FAILED
    assert actions(result) == [Action.FAILED, Action.FAILED]


def test_the_transport_prefix_is_in_force(tmp_path: Path) -> None:
    script = fake_smi(tmp_path)
    prefix = [sys.executable, "-c", "import sys; sys.exit(7)"]
    handler, _ = build(
        caps_document(), subprocess_overlay(script, transport_prefix=prefix)
    )
    assert ask(handler).refusal is Refusal.GPUS_UNREADABLE


def test_a_card_model_round_trips() -> None:
    card = ModelGpuCard(
        index=0, name=BIG, power_limit_watts=1, min_limit_watts=1, max_limit_watts=2
    )
    assert ModelGpuCard.model_validate_json(card.model_dump_json()) == card
