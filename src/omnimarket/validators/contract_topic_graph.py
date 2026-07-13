# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Static contract topic graph — the producer/consumer oracle (OMN-14527).

Every runtime wiring check the platform had before this asked the RUNTIME
"are you alive?" -- consumer groups, row counts, wiring reports. That diagnoses
from the effect side, one node at a time, and it only ever sees nodes that
someone remembered to look at.

This asks the opposite, and it asks it of every node at once:

    who is SUPPOSED to talk to whom -- and do they?

Nodes are contracts. Edges are topics. A producer declares ``publish_topics``;
a consumer declares ``subscribe_topics``. An edge exists when the topic names
match. Everything below falls out of that one relation, statically, with no
broker and no deploy.

The motivating defect (OMN-14134 / 13532 / 4828 / 13989 / 13997 -- five merged,
CI-green, ``Done`` tickets that were 100% runtime no-ops):
``node_ledger_write_effect`` has a fully wired ``handler_routing``
(HandlerLedgerAppend + HandlerLedgerQuery, topic-routed) and subscribes to
``onex.cmd.platform.ledger-append.v1``. No contract anywhere publishes that
topic, and the node declares no ``runtime_dispatch`` entry point -- so there is
no producer and no CLI dispatch path. The handler was alive, healthy, and
starved for its entire life. Five green CI runs never noticed, because CI was
never asked this question.

Soundness requires the WHOLE graph -- and "whole" serves two different jobs
----------------------------------------------------------------------------
A partial graph invents false orphans: a topic whose producer lives in a repo
you cannot see looks exactly like a topic nobody produces. That constraint
means two different things depending on what the graph is being asked:

1. **Defect-flagging soundness.** Per ``omnibase_infra/docker/runtime-policy.env``
   the runtime only ever loads ``ONEX_ACTIVE_RUNTIME_PACKAGES=omnibase_infra,omnimarket``
   (:data:`ACTIVE_RUNTIME_PACKAGES`), and omnimarket is the only repo that
   depends on omnibase_infra -- so omnimarket's CI is where THOSE two packages'
   nodes can be soundly flagged ``runtime_loaded`` and defect-checked.
   :func:`build_graph` FAILS CLOSED if either is missing.
2. **Census / edge-visibility completeness.** A topic produced only by a
   package outside the graph looks exactly like an orphan to every consumer
   the graph CAN see -- regardless of whether that outside package is itself
   runtime-loaded. OMN-14527 shipped with only 3 of the ~9 topic-declaring
   repos in :data:`GRAPH_PACKAGES` (``omnibase_infra``, ``omnibase_core``,
   ``omnimarket``), which left ~410 of ~1,491 statically-declared topics
   invisible (``omniclaude``, ``omniintelligence``) -- OMN-14568 closed that
   gap. Packages added for census completeness are NEVER added to
   :data:`ACTIVE_RUNTIME_PACKAGES`; they contribute producer/consumer edges
   only and are never eligible for ``runtime_loaded`` / defect-flagging.

:func:`discover_contract_roots` resolves :data:`ACTIVE_RUNTIME_PACKAGES` and
the rest of the already-installed :data:`GRAPH_PACKAGES` from installed
distributions (the same way ``runtime_host_process`` does -- these are all
pip/uv dependencies of omnimarket, so CI pays nothing extra for them).
``omniclaude`` and ``omniintelligence`` are NOT pip dependencies of
omnimarket -- adding them as such would invert the repo-layering (omnimarket
owns portable workflow packages; omniclaude is a Claude Code agent plugin)
purely to read YAML text out of packaged ``contract.yaml`` files. Since
:func:`parse_contract` only ever reads YAML -- it never imports or executes
the package -- a checked-out source tree is sufficient. Those two resolve via
``CONTRACT_GRAPH_CHECKOUT_ROOT``, an env var pointing at a directory
containing a checkout of each (see :func:`discover_contract_roots`).
:func:`build_graph` FAILS CLOSED if ANY package in :data:`GRAPH_PACKAGES` --
installed or checkout-tier -- cannot be resolved, rather than silently
reporting a smaller graph as clean. A silent partial is exactly how this
census went blind the first time.

Ratchet, not a big bang
-----------------------
The corpus already carries a large pre-existing defect population. A gate that
hard-fails all of it on day one blocks every merge and gets disabled within the
hour -- the classic way a correct check dies. So the existing defects are frozen
into a baseline that may only ever SHRINK. Any defect NOT in the baseline is a
hard failure. That stops the bleeding immediately and turns the backlog into a
burn-down list instead of an outage.

Usage::

    python -m omnimarket.validators.contract_topic_graph            # gate (ratcheted)
    python -m omnimarket.validators.contract_topic_graph --report   # full census
    python -m omnimarket.validators.contract_topic_graph --write-baseline

    # Full coverage requires the checkout-tier packages (see CHECKOUT_PACKAGES):
    CONTRACT_GRAPH_CHECKOUT_ROOT=/path/to/checkouts \\
        python -m omnimarket.validators.contract_topic_graph --report
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# The active runtime surface (omnibase_infra/docker/runtime-policy.env:64).
# These are the packages whose contracts the runtime host actually loads, and
# the ONLY packages whose nodes are ever eligible for runtime_loaded=True /
# defect-flagging (see the module docstring, "Defect-flagging soundness").
ACTIVE_RUNTIME_PACKAGES: tuple[str, ...] = ("omnibase_infra", "omnimarket")

# Resolved via importlib.util.find_spec against packages installed in the
# omnimarket venv (pip/uv dependencies already paid for by `uv sync`). Beyond
# the two active-runtime packages, these ship node contracts that participate
# as producers/consumers -- their edges matter for census completeness even
# though none of these packages is itself "active runtime".
INSTALLED_PACKAGES: tuple[str, ...] = (
    "omnibase_infra",
    "omnibase_core",
    "omnimarket",
    "omnibase_spi",
    "omnibase_compat",
    "onex_change_control",
    "omnimemory",
)

# Resolved via a filesystem checkout, NOT an import (see
# discover_contract_roots / CONTRACT_GRAPH_CHECKOUT_ROOT). These are NOT pip
# dependencies of omnimarket and never will be -- omnimarket owning a hard
# runtime dependency on omniclaude (a Claude Code agent plugin) purely to read
# packaged YAML would invert the repo-layering. parse_contract only reads
# text, so a checkout is sufficient; no import, no code execution, no new
# dependency. These packages are never in ACTIVE_RUNTIME_PACKAGES and so are
# never runtime_loaded -- they contribute producer/consumer edges only.
CHECKOUT_PACKAGES: tuple[str, ...] = ("omniclaude", "omniintelligence")

# The full set the graph is expected to cover. build_graph() fails closed if
# any of these cannot be resolved -- see the module docstring.
GRAPH_PACKAGES: tuple[str, ...] = INSTALLED_PACKAGES + CHECKOUT_PACKAGES

# Points at a directory containing a checkout of each CHECKOUT_PACKAGES repo
# (e.g. CONTRACT_GRAPH_CHECKOUT_ROOT/omniclaude/, .../omniintelligence/). CI
# populates this via extra actions/checkout steps at origin/dev HEAD -- this
# is a wiring-reconciliation oracle, so tracking dev's moving HEAD (rather
# than a pinned SHA) is the correct behavior, not a determinism bug.
CHECKOUT_ROOT_ENV = "CONTRACT_GRAPH_CHECKOUT_ROOT"

# onex.<kind>.<producer>.<event-name>.<version>
_TOPIC_RE = re.compile(r"^onex\.(evt|cmd|intent|dlq)\.[a-z0-9._-]+\.v\d+$")

DEFAULT_BASELINE = Path(__file__).parent / "data" / "contract_topic_graph_baseline.yaml"

DefectClass = Literal[
    "ORPHANED_CONSUMER",
    "ORPHANED_PRODUCER",
    "DECLARED_BUT_UNWIRED",
    "DISCONNECTED_SUBGRAPH",
]


class ModelContractNode(BaseModel):
    """One contract.yaml, reduced to its graph-relevant surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    package: str
    path: str
    publish_topics: tuple[str, ...] = ()
    subscribe_topics: tuple[str, ...] = ()
    command_topic: str | None = None
    terminal_topics: tuple[str, ...] = ()
    externally_consumed: tuple[str, ...] = ()
    has_dispatch_wiring: bool = False
    runtime_loaded: bool = False


class ModelGraphFinding(BaseModel):
    """A single static defect on the contract graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defect: DefectClass
    topic: str | None = None
    node: str | None = None
    package: str = ""
    detail: str = ""

    def key(self) -> str:
        """Stable identity used for baseline matching."""
        return f"{self.defect}::{self.node or '-'}::{self.topic or '-'}"


class ModelTopicGraph(BaseModel):
    """The contract graph: nodes are contracts, edges are topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[ModelContractNode, ...]
    producers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    consumers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Topics an external, non-contract actor legitimately publishes (the skill
    # CLI, a GitHub webhook, the omniclaude hook daemon). Declared, never assumed.
    external_producers: dict[str, str] = Field(default_factory=dict)

    @property
    def topics(self) -> set[str]:
        return set(self.producers) | set(self.consumers)

    def edges(self) -> list[tuple[str, str, str]]:
        """Every (producer_node, topic, consumer_node) triple."""
        out: list[tuple[str, str, str]] = []
        for topic, prods in self.producers.items():
            for consumer in self.consumers.get(topic, ()):
                for producer in prods:
                    out.append((producer, topic, consumer))
        return out

    def is_reachable(self, topic: str) -> bool:
        """Can anything, anywhere, ever put a message on this topic?"""
        if self.producers.get(topic):
            return True
        if topic in self.external_producers:
            return True
        # A node's runtime_dispatch.command_topic is the CLI's publish target,
        # so declaring one IS an entry point even with no contract producer.
        return any(n.command_topic == topic for n in self.nodes)


def _collect_topics(value: object) -> list[str]:
    """Flatten any nested YAML value into the ONEX topic literals it contains."""
    if isinstance(value, str):
        candidate = value.strip()
        return [candidate] if _TOPIC_RE.match(candidate) else []
    if isinstance(value, list):
        return [t for item in value for t in _collect_topics(item)]
    if isinstance(value, dict):
        return [t for item in value.values() for t in _collect_topics(item)]
    return []


def _dig(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _has_dispatch_entry(handler_routing: object) -> bool:
    """True only if ``handler_routing`` names a real, dispatchable handler.

    A bare ``{"handlers": []}`` or a mapping missing ``handlers`` entirely is
    NOT wired -- the runtime creates the Kafka subscription and has nowhere to
    dispatch the event it receives ("No dispatcher found"). Each
    ``handlers[]`` entry must resolve a module plus a class/name, matching the
    shape RuntimeLocal actually dispatches on: ``handler: {module, name}`` (or
    ``class``). ``default_handler`` is the other real dispatch path, declared
    as a non-empty ``"module:ClassName"`` string.
    """
    if not isinstance(handler_routing, dict):
        return False
    handlers = handler_routing.get("handlers")
    if isinstance(handlers, list):
        for entry in handlers:
            if not isinstance(entry, dict):
                continue
            target = entry.get("handler")
            if (
                isinstance(target, dict)
                and target.get("module")
                and (target.get("name") or target.get("class"))
            ):
                return True
    default_handler = handler_routing.get("default_handler")
    if isinstance(default_handler, str) and default_handler.strip():
        return True
    return False


def _has_top_level_handler(handler: object) -> bool:
    """True only if a top-level ``handler:`` names a module and a class/name."""
    return bool(
        isinstance(handler, dict)
        and handler.get("module")
        and (handler.get("class") or handler.get("name"))
    )


def parse_contract(path: Path, package: str) -> ModelContractNode | None:
    """Reduce one contract.yaml to its graph surface.

    The corpus declares topics through roughly a dozen different key shapes that
    accreted over time (``event_bus.publish_topics``, ``event_bus.publish.success_topic``,
    ``runtime_dispatch.terminal_events``, ``published_events[].topic``, ...). All of
    them are real and all of them are load-bearing, so this reads the union. A
    parser that knew only the dominant shape would silently miss ~40% of the
    declarations and report the resulting phantom orphans as defects.

    Note the contract's ``name:`` is authoritative and is NOT the directory name.
    """
    try:
        data = yaml.safe_load(path.read_text(errors="replace"))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid contract YAML: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Contract must contain a YAML mapping: {path}")

    raw_bus = data.get("event_bus")
    bus: dict[str, object] = raw_bus if isinstance(raw_bus, dict) else {}

    published: list[str] = []
    published += _collect_topics(bus.get("publish_topics"))
    published += _collect_topics(_dig(bus, "publish", "success_topic"))
    published += _collect_topics(_dig(bus, "publish", "failure_topic"))
    published += _collect_topics(_dig(data, "runtime_dispatch", "terminal_events"))
    for key in ("published_events", "produced_events", "publications"):
        published += _collect_topics(data.get(key))
    published += _collect_topics(_dig(data, "topics", "produces"))

    subscribed: list[str] = []
    subscribed += _collect_topics(bus.get("subscribe_topics"))
    subscribed += _collect_topics(_dig(bus, "subscribe", "topic"))
    for key in ("consumed_events", "subscribed_events", "input_subscriptions"):
        subscribed += _collect_topics(data.get(key))
    subscribed += _collect_topics(_dig(data, "topics", "consumes"))
    subscribed += _collect_topics(_dig(data, "subscriptions", "topics"))

    command_topic = _dig(data, "runtime_dispatch", "command_topic")
    if not (isinstance(command_topic, str) and _TOPIC_RE.match(command_topic)):
        command_topic = None
    else:
        # The node consumes commands here; the CLI dispatcher is the producer.
        subscribed.append(command_topic)

    # runtime_dispatch.terminal_events are the success/failure results the CLI
    # dispatcher itself awaits. They have an external consumer by construction, so
    # "no contract subscribes to them" is the design, not an orphaned producer.
    terminal_topics = tuple(
        sorted(set(_collect_topics(_dig(data, "runtime_dispatch", "terminal_events"))))
    )

    # The runtime host only loads <package>/nodes/*/contract.yaml direct children
    # (_discover_package_node_contracts), and only for active runtime packages.
    parts = path.parts
    is_node_dir = "nodes" in parts and parts.index("nodes") == len(parts) - 3
    runtime_loaded = (
        package in ACTIVE_RUNTIME_PACKAGES
        and is_node_dir
        and not bus.get("plugin_managed")
    )

    return ModelContractNode(
        name=str(
            data.get("name")
            or data.get("contract_name")
            or data.get("node_name")
            or path.parent.name
        ),
        package=package,
        path=str(path),
        publish_topics=tuple(sorted(set(published))),
        subscribe_topics=tuple(sorted(set(subscribed))),
        command_topic=command_topic,
        terminal_topics=terminal_topics,
        externally_consumed=tuple(
            sorted(set(_collect_topics(data.get("externally_consumed_topics"))))
        ),
        # A subscription is wired to a dispatcher by handler_routing (topic/operation
        # match) or by a single top-level handler. Without either, the runtime still
        # creates the Kafka subscription -- it just has nowhere to dispatch the event
        # it receives, and drops it ("No dispatcher found"). Truthiness of the
        # handler_routing MAPPING is not enough -- {"handlers": []} is truthy and
        # still undispatchable, so this checks for an actual resolvable entry.
        has_dispatch_wiring=_has_dispatch_entry(data.get("handler_routing"))
        or _has_top_level_handler(data.get("handler")),
        runtime_loaded=runtime_loaded,
    )


def discover_contract_roots() -> dict[str, Path]:
    """Resolve every graph package to its on-disk root -- two different ways.

    :data:`INSTALLED_PACKAGES` resolve from the installed distribution, the
    same way ``runtime_host_process._discover_package_node_contracts`` does:
    contracts ship as package data, and these are all pip/uv dependencies of
    omnimarket already, so CI needs no checkout for them.

    :data:`CHECKOUT_PACKAGES` resolve from a filesystem checkout under
    ``CONTRACT_GRAPH_CHECKOUT_ROOT`` instead. They are deliberately NOT pip
    dependencies of omnimarket (see the module docstring), and
    :func:`parse_contract` only ever reads YAML text -- never imports or
    executes the package -- so a checkout is sufficient.

    FAILS CLOSED when ``PYTHONPATH`` is set. An ambient PYTHONPATH silently
    reroutes ``find_spec`` from the pinned wheel to a local canonical clone, so
    the graph is built from a DIFFERENT contract corpus than the one CI resolves
    and the gate returns a different verdict on the same commit depending on whose
    shell it ran in. That is not a hypothetical: developing this validator, the
    ambient PYTHONPATH pointed at the omnibase_infra clone and produced 9 defects
    that do not exist in the pinned wheel. A gate whose answer depends on the
    caller's environment is not a gate. Run with ``env -u PYTHONPATH``.
    """
    shadowed = os.environ.get("PYTHONPATH")
    if shadowed:
        raise RuntimeError(
            "Contract graph refuses to run with PYTHONPATH set "
            f"({shadowed!r}): it reroutes package resolution away from the pinned "
            "wheels, so the graph would be built from a different corpus than CI "
            "resolves and the verdict would depend on the caller's shell. "
            "Re-run with: env -u PYTHONPATH"
        )

    roots: dict[str, Path] = {}
    for package in INSTALLED_PACKAGES:
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.origin:
            continue
        roots[package] = Path(spec.origin).parent

    checkout_root_raw = os.environ.get(CHECKOUT_ROOT_ENV)
    if checkout_root_raw:
        checkout_root = Path(checkout_root_raw)
        for package in CHECKOUT_PACKAGES:
            checkout = checkout_root / package
            # Scope to the package's OWN src tree, never the raw repo checkout:
            # a local dev checkout can carry its own .venv with OTHER packages
            # vendored as installed dependencies inside it (e.g. omniclaude's
            # .venv ships an installed omniintelligence), and rglob-ing the
            # whole repo root would silently re-scan and mis-attribute those
            # nested copies under this package's name -- duplicate nodes,
            # inflated counts, and a real DISCONNECTED_SUBGRAPH false-positive
            # from cross-linking two copies of the same contract.
            src_layout = checkout / "src" / package
            candidate = src_layout if src_layout.is_dir() else checkout
            if candidate.is_dir():
                roots[package] = candidate
    return roots


def build_graph(
    roots: dict[str, Path] | None = None,
    external_producers: dict[str, str] | None = None,
) -> ModelTopicGraph:
    """Build the contract graph, failing closed on an incomplete package surface."""
    roots = roots if roots is not None else discover_contract_roots()

    missing_runtime = [p for p in ACTIVE_RUNTIME_PACKAGES if p not in roots]
    if missing_runtime:
        raise RuntimeError(
            "Contract graph is UNSOUND without the full active runtime surface: "
            f"missing {missing_runtime}. A partial graph reports every cross-package "
            "producer as a phantom orphan. Run this where "
            f"{list(ACTIVE_RUNTIME_PACKAGES)} are co-installed (omnimarket)."
        )

    # Beyond runtime-soundness, the census must cover every topic-declaring
    # package or it reinvents the exact blind spot OMN-14568 fixed: a topic
    # whose producer lives in an unscanned package looks exactly like an
    # orphan to every consumer the graph CAN see. No silent shrinking.
    missing_census = [p for p in GRAPH_PACKAGES if p not in roots]
    if missing_census:
        missing_checkout = [p for p in missing_census if p in CHECKOUT_PACKAGES]
        hint = (
            f" Set {CHECKOUT_ROOT_ENV} to a directory containing a checkout of "
            f"{missing_checkout} -- parse_contract only reads YAML, no import "
            "or install required."
            if missing_checkout
            else ""
        )
        raise RuntimeError(
            f"Contract graph is INCOMPLETE: missing package roots for {missing_census}."
            f"{hint}"
        )

    nodes: list[ModelContractNode] = []
    for package, root in sorted(roots.items()):
        for contract_path in sorted(root.rglob("contract.yaml")):
            node = parse_contract(contract_path, package)
            if node is not None:
                nodes.append(node)

    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for node in nodes:
        for topic in node.publish_topics:
            producers.setdefault(topic, []).append(node.name)
        for topic in node.subscribe_topics:
            consumers.setdefault(topic, []).append(node.name)

    return ModelTopicGraph(
        nodes=tuple(nodes),
        producers={t: tuple(v) for t, v in producers.items()},
        consumers={t: tuple(v) for t, v in consumers.items()},
        external_producers=dict(external_producers or {}),
    )


def _nodes_by_name(
    nodes: Sequence[ModelContractNode],
) -> dict[str, list[ModelContractNode]]:
    """Every node sharing a bare contract `name`, in declaration order.

    OMN-14575: a bare `name` is not unique across packages --
    node_intelligence_orchestrator exists in both omnimarket and
    omniintelligence, node_skill_dispatch_engine_orchestrator in both
    omnimarket and omniclaude (real, pre-existing duplicates, surfaced once
    OMN-14568 broadened the scan far enough to see both copies at once). A
    single ``{name: node}`` dict silently collapses to whichever copy sorts
    last, attributing one package's topics to the OTHER package's
    runtime_loaded flag. Keep every candidate so callers can disambiguate by
    which one actually declares the topic in question.
    """
    out: dict[str, list[ModelContractNode]] = {}
    for node in nodes:
        out.setdefault(node.name, []).append(node)
    return out


def _resolve_node_for_topic(
    by_name: dict[str, list[ModelContractNode]],
    name: str,
    topic: str,
    *,
    consumer: bool,
) -> ModelContractNode | None:
    """The node that actually declares `topic` under `name`.

    When `name` is unambiguous this is just a dict lookup. When it collides
    across packages (see :func:`_nodes_by_name`), pick the candidate that
    genuinely subscribes/publishes `topic` -- not an arbitrary one that merely
    happens to share the name.
    """
    candidates = by_name.get(name, [])
    if len(candidates) == 1:
        return candidates[0]
    for node in candidates:
        if topic in (node.subscribe_topics if consumer else node.publish_topics):
            return node
    return candidates[0] if candidates else None


def find_defects(graph: ModelTopicGraph) -> list[ModelGraphFinding]:
    """Every static defect the graph can prove, without a broker."""
    findings: list[ModelGraphFinding] = []
    by_name = _nodes_by_name(graph.nodes)

    externally_consumed: set[str] = set()
    for node in graph.nodes:
        externally_consumed.update(node.externally_consumed)
        # The CLI dispatcher awaits these; they are consumed off-graph by design.
        externally_consumed.update(node.terminal_topics)

    # --- ORPHANED CONSUMER: subscribed, but nothing can ever publish it.
    # This is HandlerLedgerAppend: alive, healthy, starved for its entire life.
    for topic in sorted(graph.consumers):
        if graph.is_reachable(topic):
            continue
        for consumer in graph.consumers[topic]:
            consumer_node = _resolve_node_for_topic(
                by_name, consumer, topic, consumer=True
            )
            if consumer_node is None or not consumer_node.runtime_loaded:
                continue
            findings.append(
                ModelGraphFinding(
                    defect="ORPHANED_CONSUMER",
                    topic=topic,
                    node=consumer,
                    package=consumer_node.package,
                    detail=(
                        "subscribes to a topic that NO contract publishes, that is not "
                        "declared as an external producer, and that is no node's "
                        "runtime_dispatch.command_topic — nothing can ever send it a message"
                    ),
                )
            )

    # --- ORPHANED PRODUCER: published, but nothing consumes it. Output goes nowhere.
    for topic in sorted(graph.producers):
        if graph.consumers.get(topic) or topic in externally_consumed:
            continue
        for producer in graph.producers[topic]:
            producer_node = _resolve_node_for_topic(
                by_name, producer, topic, consumer=False
            )
            if producer_node is None or not producer_node.runtime_loaded:
                continue
            findings.append(
                ModelGraphFinding(
                    defect="ORPHANED_PRODUCER",
                    topic=topic,
                    node=producer,
                    package=producer_node.package,
                    detail=(
                        "publishes a topic that NO contract subscribes to and that is not "
                        "declared in externally_consumed_topics — its output goes nowhere"
                    ),
                )
            )

    # --- DECLARED BUT UNWIRED: the runtime creates the subscription, then has no
    # dispatcher for what arrives. Events are consumed and silently dropped.
    for node in graph.nodes:
        if (
            node.runtime_loaded
            and node.subscribe_topics
            and not node.has_dispatch_wiring
        ):
            findings.append(
                ModelGraphFinding(
                    defect="DECLARED_BUT_UNWIRED",
                    node=node.name,
                    package=node.package,
                    detail=(
                        f"declares {len(node.subscribe_topics)} subscribe topic(s) with NO "
                        "handler_routing and NO handler — the runtime subscribes, receives, "
                        "and drops every event because there is nothing to dispatch to"
                    ),
                )
            )

    # --- DISCONNECTED SUBGRAPH: a fully-wired cluster with no inbound edge from
    # the rest of the platform. This is how two rival ledger systems both sat idle.
    findings.extend(_find_disconnected_subgraphs(graph, by_name))
    return findings


def _find_disconnected_subgraphs(
    graph: ModelTopicGraph, by_name: dict[str, list[ModelContractNode]]
) -> list[ModelGraphFinding]:
    """Weakly-connected components containing no reachable entry point.

    A component is live only if SOME topic in it can be fed from outside: a
    contract producer beyond the component, a declared external producer, or a
    runtime_dispatch entry point. A component where every inbound topic is
    unreachable can never run, however perfectly its internals are wired.

    KNOWN RESIDUAL (OMN-14575): the adjacency/component identity here is still
    bare-name-keyed, so a name collision across packages can still conflate
    two distinct nodes into one graph vertex (unlike the ORPHANED_CONSUMER/
    ORPHANED_PRODUCER checks above, which now disambiguate by topic). Not
    fixed here -- none of OMN-14568's 11 misattributed findings were this
    class; fixing this fully needs the adjacency graph itself keyed on
    (package, name), a larger change than this ticket's scope.
    """
    adjacency: dict[str, set[str]] = {n.name: set() for n in graph.nodes}
    for producer, _topic, consumer in graph.edges():
        adjacency.setdefault(producer, set()).add(consumer)
        adjacency.setdefault(consumer, set()).add(producer)

    seen: set[str] = set()
    findings: list[ModelGraphFinding] = []
    for node in graph.nodes:
        if node.name in seen or not node.runtime_loaded:
            continue
        # Flood-fill the weakly-connected component.
        component: set[str] = set()
        stack = [node.name]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency.get(current, set()) - component)
        seen |= component

        # A single node with no edges at all is covered by the orphan checks;
        # the subgraph check is about CLUSTERS that are internally wired but fed
        # by nothing.
        if len(component) < 2:
            continue

        has_entry = False
        for member in component:
            member_candidates = by_name.get(member, [])
            member_node = member_candidates[0] if member_candidates else None
            if member_node is None:
                continue
            if member_node.command_topic is not None:
                has_entry = True
                break
            for topic in member_node.subscribe_topics:
                if topic in graph.external_producers:
                    has_entry = True
                    break
                # Fed by a producer OUTSIDE this component => has an inbound edge.
                if any(p not in component for p in graph.producers.get(topic, ())):
                    has_entry = True
                    break
            if has_entry:
                break

        if not has_entry:
            leader = min(component)
            findings.append(
                ModelGraphFinding(
                    defect="DISCONNECTED_SUBGRAPH",
                    node=leader,
                    package=by_name[leader][0].package,
                    detail=(
                        f"{len(component)} internally-wired nodes with NO inbound edge from the "
                        f"rest of the platform and no entry point: {sorted(component)} — "
                        "the cluster is fully wired and can never receive a message"
                    ),
                )
            )
    return findings


class ModelBaseline(BaseModel):
    """Frozen pre-existing defects. May only ever shrink."""

    model_config = ConfigDict(extra="forbid")

    external_producers: dict[str, str] = Field(default_factory=dict)
    accepted: list[str] = Field(default_factory=list)


def load_baseline(path: Path) -> ModelBaseline:
    if not path.is_file():
        return ModelBaseline()
    data = yaml.safe_load(path.read_text()) or {}
    return ModelBaseline.model_validate(data)


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def merge_base_accepted_keys(baseline_path: Path) -> set[str] | None:
    """Return the ``accepted`` keys frozen in the baseline AT THE MERGE-BASE.

    The PR-controlled baseline on disk cannot be trusted to gate itself: a PR
    can add a real new defect and its exact key to the baseline in the same
    commit (by hand, or via ``--write-baseline``), which makes ``new_defects``
    empty against the PR's own copy -- the advertised shrink-only ratchet is
    then not enforced at all. This resolves the SAME file's content at the
    merge-base with the PR's target branch instead, so a key can only ever
    count as "already accepted" if it predates this PR.

    Returns ``None`` (best-effort, not a hard requirement) when the merge base
    cannot be determined -- a shallow local clone, a detached/unpushed branch,
    or ``git`` being unavailable. Callers fall back to trusting the PR-local
    baseline in that case rather than hard-failing every run on an
    environment limitation unrelated to the actual defect population.
    """
    repo_root = baseline_path.resolve().parent
    while repo_root != repo_root.parent and not (repo_root / ".git").exists():
        repo_root = repo_root.parent
    if not (repo_root / ".git").exists():
        return None

    rel_path = baseline_path.resolve().relative_to(repo_root).as_posix()
    target_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    candidates = (
        [f"origin/{target_ref}"] if target_ref else ["origin/dev", "origin/main"]
    )
    for candidate in candidates:
        merge_base = _run_git(["merge-base", "HEAD", candidate], cwd=repo_root)
        if not merge_base:
            continue
        content = _run_git(["show", f"{merge_base}:{rel_path}"], cwd=repo_root)
        if content is None:
            continue
        try:
            data = yaml.safe_load(content) or {}
            baseline = ModelBaseline.model_validate(data)
        except (yaml.YAMLError, ValueError):
            continue
        return set(baseline.accepted)
    return None


def evaluate_ratchet(
    findings: list[ModelGraphFinding],
    local_accepted: set[str],
    trusted_accepted: set[str],
) -> tuple[list[ModelGraphFinding], list[str]]:
    """Split ``findings`` into ``(new_defects, fixed)`` against the ratchet.

    ``trusted_accepted`` decides what counts as "already accepted" -- it must
    be the merge-base baseline (or, as a documented fallback, the PR-local
    one), NEVER a baseline this same run could have just written. A key is a
    new defect the instant it is not in ``trusted_accepted``, regardless of
    whether ``local_accepted`` (the PR's own, possibly-just-edited, baseline
    file) also contains it -- that is what stops a PR from adding a real
    defect and its baseline entry in the same commit.

    ``fixed`` is evaluated against ``local_accepted`` instead: a key the
    PR-local baseline still claims to accept, but that no longer corresponds
    to any current finding, must be removed from the baseline so it cannot
    silently re-authorize a regression later.
    """
    current = {f.key(): f for f in findings}
    new_defects = [
        f for key, f in sorted(current.items()) if key not in trusted_accepted
    ]
    fixed = sorted(local_accepted - set(current))
    return new_defects, fixed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", action="store_true", help="print the full census")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="freeze current defects (ratchet reset)",
    )
    args = parser.parse_args(argv)

    baseline = load_baseline(args.baseline)
    graph = build_graph(external_producers=baseline.external_producers)
    findings = find_defects(graph)

    if args.write_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            yaml.safe_dump(
                {
                    "external_producers": baseline.external_producers,
                    "accepted": sorted({f.key() for f in findings}),
                },
                sort_keys=True,
                default_flow_style=False,
            )
        )
        sys.stdout.write(f"Froze {len(findings)} defect(s) into {args.baseline}\n")
        return 0

    if args.report:
        sys.stdout.write(
            f"contracts={len(graph.nodes)} topics={len(graph.topics)} edges={len(graph.edges())}\n"
        )
        for finding in sorted(
            findings, key=lambda f: (f.defect, f.node or "", f.topic or "")
        ):
            sys.stdout.write(
                f"  {finding.defect:<22} {finding.node or '-':<45} {finding.topic or ''}\n"
            )

    accepted = set(baseline.accepted)

    # Trust the merge-base's baseline, not this PR's own copy, to decide what
    # counts as "already accepted" -- otherwise a PR could add a real new
    # defect and its baseline entry in the same commit and the ratchet would
    # never see it. Falls back to the PR-local baseline only when the merge
    # base genuinely cannot be resolved (see merge_base_accepted_keys).
    trusted_accepted = merge_base_accepted_keys(args.baseline)
    if trusted_accepted is None:
        sys.stderr.write(
            "::warning::contract-topic-graph could not resolve the merge-base "
            "baseline (shallow clone or detached checkout) -- falling back to "
            "the PR-local baseline for this run's new-defect check.\n"
        )
        trusted_accepted = accepted

    new_defects, fixed = evaluate_ratchet(findings, accepted, trusted_accepted)

    if new_defects:
        lines = [
            "",
            f"CONTRACT GRAPH GATE FAILED — {len(new_defects)} new static defect(s).",
            "",
            "The contract graph proves these are broken WITHOUT running anything:",
            "",
        ]
        for finding in new_defects:
            lines.append(f"  [{finding.defect}] {finding.node} ({finding.package})")
            if finding.topic:
                lines.append(f"      topic: {finding.topic}")
            lines.append(f"      {finding.detail}")
            lines.append("")
        lines.append(
            "Fix the wiring. Do NOT add these to the baseline — the baseline is frozen "
            "pre-existing debt and may only shrink."
        )
        sys.stderr.write("\n".join(lines) + "\n")
        return 1

    if fixed:
        sys.stderr.write(
            f"\nCONTRACT GRAPH GATE FAILED — {len(fixed)} baselined defect(s) are now FIXED.\n\n"
            "Remove them from the baseline so the ratchet cannot silently re-authorize a\n"
            "regression:\n\n" + "\n".join(f"  {key}" for key in fixed) + "\n"
        )
        return 1

    sys.stdout.write(
        f"OK: contract graph clean — {len(graph.nodes)} contracts, {len(graph.topics)} topics, "
        f"{len(graph.edges())} edges, {len(accepted)} baselined defect(s) pending burn-down.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
