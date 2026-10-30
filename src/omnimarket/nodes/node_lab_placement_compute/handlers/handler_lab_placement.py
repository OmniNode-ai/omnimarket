# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabPlacement -- what the lab host table lets each host take (OMN-17232).

Placement used to read only the capacity advertisements, so ``available: false`` in the host
table kept a host out of the ssh path alone: on 2026-10-29 a host marked unavailable ran seven or
more units from other lanes over the bus, on a CPU without AVX2 (an omnibase_core suite among them
ended 128 failed; a census of fifteen repositories on that host found one, a private repository, whose unit suite
dies of the missing instruction set, in a wheel). A host now takes a unit only when the table says so:

1. it is a row of the table (an advertising host the table does not know takes nothing);
2. the row is not ``available: false``;
3. the row takes the unit's work kind (``job_kinds``, omitted: every kind);
4. the unit's weight is no heavier than the row's ``max_weight``;
5. the row has every capability the unit needs (``avx2``, a boolean column).

A unit's weight and needs come from the rules in this node's ``contract.yaml`` (``placement``)
and from the table document's ``unit_needs`` (a private repository's rule is data of the private
table, never of this public contract), keyed by the repository name and the work kind. The handler is pure: the table, the unit and
the hosts arrive in the request, and the rules are the contract's.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import (
    ModelLabHostRow,
    ModelLabHostsTable,
    ModelUnitNeedsRule,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_placement import (
    ModelHostTableVerdict,
    ModelLabPlacementRequest,
    ModelLabPlacementResult,
    ModelPlacementRules,
    ModelUnitProfile,
)

NODE_PACKAGE = "omnimarket.nodes.node_lab_placement_compute"


class HostTableError(ValueError):
    """The host table or the placement rules could not be read, so nothing can be judged."""


def load_host_table(
    environ: Mapping[str, str] | None = None,
) -> ModelLabHostsTable:
    """The lab host table: the file ``ONEX_LAB_RUN_HOSTS`` names, else the ``lab_hosts`` overlay.

    Raises :class:`HostTableError` when neither supplies a valid table: placement then refuses
    instead of admitting every host that advertises."""
    try:
        return resolve_lab_hosts_table(environ)
    except NodeOverlayError as exc:
        raise HostTableError(f"the lab host table could not be read: {exc}") from None


def load_placement_rules() -> ModelPlacementRules:
    """Read and check ``placement`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("placement")
    if not isinstance(block, dict):
        raise HostTableError("the lab placement contract declares no placement rules")
    try:
        return ModelPlacementRules.model_validate(block)
    except ValidationError as exc:
        raise HostTableError(
            f"the lab placement rules are invalid: {exc.errors()[0]['msg']}"
        ) from None


def repo_name(repo: str) -> str:
    """The repository name without its owner (``OmniNode-ai/omnibase_core`` -> ``omnibase_core``)."""
    return repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


class HandlerLabPlacement:
    """COMPUTE handler: judge hosts against the host table for one unit."""

    def __init__(self, rules: ModelPlacementRules | None = None) -> None:
        self._rules = rules or load_placement_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    @property
    def rules(self) -> ModelPlacementRules:
        return self._rules

    def unit_profile(
        self,
        *,
        repo: str,
        kind: str,
        weight: str | None,
        table_needs: tuple[ModelUnitNeedsRule, ...] = (),
    ) -> ModelUnitProfile:
        """The unit's weight (declared, else the rules' weight for its kind) and the needs its repo carries,
        from the contract's rules and from the table document's ``unit_needs`` (private repositories)."""
        rules = self._rules
        name = repo_name(repo)
        chosen = (
            weight or rules.weight_by_kind.get(kind) or rules.host_defaults.max_weight
        )
        try:
            rules.weight_rank(chosen)
        except ValueError as exc:
            raise HostTableError(str(exc)) from None
        try:
            rules.check_needs(table_needs)
        except ValueError as exc:
            raise HostTableError(str(exc)) from None
        needs: dict[str, str] = {}
        for rule in (*rules.unit_needs, *table_needs):
            if rule.repo == name and rule.kind in (None, kind):
                for need in rule.needs:
                    needs.setdefault(need, f"{need}: {rule.reason}")
        return ModelUnitProfile(
            repo=name,
            kind=kind,
            weight=chosen,
            needs=tuple(sorted(needs)),
            need_reasons=tuple(needs[need] for need in sorted(needs)),
        )

    def judge_host(
        self, host_name: str, row: ModelLabHostRow | None, unit: ModelUnitProfile
    ) -> ModelHostTableVerdict:
        """The host table's verdict for one host."""
        rules = self._rules
        if row is None:
            return ModelHostTableVerdict(
                host_name=host_name,
                eligible=False,
                reason="not a row of the host table",
            )
        speed = row.speed if row.speed is not None else rules.host_defaults.speed
        own_bar = row.min_free_gb

        def refuse(reason: str) -> ModelHostTableVerdict:
            return ModelHostTableVerdict(
                host_name=host_name,
                eligible=False,
                reason=reason,
                min_free_gb=own_bar,
                speed=speed,
            )

        if not row.available:
            why = row.unavailable_reason or "no reason recorded"
            return refuse(f"unavailable in the host table: {why}")
        if row.job_kinds is not None and unit.kind not in row.job_kinds:
            return refuse(
                f"takes only {sorted(row.job_kinds)} units, not {unit.kind} "
                "(host table job_kinds)"
            )
        max_weight = row.max_weight or rules.host_defaults.max_weight
        try:
            too_heavy = rules.weight_rank(unit.weight) > rules.weight_rank(max_weight)
        except ValueError as exc:
            raise HostTableError(f"host {host_name}: {exc}") from None
        if too_heavy:
            return refuse(
                f"takes {max_weight} units at most, this one is {unit.weight} "
                "(host table max_weight)"
            )
        for need, why in zip(unit.needs, unit.need_reasons, strict=True):
            declared = getattr(row, need)
            has = getattr(rules.host_defaults, need) if declared is None else declared
            if not has:
                return refuse(f"lacks {need}, which a {unit.repo} unit needs ({why})")
        return ModelHostTableVerdict(
            host_name=host_name,
            eligible=True,
            reason="eligible",
            min_free_gb=own_bar,
            speed=speed,
        )

    def handle(self, request: ModelLabPlacementRequest) -> ModelLabPlacementResult:
        unit = self.unit_profile(
            repo=request.repo,
            kind=request.kind,
            weight=request.weight,
            table_needs=request.unit_needs,
        )
        rows = {row.name: row for row in request.table}
        return ModelLabPlacementResult(
            unit=unit,
            verdicts=tuple(
                self.judge_host(name, rows.get(name), unit)
                for name in sorted(set(request.host_names))
            ),
        )


__all__ = [
    "HandlerLabPlacement",
    "HostTableError",
    "load_host_table",
    "load_placement_rules",
    "repo_name",
]
