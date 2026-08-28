# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every admitted task_type keeps at least one declared backend [OMN-16442].

OMN-16442 deleted two local rungs from ``routing_tiers.yaml`` whose endpoints
no longer exist on the fleet (re-probed 2026-08-28 against the canonical
inventory ``omni_home/docs/reference/AI_LAB_HARDWARE.md``):

* ``local-reasoner``  — .201:8001, the RTX 4090 slot physically removed for RMA
  (OMN-16407). ``curl http://192.168.86.201:8001/v1/models`` -> exit 7
  "Couldn't connect to server".
* ``local-coder-mlx`` — .200:8401, gone. The Mac Studio's MLX server now serves
  ``Qwen3.8-27B-8bit`` on 127.0.0.1:8099, LOCALHOST-ONLY, so it is not
  reachable from the .201 runtime and is deliberately NOT re-registered.

Deleting a tier member is only safe if it does not strand a task class. That is
an INVARIANT of the routing contracts, not a property of this one edit, so it is
asserted here as data — read straight off the committed YAML, with no lane
overlay, env binding, or live endpoint in the loop (memory
``feedback_real_dispatch_path_tests``: the resolution-order proof belongs in
``test_same_tier_backend_fallback_omn14402.py``; this file guards COVERAGE).

Cross-references: OMN-16419 (the ticket that had to leave local-reasoner wired),
OMN-15961 (why ``agent_delegation`` deliberately has no backend at all).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_ROUTING_TIERS = _CONFIGS / "routing_tiers.yaml"
_TASK_CLASSES = _CONFIGS / "task_class_contracts.v1.yaml"
_BIFROST = _CONFIGS / "bifrost_delegation.yaml"

# OMN-15961: agent_delegation is the ONE admitted class that intentionally
# resolves to nothing. Its task class requires the ``agent_orchestration``
# capability, which no plain HTTP chat-completion backend can genuinely
# provide, so it fails closed (``no_routable_backend_for_task``) until the real
# coding-agent producer is wired (OMN-15961 WS-4/C6). Restoring a rung for it is
# that follow-on's job — this exemption is deliberate, not an oversight.
_FAIL_CLOSED_BY_DESIGN: frozenset[str] = frozenset({"agent_delegation"})

# OMN-16442: backend_ids whose endpoint is dead. Nothing may declare them.
_RETIRED_BACKEND_IDS: frozenset[str] = frozenset({"local-reasoner", "local-coder-mlx"})


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _tiers() -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = _load(_ROUTING_TIERS)["tiers"]
    return tiers


def _declared_backend_ids() -> set[str]:
    return {backend["backend_id"] for backend in _load(_BIFROST)["backends"]}


def _task_classes() -> dict[str, Any]:
    classes: dict[str, Any] = _load(_TASK_CLASSES)["task_classes"]
    return classes


def _tier_order(task_type: str, contract: dict[str, Any]) -> list[str]:
    """Tier order for a class, defaulting to routing_tiers.yaml order.

    ``escalation_policy.tier_order`` is the COMPLETE, CLOSED, ORDERED set when
    declared; when absent the declaration order in routing_tiers.yaml applies
    (task_class_contracts.v1.yaml header).
    """
    policy = contract[task_type].get("escalation_policy") or {}
    declared = policy.get("tier_order")
    if declared:
        order: list[str] = list(declared)
        return order
    return [tier["name"] for tier in _tiers()]


def _serving_backends(task_type: str) -> dict[str, list[str]]:
    """tier_name -> backend_ids in that tier declaring ``task_type``."""
    declared = _declared_backend_ids()
    result: dict[str, list[str]] = {}
    for tier in _tiers():
        serving = [
            model["backend_id"]
            for model in tier["models"]
            if task_type in (model.get("use_for") or [])
            # A tier member pointing at a backend the bifrost contract does not
            # define is unroutable — it must not count toward coverage.
            and model["backend_id"] in declared
        ]
        if serving:
            result[tier["name"]] = serving
    return result


@pytest.mark.unit
class TestTaskTypeBackendCoverage:
    """Coverage invariants over the committed routing contracts."""

    def test_no_task_type_loses_its_last_local_backend_omn16442(self) -> None:
        """Every admitted task_type resolves to >=1 backend in its tier_order.

        This is the assertion OMN-16442's tier deletions had to satisfy: a
        removal may shorten a ladder, never empty one.
        """
        contract = _task_classes()
        stranded: dict[str, list[str]] = {}

        for task_type in sorted(contract):
            if task_type in _FAIL_CLOSED_BY_DESIGN:
                continue
            order = _tier_order(task_type, contract)
            serving = _serving_backends(task_type)
            reachable = [
                f"{tier}:{backend}"
                for tier in order
                for backend in serving.get(tier, [])
            ]
            if not reachable:
                stranded[task_type] = order

        assert not stranded, (
            "task_types with ZERO resolvable backends across their whole "
            f"escalation ladder: {stranded}. A tier member was removed without "
            "rehoming the task types it was the last declarant for."
        )

    def test_prose_and_test_classes_kept_a_local_rung(self) -> None:
        """``test``/``documentation``/``summarization`` stay on owned GPUs.

        All three were served in the ``local`` tier ONLY by the retired
        ``local-reasoner``. Coverage alone would be satisfied by the metered
        cheap_cloud tier, so assert the stronger property the rehoming exists
        for: each keeps a zero-marginal-cost LOCAL declarant.
        """
        for task_type in ("test", "documentation", "summarization"):
            serving = _serving_backends(task_type)
            assert serving.get("local"), (
                f"{task_type!r} has no local-tier backend — it would fall "
                "straight through to the metered cheap_cloud tier"
            )

    def test_retired_backends_are_absent_from_both_contracts(self) -> None:
        """A dead endpoint must not be reachable from either routing surface."""
        assert not (_declared_backend_ids() & _RETIRED_BACKEND_IDS), (
            "bifrost_delegation.yaml defines a retired backend: "
            f"{sorted(_declared_backend_ids() & _RETIRED_BACKEND_IDS)}"
        )

        tier_refs = {
            model["backend_id"] for tier in _tiers() for model in tier["models"]
        }
        assert not (tier_refs & _RETIRED_BACKEND_IDS), (
            "routing_tiers.yaml declares a retired backend: "
            f"{sorted(tier_refs & _RETIRED_BACKEND_IDS)}"
        )

        rules = _load(_BIFROST).get("routing_rules") or []
        for rule in rules:
            offending = set(rule.get("backend_ids") or []) & _RETIRED_BACKEND_IDS
            assert not offending, (
                f"routing_rule {rule.get('task_class')!r} references a retired "
                f"backend: {sorted(offending)}"
            )

        defaults = set(_load(_BIFROST).get("default_backends") or [])
        assert not (defaults & _RETIRED_BACKEND_IDS)

    def test_every_tier_member_maps_to_a_defined_backend(self) -> None:
        """No routing_tiers.yaml member may name an undefined backend_id.

        This is the guard that would have caught the OMN-16419 deferral in the
        other direction: deleting a bifrost backend while a tier still names it.
        """
        declared = _declared_backend_ids()
        dangling = {
            (tier["name"], model["backend_id"])
            for tier in _tiers()
            for model in tier["models"]
            if model["backend_id"] not in declared
        }
        assert not dangling, (
            "routing_tiers.yaml members naming a backend_id absent from "
            f"bifrost_delegation.yaml: {sorted(dangling)}"
        )
