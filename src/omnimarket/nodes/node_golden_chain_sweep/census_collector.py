# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Harness-collected golden-chain census for the omnimarket CI gate (OMN-14536).

The golden_chain_sweep node is a pure validator over ``projected_rows`` the
caller supplies. Before this module, CI supplied ``projected_rows={}`` and gated
on the *workflow* completing rather than the *node's verdict* — so 0 of 13
chains were ever verified and the required check was vacuous-green.

This collector is the missing harness. For every chain the reachability oracle
classifies as **locally reachable (Group A)** it:

  1. resolves the in-repo projection that closes the chain (from contracts),
  2. drives that projection's REAL ``project(event, db)`` handler on an
     ``InmemoryDatabaseAdapter`` with a canonical head-event payload, and
  3. reads the materialized tail row back.

The rows are genuinely computed by the real handlers (zero infra, the same
inmemory-bus proof the golden-chain suite already runs on every PR), then handed
to ``NodeGoldenChainSweep`` together with a ``ModelChainCensus`` recording how
many tail surfaces were actually observed. The sweep's ``overall_status`` and
``scanned_count`` become the gate verdict.

Fail-closed invariants (any violation exits non-zero):
  * a reachable chain that yields no row is a harness bug, never a silent skip;
  * ``scanned_count == 0`` (nothing observed) can never read green;
  * the fixture set must exactly match the derived reachable set;
  * ``reachable+routed`` must equal the whole registry — no chain may fall out
    of the denominator (a shrinking census is how a gate silently goes green).

The **Group B** (routed) chains are emitted with the coverage boundary so
omnibase_infra CI can prove them against the live delegation runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
import typing
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    GoldenChainSweepRequest,
    ModelChainCensus,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)
from omnimarket.nodes.node_golden_chain_sweep.reachability import (
    derive_chain_reachability,
)
from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry
from omnimarket.projection.protocol_database import (
    InmemoryDatabaseAdapter,
    ProtocolProjectionDatabaseSync,
)

_FIXTURES_PATH = Path(__file__).parent / "census_fixtures.yaml"
_CENSUS_SOURCE = "omnimarket-inmemory-projection-drive"


class CensusCollectionError(RuntimeError):
    """A reachable chain could not be driven to a materialized row (fail-closed)."""


def _load_fixtures() -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(_FIXTURES_PATH.read_text()) or {}
    payloads = raw.get("payloads", {})
    if not isinstance(payloads, dict):
        raise CensusCollectionError(
            f"census_fixtures.yaml 'payloads' must be a mapping, got {type(payloads)}"
        )
    return payloads


def _resolve_closing_node(chain: ModelChainDefinition) -> dict[str, object]:
    """Resolve the in-repo projection node that closes ``chain``.

    Reuses the reachability oracle's own contract scan so the collector and the
    derivation can never disagree on which node closes a chain.
    """
    from omnimarket.nodes.node_golden_chain_sweep.reachability import (
        _closing_projection,
    )

    node = _closing_projection(chain)
    if node is None:  # pragma: no cover - guarded by reachability cross-check
        raise CensusCollectionError(
            f"chain {chain.name} has no closing projection but was marked reachable"
        )
    return node


def _resolve_handler_and_model(
    node: dict[str, object],
) -> tuple[object, type]:
    """Import the projection handler and resolve its ``project`` event model.

    The event model is read from the ``project(self, event, db)`` type hints
    (authoritative), not the contract ``input_model`` field which is stale/empty
    for several projections.
    """
    import importlib

    mod = importlib.import_module(str(node["handler_module"]))
    cls = getattr(mod, str(node["handler_class"]))
    hints = typing.get_type_hints(cls.project)
    # the event parameter is the first non-return, non-db hint
    event_model: type | None = None
    for name, hint in hints.items():
        if name in ("return", "db"):
            continue
        if isinstance(hint, type):
            event_model = hint
            break
    if event_model is None:
        raise CensusCollectionError(
            f"could not resolve event model from {node['handler_class']}.project hints"
        )
    return cls(), event_model


def collect_census(
    *,
    now_iso: str,
) -> tuple[dict[str, dict[str, object]], ModelChainCensus, list[ModelChainDefinition]]:
    """Drive every reachable chain's real projection and collect its tail row.

    Returns ``(projected_rows, census, group_a_chains)``. Raises
    ``CensusCollectionError`` (fail-closed) if the fixture set diverges from the
    derived reachable set or any reachable chain yields no row.
    """
    all_chains = load_registry()
    by_name = {c.name: c for c in all_chains}
    reach = derive_chain_reachability(all_chains)
    fixtures = _load_fixtures()

    # Denominator integrity: the derived split must cover the whole registry, and
    # the fixture set must match the derived reachable set exactly.
    covered = set(reach.reachable) | set(reach.routed)
    registry = {c.name for c in all_chains}
    if covered != registry:
        missing = registry - covered
        raise CensusCollectionError(
            f"reachability split does not cover the registry — uncovered: {sorted(missing)}"
        )
    fixture_names = set(fixtures)
    reachable_names = set(reach.reachable)
    if fixture_names != reachable_names:
        raise CensusCollectionError(
            "census_fixtures.yaml diverges from the derived reachable set — "
            f"only-in-fixtures={sorted(fixture_names - reachable_names)}, "
            f"only-in-derived={sorted(reachable_names - fixture_names)}"
        )

    projected_rows: dict[str, dict[str, object]] = {}
    group_a: list[ModelChainDefinition] = []
    scanned = 0

    for name in reach.reachable:
        chain = by_name[name]
        node = _resolve_closing_node(chain)
        handler, event_model = _resolve_handler_and_model(node)
        payload = dict(fixtures[name])

        # Freshness-gated chains must present a recent row. The census IS collected
        # now, so stamp the collection clock into the timestamp field when the
        # event model carries it (else the projection stamps its own now()).
        if chain.max_row_age_seconds is not None and chain.timestamp_field in getattr(
            event_model, "model_fields", {}
        ):
            payload[chain.timestamp_field] = now_iso

        event = event_model(**payload)
        db: ProtocolProjectionDatabaseSync = InmemoryDatabaseAdapter()
        handler.project(event, db)  # type: ignore[attr-defined]
        scanned += 1  # a tail surface was actually queried

        rows = db.query(chain.tail_table)
        if not rows:
            raise CensusCollectionError(
                f"reachable chain {name}: projection {node['node']} produced no row "
                f"in {chain.tail_table} — harness bug, not a skip"
            )
        projected_rows[name] = rows[-1]
        group_a.append(chain)

    census = ModelChainCensus(
        source=_CENSUS_SOURCE,
        scanned_count=scanned,
        unreachable=[],  # Group B is routed out of scope, not marked unreachable here
    )
    return projected_rows, census, group_a


def run(*, now_iso: str | None = None) -> dict[str, object]:
    """Collect the census, run the sweep over Group A, and return the gate result."""
    now_iso = now_iso or datetime.now(tz=UTC).isoformat()
    all_chains = load_registry()
    reach = derive_chain_reachability(all_chains)

    projected_rows, census, group_a = collect_census(now_iso=now_iso)

    request = GoldenChainSweepRequest(
        chains=group_a,
        projected_rows=projected_rows,
        census=census,
        now_iso=now_iso,
    )
    result = NodeGoldenChainSweep().handle(request)

    return {
        "coverage_boundary": reach.coverage_boundary,
        "reachable": reach.reachable,
        "routed_to_omnibase_infra": reach.routed,
        "reasons": reach.reasons,
        "now_iso": now_iso,
        "handler_result": result.model_dump(mode="json"),
    }


def derive_routing() -> dict[str, object]:
    """Emit the DERIVED Group B routing manifest (for omnibase_infra CI).

    omnibase_infra consumes this to learn which cross-repo chains it must prove —
    the list is computed from the same reachability oracle, never hand-maintained.
    """
    reach = derive_chain_reachability(load_registry())
    return {
        "routed_to_omnibase_infra": reach.routed,
        "reachable_in_omnimarket": reach.reachable,
        "reasons": reach.reasons,
        "coverage_boundary": reach.coverage_boundary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Collect the golden-chain census by driving reachable projections "
            "in-memory, run the sweep, and emit the fail-closed gate verdict."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("gate", "route"),
        default="gate",
        help=(
            "gate (default): collect the Group A census, run the sweep, and gate "
            "fail-closed on overall_status/scanned_count. route: emit only the "
            "DERIVED Group B routing manifest for omnibase_infra CI (no drive)."
        ),
    )
    parser.add_argument(
        "--now-iso",
        default=None,
        help="Reference clock (ISO-8601). Defaults to collection time.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the result JSON (in addition to stdout).",
    )
    args = parser.parse_args()

    if args.mode == "route":
        manifest = derive_routing()
        text = json.dumps(manifest, indent=2)
        sys.stdout.write(text + "\n")
        if args.out:
            Path(args.out).write_text(text + "\n")
        if not manifest["routed_to_omnibase_infra"]:
            sys.stderr.write("::error::routing manifest is empty\n")
            sys.exit(1)
        return

    try:
        out = run(now_iso=args.now_iso)
    except CensusCollectionError as exc:
        # A harness failure is a hard gate failure — never a silent skip.
        payload = {
            "error": str(exc),
            "gate": "FAIL",
            "reason": "census_collection_error",
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        sys.exit(2)

    text = json.dumps(out, indent=2)
    sys.stdout.write(text + "\n")
    if args.out:
        Path(args.out).write_text(text + "\n")

    hr = out["handler_result"]
    assert isinstance(hr, dict)
    status = str(hr["overall_status"])
    scanned = int(hr["scanned_count"])
    # The gate: fail-closed on anything but a populated, passing census.
    if scanned <= 0:
        sys.stderr.write(
            f"::error::census scanned_count={scanned} — nothing observed\n"
        )
        sys.exit(1)
    if status != "pass":
        sys.stderr.write(
            f"::error::golden_chain overall_status={status} (scanned {scanned}) — gate RED\n"
        )
        sys.exit(1)
    sys.stderr.write(
        f"golden_chain gate GREEN: overall_status=pass, scanned_count={scanned}\n"
    )


if __name__ == "__main__":
    main()
