#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify every ORPHANED_PRODUCER/ORPHANED_CONSUMER baseline entry (OMN-15984).

``contract_topic_graph_baseline.yaml`` freezes 688 ``ORPHANED_PRODUCER`` /
``ORPHANED_CONSUMER`` entries under ``accepted:``. A prior audit pass
(OMN-15982, 2026-08-12) individually re-verified only ~77 of the 688 (~11%)
against a CLI-dispatch-shim cross-reference before asserting the mass was
"substantially" false positives. This script closes that sampling gap: it
re-derives the SAME 688 findings from the live contract graph (reusing
``omnimarket.validators.contract_topic_graph`` -- no reimplementation of the
graph/defect logic) and classifies every one of them against two REAL,
positive, independently-checkable signals:

1. ``skill_dispatch`` -- the node is the declared backing node of a live
   ``/onex:`` CLI skill. Checked four ways, most-authoritative first
   (``skill_mapping_registry`` > ``skill_md_backing_node`` >
   ``skill_md_dispatch_command`` > ``skill_md_architecture_map`` -- see
   ``_load_cli_registry``): (a) ``omnibase_infra``'s ``cli/skill_mapping.yaml``
   (the canonical declarative skill->node registry), (b) an explicit "Backing
   node(s)"/"Backed by" declaration in a
   ``omniclaude/plugins/*/skills/*/SKILL.md`` file (the same pattern
   ``skill_functional_audit`` already uses to verify a skill's backing node
   exists), (c) the literal dispatch command line (``onex run-node node_x`` /
   ``onex node node_x``) for skills invoked directly rather than through the
   registry, (d) "## Architecture" ASCII-map notation (``node_x -> path
   (description)``). ``onex skill <name>`` dispatches through the in-process
   ``run_receipt_mode`` path -- invisible to the Kafka-edges-only contract
   graph unless the contract also declares ``runtime_dispatch.command_topic``
   (only 57/389 contracts do).
2. ``declared_ingress_root`` -- the node's OWN contract declares
   ``runtime_dispatch.external_trigger: true`` or is registered under a
   poller-type integration in a package's ``contracts/integrations/
   catalog.yaml``. This is ``contract_topic_graph.py``'s OWN pre-existing,
   validated signal for "a real live worker with zero producer by design"
   (already used to exempt on-demand ingress roots from
   ``DISCONNECTED_SUBGRAPH``) -- reused here, not reinvented.

A node is tagged ``cli_dispatch_reachable`` if EITHER signal fires, else
``genuine_orphan``.

This script does NOT modify ``contract_topic_graph_baseline.yaml`` or its
ratchet semantics -- ``ModelBaseline`` is ``extra="forbid"`` and the gate's
``evaluate_ratchet``/``merge_base_accepted_keys`` match on the flat
``accepted:`` string keys verbatim, so widening that schema would break the
gate. It writes a READ-ONLY companion classification file instead.

Package resolution follows the SAME two-tier split as ``contract_topic_graph.py``
itself, for the same reason (see its module docstring): ``omnibase_infra`` is an
installed pip dependency of omnimarket, so ``skill_mapping.yaml`` resolves via
``importlib.util.find_spec`` -- no extra checkout needed, portable to CI.
``omniclaude`` is NOT a pip dependency (a checkout-tier package, per
:data:`CHECKOUT_PACKAGES`), so its SKILL.md corpus resolves from
``CONTRACT_GRAPH_CHECKOUT_ROOT`` -- the same env var :func:`discover_contract_roots`
already requires to build the graph at all. No new env var.

Usage::

    cd omnimarket
    CONTRACT_GRAPH_CHECKOUT_ROOT=/path/to/omni_home \\
        env -u PYTHONPATH uv run python scripts/classify_contract_graph_orphans.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omnimarket.validators.contract_topic_graph import (
    CHECKOUT_ROOT_ENV,
    ModelContractNode,
    ModelGraphFinding,
    build_graph,
    discover_contract_roots,
    find_defects,
    load_baseline,
)

DEFAULT_BASELINE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "validators"
    / "data"
    / "contract_topic_graph_baseline.yaml"
)
CLASSIFICATION_OUT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "validators"
    / "data"
    / "contract_topic_graph_orphan_classification.yaml"
)

_NODE_TOKEN_RE = re.compile(r"node_[a-z0-9_]+")
# Both phrasings appear across the corpus ("**Backing node**: `node_x`" and
# "Backed by `node_x` in omnimarket") -- and markdown prose wraps mid-phrase
# (e.g. authorize/SKILL.md: "Backed\nby `node_authorize`"), so this must be
# matched against whitespace-normalized text, never per physical line.
_BACKING_PHRASE_RE = re.compile(r"(?i)back(?:ing nodes?|ed by)")
# How far past the phrase a node_ token still counts as "declared by" it,
# in the whitespace-normalized text. Generous enough for a full sentence,
# tight enough that it won't wander into an unrelated code block.
_BACKING_WINDOW_CHARS = 250
_BACKING_WINDOW_MAX_TOKENS = 4
# The literal CLI invocation line itself (e.g. "onex run-node node_ci_watch",
# "onex node node_X --input <file>") is a stronger signal than backing-node
# prose for skills dispatched directly rather than through skill_mapping.yaml
# -- it names the exact node the command executes, not a description of it.
_DISPATCH_COMMAND_RE = re.compile(r"(?:run-node|onex node)\s+(node_[a-z0-9_]+)")
# "## Architecture" ASCII-map notation ("node_x  -> omnimarket/nodes/node_x/
# (description)"), used by skills that document their dispatch chain as a
# table instead of prose (e.g. onboarding/SKILL.md).
_ARCHITECTURE_MAP_RE = re.compile(r"(node_[a-z0-9_]+)\s+->")

Tag = Literal["cli_dispatch_reachable", "genuine_orphan"]


class ModelOrphanClassification(BaseModel):
    """One classified ORPHANED_PRODUCER/ORPHANED_CONSUMER baseline entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    defect: str
    node: str
    package: str
    topic: str
    tag: Tag
    signal: str | None = Field(
        default=None,
        description=(
            "Which check fired: 'skill_mapping_registry', 'skill_md_backing_node', "
            "'skill_md_dispatch_command', 'skill_md_architecture_map', or "
            "'declared_ingress_root'. None when tag == genuine_orphan."
        ),
    )


def _checkout_root() -> Path:
    # Fail fast (rule 8) -- no silent default. Same env var
    # discover_contract_roots() already requires for the checkout tier.
    return Path(os.environ[CHECKOUT_ROOT_ENV])


def _skill_mapping_registry() -> set[str]:
    """Bare node identifiers (``node_`` prefix stripped) from skill_mapping.yaml.

    Resolved via the installed distribution, exactly like
    ``discover_contract_roots`` resolves every other INSTALLED_PACKAGES root --
    ``omnibase_infra`` is a pip dependency of omnimarket, so this needs no
    checkout and works identically in CI and locally.
    """
    spec = importlib.util.find_spec("omnibase_infra")
    if spec is None or not spec.origin:
        raise RuntimeError(
            "omnibase_infra is not installed in this environment -- cannot "
            "resolve skill_mapping.yaml. Run via `uv run` from omnimarket."
        )
    path = Path(spec.origin).parent / "cli" / "skill_mapping.yaml"
    data = yaml.safe_load(path.read_text())
    names: set[str] = set()
    for entry in data.get("skills", []):
        node_name = entry.get("node_name", "")
        if node_name:
            names.add(_strip_prefix(node_name))
    return names


def _skill_md_backing_nodes() -> tuple[set[str], set[str], set[str]]:
    """Bare node identifiers declared as a skill's backing node in SKILL.md,
    from three independent signals: (1) "Backing node(s)"/"Backed by" prose,
    matched on whitespace-normalized text so a markdown line-wrap mid-phrase
    can't produce a silent false negative; (2) the literal dispatch command
    line ("onex run-node node_x" / "onex node node_x"), for skills dispatched
    directly rather than through skill_mapping.yaml; (3) "## Architecture"
    ASCII-map notation ("node_x -> path (description)"). Returns
    (prose_names, dispatch_command_names, architecture_map_names) so callers
    can attribute signal provenance precisely.

    ``plugins/`` lives at the omniclaude REPO ROOT, not under
    ``src/omniclaude/`` (that subpackage is the hook runtime, a different
    thing) -- so this reads directly under the checkout root, not through
    discover_contract_roots()'s src-layout resolution (which is for the
    installed-contract tier and would resolve the wrong subtree here).
    """
    prose_names: set[str] = set()
    dispatch_names: set[str] = set()
    architecture_names: set[str] = set()
    skills_root = _checkout_root() / "omniclaude" / "plugins"
    for skill_md in sorted(skills_root.glob("*/skills/*/SKILL.md")):
        raw = skill_md.read_text(errors="replace")
        normalized = re.sub(r"\s+", " ", raw)
        for phrase in _BACKING_PHRASE_RE.finditer(normalized):
            window = normalized[phrase.end() : phrase.end() + _BACKING_WINDOW_CHARS]
            tokens = _NODE_TOKEN_RE.findall(window)[:_BACKING_WINDOW_MAX_TOKENS]
            prose_names.update(_strip_prefix(t) for t in tokens)
        for match in _DISPATCH_COMMAND_RE.finditer(raw):
            dispatch_names.add(_strip_prefix(match.group(1)))
        for match in _ARCHITECTURE_MAP_RE.finditer(raw):
            architecture_names.add(_strip_prefix(match.group(1)))
    return prose_names, dispatch_names, architecture_names


def _strip_prefix(name: str) -> str:
    return name[5:] if name.startswith("node_") else name


def _load_cli_registry() -> tuple[set[str], dict[str, str]]:
    """Union of all three registries, plus a bare-name -> signal provenance map.

    Priority order where a node matches more than one signal (first write
    wins via setdefault): skill_mapping.yaml is the canonical declarative
    registry, so it wins; SKILL.md prose is the next most explicit; the
    dispatch-command grep is the broadest/weakest signal, used last.
    """
    signal: dict[str, str] = {}
    for n in _skill_mapping_registry():
        signal[n] = "skill_mapping_registry"
    prose_names, dispatch_names, architecture_names = _skill_md_backing_nodes()
    for n in prose_names:
        signal.setdefault(n, "skill_md_backing_node")
    for n in dispatch_names:
        signal.setdefault(n, "skill_md_dispatch_command")
    for n in architecture_names:
        signal.setdefault(n, "skill_md_architecture_map")
    return set(signal), signal


def classify() -> tuple[list[ModelOrphanClassification], set[str]]:
    cli_registry, cli_signal = _load_cli_registry()

    roots = discover_contract_roots()
    baseline = load_baseline(DEFAULT_BASELINE)
    graph = build_graph(roots, external_producers=baseline.external_producers)
    findings = find_defects(graph)

    nodes_by_name_pkg: dict[tuple[str, str], ModelContractNode] = {
        (n.name, n.package): n for n in graph.nodes
    }
    # Fallback for the rare cross-package name collision where a finding's
    # (node, package) pair still needs a candidate -- match by name alone.
    nodes_by_name: dict[str, list[ModelContractNode]] = {}
    for n in graph.nodes:
        nodes_by_name.setdefault(n.name, []).append(n)

    def resolve(finding: ModelGraphFinding) -> ModelContractNode | None:
        assert finding.node is not None
        exact = nodes_by_name_pkg.get((finding.node, finding.package))
        if exact is not None:
            return exact
        candidates = nodes_by_name.get(finding.node, [])
        return candidates[0] if candidates else None

    # find_defects() can emit two DISTINCT finding objects that collapse to the
    # SAME string key (ModelGraphFinding.key() omits package -- a cross-package
    # bare-name collision, e.g. node_skill_dispatch_engine_orchestrator exists
    # in both omnimarket and omniclaude). The live gate's own ratchet
    # (evaluate_ratchet) collapses exactly this way via
    # ``{f.key(): f for f in findings}`` -- last-write-wins per key, in
    # find_defects()'s deterministic (topic-sorted) traversal order. Mirror
    # that EXACT collapse so this classification corresponds 1:1 with the 688
    # entries actually frozen in accepted:, not 701 raw finding objects.
    current_by_key = {f.key(): f for f in findings}

    out: list[ModelOrphanClassification] = []
    for finding in current_by_key.values():
        if finding.defect not in ("ORPHANED_PRODUCER", "ORPHANED_CONSUMER"):
            continue
        assert finding.node is not None
        assert finding.topic is not None
        bare = _strip_prefix(finding.node)

        signal: str | None = None
        tag: Tag = "genuine_orphan"

        if bare in cli_registry:
            tag = "cli_dispatch_reachable"
            signal = cli_signal[bare]
        else:
            contract_node = resolve(finding)
            if contract_node is not None and contract_node.declared_ingress_root:
                tag = "cli_dispatch_reachable"
                signal = "declared_ingress_root"

        out.append(
            ModelOrphanClassification(
                key=finding.key(),
                defect=finding.defect,
                node=finding.node,
                package=finding.package,
                topic=finding.topic,
                tag=tag,
                signal=signal,
            )
        )
    return out, set(baseline.accepted)


def _verify_completeness(
    entries: list[ModelOrphanClassification], baseline_accepted: set[str]
) -> None:
    """Every ORPHANED_PRODUCER/ORPHANED_CONSUMER key in accepted: is classified,
    and nothing else -- no sampling, no invented keys."""
    baseline_orphan_keys = {
        k
        for k in baseline_accepted
        if k.startswith("ORPHANED_PRODUCER::") or k.startswith("ORPHANED_CONSUMER::")
    }
    classified_keys = {e.key for e in entries}
    missing = baseline_orphan_keys - classified_keys
    extra = classified_keys - baseline_orphan_keys
    if missing or extra:
        raise SystemExit(
            "COMPLETENESS CHECK FAILED: classification does not exactly match "
            f"baseline accepted: ORPHANED_* keys. missing={len(missing)} extra={len(extra)}\n"
            f"missing sample: {sorted(missing)[:5]}\nextra sample: {sorted(extra)[:5]}"
        )
    if len(baseline_orphan_keys) != 688:
        sys.stderr.write(
            f"::warning::baseline ORPHANED_* count is {len(baseline_orphan_keys)}, "
            "not the 688 this ticket was scoped against -- baseline moved since "
            "OMN-15982's audit. Classification still covers 100% of the current set.\n"
        )


def _verify_positive_controls(entries: list[ModelOrphanClassification]) -> None:
    by_key = {e.key: e for e in entries}

    reachable_control = (
        "ORPHANED_PRODUCER::aislop_sweep::onex.evt.omnimarket.aislop-sweep-completed.v1"
    )
    orphan_control = "ORPHANED_CONSUMER::node_ledger_write_effect::onex.cmd.platform.ledger-append.v1"

    reachable_entry = by_key.get(reachable_control)
    if reachable_entry is None or reachable_entry.tag != "cli_dispatch_reachable":
        raise SystemExit(
            f"POSITIVE CONTROL FAILED: {reachable_control!r} must classify as "
            f"cli_dispatch_reachable, got {reachable_entry.tag if reachable_entry else 'MISSING'}"
        )

    orphan_entry = by_key.get(orphan_control)
    if orphan_entry is None or orphan_entry.tag != "genuine_orphan":
        raise SystemExit(
            f"POSITIVE CONTROL FAILED: {orphan_control!r} must classify as "
            f"genuine_orphan, got {orphan_entry.tag if orphan_entry else 'MISSING'}"
        )


def main() -> int:
    entries, baseline_accepted = classify()
    _verify_completeness(entries, baseline_accepted)
    _verify_positive_controls(entries)

    reachable = [e for e in entries if e.tag == "cli_dispatch_reachable"]
    orphan = [e for e in entries if e.tag == "genuine_orphan"]

    signal_counts: dict[str, int] = {}
    for e in reachable:
        assert e.signal is not None
        signal_counts[e.signal] = signal_counts.get(e.signal, 0) + 1

    document = {
        "generated_by": "OMN-15984",
        "description": (
            "Companion classification of the 688 ORPHANED_PRODUCER/ORPHANED_CONSUMER "
            "entries in contract_topic_graph_baseline.yaml's accepted: list. NOT read "
            "by contract_topic_graph.py's gate -- the gate's ratchet matches the flat "
            "accepted: string keys verbatim; this file is a read-only triage artifact. "
            "Regenerate with scripts/classify_contract_graph_orphans.py."
        ),
        "source_baseline_entries": len(entries),
        "counts": {
            "cli_dispatch_reachable": len(reachable),
            "genuine_orphan": len(orphan),
            "cli_dispatch_reachable_by_signal": signal_counts,
        },
        "positive_controls_verified": [
            "ORPHANED_PRODUCER::aislop_sweep::onex.evt.omnimarket.aislop-sweep-completed.v1 -> cli_dispatch_reachable",
            "ORPHANED_CONSUMER::node_ledger_write_effect::onex.cmd.platform.ledger-append.v1 -> genuine_orphan",
        ],
        "entries": [
            {
                "key": e.key,
                "defect": e.defect,
                "node": e.node,
                "package": e.package,
                "topic": e.topic,
                "tag": e.tag,
                "signal": e.signal,
            }
            for e in sorted(entries, key=lambda e: e.key)
        ],
    }

    CLASSIFICATION_OUT.write_text(
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)
    )
    sys.stdout.write(
        f"Classified {len(entries)} entries -> {len(reachable)} cli_dispatch_reachable, "
        f"{len(orphan)} genuine_orphan. Positive controls: PASSED. "
        f"Wrote {CLASSIFICATION_OUT}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
