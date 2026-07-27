# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract guard (OMN-15005/OMN-15007, children of OMN-14141): every
omnimarket ``handler_routing.handlers[]`` entry must use the nested
``handler: {name, module}`` mapping the real loader understands.

OMN-14141 shipped a fail-closed guard in
``omnibase_infra.runtime.auto_wiring.discovery.discover_contracts_from_paths``:
a ``handlers[]`` entry that still uses the historical FLAT
``handler_class:``/``handler_module:`` string schema (or omits a per-entry
``handler:`` mapping entirely) cannot be parsed into a dispatcher and raises
``ValueError`` (surfaced here as a ``ModelDiscoveryError`` on the manifest)
instead of silently producing ``handlers=()`` and phantom-wiring the
subscribed topic with zero live handlers.

OMN-15004's audit found three flat-schema stragglers the OMN-14000 migration
(PR #1631) missed: ``node_agent_coordinator_orchestrator``,
``node_memory_lifecycle_orchestrator``, and ``node_persona_lifecycle_orchestrator``
(fixed in OMN-15005). Proving that fix's RED->GREEN over the FULL contract
set additionally surfaced 7 more affected nodes, tracked in OMN-15007. 3 of
those 7 are FIXED here: ``node_adr_canary_orchestrator``,
``node_adr_document_ingestion_effect``, and ``node_intent_query_effect`` (a
straightforward flat-to-nested rename, or -- for ``node_intent_query_effect``'s
4 additional entries -- adding the nested ``handler:`` mapping pointing at the
single real router class already declared at the contract's root ``handler:``
field, verified against the real handler source: it matches
``request.operation`` internally and dispatches to the exact operations the
bare ``handler_key`` entries named).

The remaining 4 of the 7 (``node_intent_storage_effect``,
``node_memory_retrieval_effect``, ``node_code_embedding_effect``,
``node_code_enrichment_effect``) are intentionally OUT OF SCOPE here and
tracked as a split-out follow-up in **OMN-15027**: converting them the same
way is loader-correct (verified GREEN locally, real loader) but newly exposes
each real handler class to a SEPARATE omnimarket gate that used to be
structurally blind to these entries (since neither gate ever read the flat/
bare-``handler_key`` shape):

* ``node_intent_storage_effect`` / ``node_memory_retrieval_effect``: real
  handlers (``HandlerIntentStorageAdapter``, ``HandlerMemoryRetrieval`` --
  both cross-repo in ``omnimemory``) expose only ``execute()``, never
  ``handle()``/``handle_async()`` -- newly fails the frozen/shrink-only
  ``handler_dispatch_entrypoint`` gate (OMN-14617), which refuses new
  baseline entries.
* ``node_code_embedding_effect`` / ``node_code_enrichment_effect``: real
  handlers (``HandlerCodeEmbeddingEffect``, ``HandlerCodeEnrichmentEffect``)
  take a required constructor param (``repository:
  ProtocolCodeEntityRepository``) that is not one of the 3 boot-injectable
  params (``event_bus``, ``container``, ``ownership_query``) -- newly fails
  the empty-allowlist, fail-closed ``test_handler_routing_boot_resolvable.py``
  gate (OMN-13551), which "quarantines" the handler at runtime boot.

None of these 4 fixes is a shape-only change (they require either cross-repo
work, a new RSD-authored wrapper node, or a constructor-shape refactor of the
handler itself to resolve dependencies from the injectable ``container``
instead) -- so none belongs in this mechanical-conversion ticket. See
OMN-15027 for the full analysis.

This test drives the REAL loader over every ``src/omnimarket/nodes/*/contract.yaml``
and asserts the 6 FIXED nodes (3 from OMN-15005 + 3 from OMN-15007) produce
zero discovery errors and the expected handler counts, while the 4 OMN-15027
carve-out nodes are explicitly documented as still-expected-to-error (so this
test stays honest: it does not silently pass because they are excluded, nor
silently start asserting on them if the carve-out list drifts without a
ticket reference).
"""

from __future__ import annotations

from pathlib import Path

from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths

_NODES_DIR = Path(__file__).parent.parent.parent / "src" / "omnimarket" / "nodes"

# node -> expected handler_routing.handlers count (OMN-15005 + OMN-15007, the
# 6 nodes FIXED across both tickets).
_EXPECTED_HANDLER_COUNTS = {
    # OMN-15005
    "node_agent_coordinator_orchestrator": 4,
    "node_memory_lifecycle_orchestrator": 3,
    "node_persona_lifecycle_orchestrator": 2,
    # OMN-15007
    "node_adr_canary_orchestrator": 1,
    "node_adr_document_ingestion_effect": 1,
    "node_intent_query_effect": 5,
}

# Split out from OMN-15007 into OMN-15027: converting these to the nested
# shape is loader-correct but collides with a separate, downstream omnimarket
# gate that was structurally blind to the flat/bare-handler_key shape (see
# module docstring for the per-node breakdown: handler_dispatch_entrypoint
# frozen baseline for the intent_storage/memory_retrieval pair,
# boot-resolvability empty allowlist for the code_embedding/code_enrichment
# pair). Left in their pre-existing (still flat-key-only, still
# loader-erroring) state -- no regression, status quo preserved. Listed
# explicitly so this test stays honest about the carve-out.
_KNOWN_OUT_OF_SCOPE_FLAT_SCHEMA_NODES = frozenset(
    {
        "node_intent_storage_effect",
        "node_memory_retrieval_effect",
        "node_code_embedding_effect",
        "node_code_enrichment_effect",
    }
)


def _all_contract_paths() -> list[Path]:
    return sorted(_NODES_DIR.glob("*/contract.yaml"))


def test_named_omnimarket_contracts_parse_with_zero_discovery_errors() -> None:
    """The real loader must parse the 6 OMN-15005/OMN-15007-fixed contracts
    without raising the OMN-14141 flat-schema ValueError (surfaced as a
    ModelDiscoveryError).

    RED on the pre-OMN-15007-fix tree: this fails with ModelDiscoveryError
    entries citing "handler_routing.handlers[N] is missing a nested
    'handler: {name, module}' mapping" for the 3 OMN-15007-fixed nodes (in
    addition to the OMN-15005 3, already fixed on this branch's base).
    """
    manifest = discover_contracts_from_paths(_all_contract_paths())

    errors_by_node = {error.entry_point_name: error.error for error in manifest.errors}

    flat_schema_errors = {
        node: msg
        for node, msg in errors_by_node.items()
        if node in _EXPECTED_HANDLER_COUNTS
    }
    assert not flat_schema_errors, (
        f"{len(flat_schema_errors)} omnimarket contract(s) still use the flat "
        "handler_class/handler_module schema and hard-fail the real loader "
        f"(OMN-14141/OMN-15005/OMN-15007): {flat_schema_errors}"
    )

    # Sanity-check the out-of-scope carve-out doesn't silently grow: any NEW
    # flat-schema error outside both the fixed set and the documented
    # OMN-15027 out-of-scope set is a real regression this test must catch.
    unexpected_errors = {
        node: msg
        for node, msg in errors_by_node.items()
        if node not in _EXPECTED_HANDLER_COUNTS
        and node not in _KNOWN_OUT_OF_SCOPE_FLAT_SCHEMA_NODES
    }
    assert not unexpected_errors, (
        f"Unexpected NEW discovery errors over omnimarket contracts (not in the "
        f"OMN-15005/OMN-15007 fixed set, not in the documented OMN-15027 "
        f"out-of-scope carve-out): {unexpected_errors}"
    )

    # The 2 OMN-15027 carve-out nodes are expected to STILL error (unchanged,
    # pre-existing state) -- if either starts passing, the carve-out is stale
    # and OMN-15027 should be closed / this list updated.
    still_erroring = {
        node for node in _KNOWN_OUT_OF_SCOPE_FLAT_SCHEMA_NODES if node in errors_by_node
    }
    assert still_erroring == _KNOWN_OUT_OF_SCOPE_FLAT_SCHEMA_NODES, (
        "OMN-15027 carve-out is stale: expected all of "
        f"{sorted(_KNOWN_OUT_OF_SCOPE_FLAT_SCHEMA_NODES)} to still error, but only "
        f"{sorted(still_erroring)} did. If a node now parses cleanly, remove it from "
        "the carve-out (and close/update OMN-15027)."
    )


def test_migrated_nodes_are_discovered_with_expected_handler_counts() -> None:
    """Each migrated node must appear in the discovered set with its full,
    correctly-parsed handler_routing.handlers count -- not silently dropped to
    zero dispatchers (the OMN-14139/OMN-14135 phantom-wiring failure mode)."""
    manifest = discover_contracts_from_paths(_all_contract_paths())
    discovered_by_name = {c.name: c for c in manifest.contracts}

    for node, expected_count in _EXPECTED_HANDLER_COUNTS.items():
        assert node in discovered_by_name, (
            f"{node} was not discovered at all -- check it did not get "
            "swallowed as a discovery error."
        )
        contract = discovered_by_name[node]
        assert contract.handler_routing is not None, (
            f"{node} discovered but handler_routing is None."
        )
        actual_count = len(contract.handler_routing.handlers)
        assert actual_count == expected_count, (
            f"{node} expected {expected_count} handler_routing.handlers entries, "
            f"got {actual_count}."
        )


def test_flat_shape_fixture_is_rejected_nested_shape_parses(tmp_path: Path) -> None:
    """Recurrence guard: a minimal flat-shape contract fixture is rejected by
    the loader; the same fixture converted to the nested shape parses with
    the expected handler count. Keeps this a permanent, repo-agnostic
    regression test independent of the three specific contracts above."""
    node_dir = tmp_path / "node_flat_schema_fixture"
    node_dir.mkdir()
    flat_contract = node_dir / "contract.yaml"
    flat_contract.write_text(
        """
name: "node_flat_schema_fixture"
contract_version: {major: 0, minor: 1, patch: 0}
node_version: {major: 0, minor: 1, patch: 0}
description: "Fixture contract using the historical flat handler schema."
node_type: orchestrator
input_model: "ModelFixtureRequest"
output_model: "ModelFixtureResponse"
handler_routing:
  version: {major: 1, minor: 0, patch: 0}
  routing_strategy: operation_match
  handlers:
    - operation: do_thing
      routing_key: "do_thing"
      handler_module: "some.module"
      handler_class: "HandlerFixture"
      handler_key: "HandlerFixture.do_thing"
      priority: 0
      output_events: []
  default_handler: null
"""
    )
    manifest = discover_contracts_from_paths([flat_contract])
    assert not manifest.contracts, "flat-shape fixture must not be discovered"
    assert len(manifest.errors) == 1
    assert "missing a nested" in manifest.errors[0].error
    assert "handler: {name, module}" in manifest.errors[0].error

    nested_dir = tmp_path / "node_nested_schema_fixture"
    nested_dir.mkdir()
    nested_contract = nested_dir / "contract.yaml"
    nested_contract.write_text(
        """
name: "node_nested_schema_fixture"
contract_version: {major: 0, minor: 1, patch: 0}
node_version: {major: 0, minor: 1, patch: 0}
description: "Fixture contract using the nested handler schema."
node_type: orchestrator
input_model: "ModelFixtureRequest"
output_model: "ModelFixtureResponse"
handler_routing:
  version: {major: 1, minor: 0, patch: 0}
  routing_strategy: operation_match
  handlers:
    - operation: do_thing
      routing_key: "do_thing"
      handler:
        name: "HandlerFixture"
        module: "some.module"
      handler_key: "HandlerFixture.do_thing"
      priority: 0
      output_events: []
  default_handler: null
"""
    )
    nested_manifest = discover_contracts_from_paths([nested_contract])
    assert not nested_manifest.errors, nested_manifest.errors
    assert len(nested_manifest.contracts) == 1
    assert len(nested_manifest.contracts[0].handler_routing.handlers) == 1
