"""OMN-15639 AC3 gate (omnimarket share) — contract-declared consumer groups.

Seam context
------------
The OMN-15639 seam table settles the disposition of the two consumer-group
contract surfaces as **DELETE, fail-closed** — neither is honored today and
wiring them would start minting IAM-non-conformant group names against MSK:

* ``event_bus.consumer_group`` (block level) — declared across the platform,
  never honored. Every derivation call site hardcodes an identity-derived
  group and ignores the declaration.
* ``event_bus.subscribe[*].consumer_group`` — parsed by
  ``omnibase_core.contracts.contract_parser_event_bus`` into
  ``ModelEventBusSubscription.consumer_group`` and read by nothing in
  production. The paired omnibase_core lane deletes the model field and makes
  the parser *raise* when the key is present, so a contract that still carries
  it stops parsing the moment that lands.

That second point is why this gate lands in omnimarket ahead of the core lane
rather than behind it: the declarations must be gone *before* the parser turns
fail-closed, in either merge order.

Scope — read this before widening the key set
---------------------------------------------
Only the four seam-named keys are in scope. A bare ``grep consumer_group``
over ``src/**/contract.yaml`` is WRONG here: omnimarket has two live,
in-scope-adjacent keys that are genuinely read at runtime and must survive.

* ``broker.consumer_group`` (``node_pattern_b_broker``) is loaded by
  ``adapter_broker_contract_config.load_pattern_b_broker_config`` via
  ``_required_str(broker, "consumer_group", ...)`` into
  ``ModelPatternBBrokerRuntimeConfig.consumer_group``. Stripping it raises at
  load time.
* ``delegation_runtime_dispatch.consumer_group_prefix``
  (``node_delegate_skill_orchestrator``) is a different key under a different
  block and is not one of the seam's four.

``test_out_of_scope_consumer_group_keys_are_preserved`` pins both so an
over-broad future strip fails here instead of at runtime.

Not yet in this file
--------------------
The rest of the AC3 gate (pinned IAM pattern set, grammar enumeration,
reserved-prefix closure, negative controls, and the AST default-deny walk over
``src/``) depends on ``omnibase_core.utils.util_consumer_group``, its
``ModelConsumerGroupScope`` / ``EnumReservedGroupPrefix`` types, and the
packaged ``consumer_group_iam_patterns.yaml`` — none of which exist in any
published omnibase-core wheel yet. omnimarket resolves omnibase-core from PyPI
(a git rev override for core is forbidden in-tree, ``pyproject.toml`` OMN-15539
note), so those assertions cannot be written against a real seam here today and
are deliberately absent rather than stubbed, skipped, or xfailed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

#: Dotted paths the OMN-15639 seam table deletes and forbids re-declaring.
#: ``subscribe`` is normalised below because contracts write it either as a
#: mapping (``event_bus.subscribe.consumer_group``) or as a list of mappings.
FORBIDDEN_CONTRACT_PATHS: tuple[str, ...] = (
    "event_bus.consumer_group",
    "event_bus.consumer_group_id",
    "event_bus.subscribe[*].consumer_group",
    "metadata.consumer_group_id",
)


def _iter_contracts(root: Path) -> list[Path]:
    return sorted(root.rglob("contract.yaml"))


def _load(path: Path) -> dict[str, Any] | None:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _forbidden_declarations(document: dict[str, Any]) -> list[tuple[str, Any]]:
    """Return every in-scope consumer-group declaration in one contract."""
    found: list[tuple[str, Any]] = []

    event_bus = document.get("event_bus")
    if isinstance(event_bus, dict):
        for key in ("consumer_group", "consumer_group_id"):
            if key in event_bus:
                found.append((f"event_bus.{key}", event_bus[key]))

        subscribe = event_bus.get("subscribe")
        subscriptions: list[Any]
        if isinstance(subscribe, dict):
            subscriptions = [subscribe]
        elif isinstance(subscribe, list):
            subscriptions = list(subscribe)
        else:
            subscriptions = []
        for index, subscription in enumerate(subscriptions):
            if isinstance(subscription, dict) and "consumer_group" in subscription:
                found.append(
                    (
                        f"event_bus.subscribe[{index}].consumer_group",
                        subscription["consumer_group"],
                    )
                )

    metadata = document.get("metadata")
    if isinstance(metadata, dict) and "consumer_group_id" in metadata:
        found.append(("metadata.consumer_group_id", metadata["consumer_group_id"]))

    return found


def test_contract_root_is_populated() -> None:
    """Guard against a vacuous pass from a mis-resolved ``src`` root."""
    contracts = _iter_contracts(SRC_ROOT)
    assert SRC_ROOT.is_dir(), f"expected omnimarket src at {SRC_ROOT}"
    assert len(contracts) > 50, (
        f"only {len(contracts)} contract.yaml files found under {SRC_ROOT}; "
        "the walker is mis-pointed and every other assertion here is vacuous"
    )


@pytest.mark.parametrize(
    ("fragment", "expected_path"),
    [
        (
            {"event_bus": {"consumer_group": "omnimarket.x.consume.v1"}},
            "event_bus.consumer_group",
        ),
        (
            {"event_bus": {"consumer_group_id": "omnimarket.x.consume.v1"}},
            "event_bus.consumer_group_id",
        ),
        (
            {"event_bus": {"subscribe": {"consumer_group": "omnimarket.x"}}},
            "event_bus.subscribe[0].consumer_group",
        ),
        (
            {"event_bus": {"subscribe": [{"topic": "t"}, {"consumer_group": "g"}]}},
            "event_bus.subscribe[1].consumer_group",
        ),
        (
            {"metadata": {"consumer_group_id": "omnimarket.x"}},
            "metadata.consumer_group_id",
        ),
    ],
)
def test_detector_discriminates_on_synthetic_contracts(
    fragment: dict[str, Any], expected_path: str
) -> None:
    """A detector that finds nothing would make the real assertion vacuous."""
    found = _forbidden_declarations(fragment)
    assert [path for path, _ in found] == [expected_path]


@pytest.mark.parametrize(
    "fragment",
    [
        {"broker": {"consumer_group": "omnimarket-pattern-b-broker"}},
        {"delegation_runtime_dispatch": {"consumer_group_prefix": "x"}},
        {"event_bus": {"subscribe": {"topic": "onex.cmd.omnimarket.x.v1"}}},
        {"event_bus": {"publish": {"success_topic": "onex.evt.omnimarket.x.v1"}}},
    ],
)
def test_detector_ignores_out_of_scope_keys(fragment: dict[str, Any]) -> None:
    """The seam deletes four specific paths — not every key spelled 'group'."""
    assert _forbidden_declarations(fragment) == []


def test_no_contract_declares_consumer_group() -> None:
    """No omnimarket contract may declare a consumer group (OMN-15639 seam §4).

    Derivation through ``compute_consumer_group_id`` /
    ``derive_prefixed_group_id`` is the single source of a group name. A
    contract-declared literal is dead today and IAM-non-conformant tomorrow.
    """
    violations: list[str] = []
    for contract in _iter_contracts(SRC_ROOT):
        document = _load(contract)
        if document is None:
            continue
        for path, value in _forbidden_declarations(document):
            relative = contract.relative_to(SRC_ROOT.parent)
            violations.append(f"{relative}: {path} = {value!r}")

    assert not violations, (
        f"{len(violations)} contract(s) still declare a consumer group. The "
        "OMN-15639 seam deletes these keys fail-closed — a group name is "
        "derived, never declared. Forbidden paths: "
        f"{', '.join(FORBIDDEN_CONTRACT_PATHS)}.\n" + "\n".join(violations)
    )


def test_out_of_scope_consumer_group_keys_are_preserved() -> None:
    """``broker.consumer_group`` is live-read — an over-broad strip breaks it."""
    broker_contract = (
        SRC_ROOT / "omnimarket" / "nodes" / "node_pattern_b_broker" / "contract.yaml"
    )
    document = _load(broker_contract)
    assert document is not None, f"{broker_contract} did not parse as a mapping"

    broker = document.get("broker")
    assert isinstance(broker, dict), (
        f"{broker_contract} lost its 'broker' block; "
        "load_pattern_b_broker_config() raises without it"
    )
    declared = broker.get("consumer_group")
    stripped_message = (
        f"{broker_contract} broker.consumer_group was stripped, but "
        "adapter_broker_contract_config._required_str() requires it at load time. "
        "That key is NOT one of the four seam-deleted paths."
    )
    assert isinstance(declared, str), stripped_message
    assert declared, stripped_message
