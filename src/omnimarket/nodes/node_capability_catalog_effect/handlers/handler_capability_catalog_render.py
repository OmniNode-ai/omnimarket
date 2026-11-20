# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Byte-deterministic renderers: ``catalog.json`` to markdown, digest, brief part and doc blocks.

Two renders of the same :class:`Catalog` and the
same ``static_facts.yaml`` are always byte-identical (``test_render.py -k deterministic``): every
collection is sorted by its own stable key before rendering, and nothing here reads the clock, the
environment or the network. The publisher supplies ``generated_at``; this module only prints
it.

``ProbeState.UNKNOWN`` and ``LaneStatus.UNKNOWN`` never render as up or free (and
``test_render.py -k unknown_is_never_up``): every renderer below maps each state through
:func:`host_state_word` or :func:`lane_status_word` rather than testing truthiness, so an unknown
state has no code path that reaches "up" or "FREE".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..models.model_capability_catalog import (
    Catalog,
    Host,
    Lane,
    LaneStatus,
    ProbeState,
)
from ..models.model_capability_catalog_render_request import (
    ModelCapabilityCatalogRenderRequest,
)
from ..models.model_capability_catalog_render_result import (
    ModelCapabilityCatalogRenderResult,
)

DEFAULT_STATIC_FACTS_PATH = Path(__file__).with_name("static_facts.yaml")

DIGEST_LINE_BUDGET = 40


def load_static_facts(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_STATIC_FACTS_PATH
    with target.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    return data


def host_state_word(host: Host) -> str:
    """Render one host's state. UNKNOWN and OUT OF SERVICE never collapse into "up"."""
    if host.out_of_service_reason:
        return f"OUT OF SERVICE ({host.out_of_service_reason})"
    if host.state is ProbeState.OK:
        return "up"
    if host.state is ProbeState.DOWN:
        return "down"
    return "UNKNOWN"


def lane_status_word(lane: Lane) -> str:
    """Render one lane's status. Only LaneStatus.FREE ever renders "FREE"."""
    if lane.status is LaneStatus.FREE:
        return "FREE"
    if lane.status is LaneStatus.BUSY:
        return "BUSY"
    if lane.status is LaneStatus.OUT_OF_SERVICE:
        return "OUT OF SERVICE"
    return "UNKNOWN"


def _sorted_hosts(catalog: Catalog) -> list[Host]:
    return sorted(catalog.hosts, key=lambda h: h.host_id)


def _sorted_lanes(catalog: Catalog) -> list[Lane]:
    return sorted(catalog.lanes, key=lambda lane: (lane.host_id, lane.lane_id))


def render_digest(
    catalog: Catalog,
    static_facts: dict[str, Any],
    sections: list[str] | None = None,
) -> str:
    """Render the at-most-40-line digest the session-start hook and the briefs paste."""
    wanted = (
        set(sections) if sections else {"pool", "engines", "skills", "ticks", "lag"}
    )
    lines: list[str] = [f"CAPABILITIES generated_at={catalog.generated_at}"]

    if "pool" in wanted:
        hosts = _sorted_hosts(catalog)
        host_words = (
            ", ".join(f"{h.host_id}={host_state_word(h)}" for h in hosts)
            or "(none declared)"
        )
        lines.append(f"HOSTS: {host_words}")

        lanes = _sorted_lanes(catalog)
        lane_words = (
            ", ".join(
                f"{lane.host_id}/{lane.lane_id}={lane_status_word(lane)}"
                for lane in lanes
            )
            if lanes
            else "(none declared)"
        )
        lines.append(f"LANES: {lane_words}")

    if "engines" in wanted:
        engines = static_facts.get("engines", [])
        live_by_id = {e.engine_id: e for e in catalog.engines}
        engine_words = []
        for entry in engines:
            engine_id = str(entry["engine_id"])
            live = live_by_id.get(engine_id)
            state = live.state.value if live is not None else ProbeState.UNKNOWN.value
            engine_words.append(f"{entry['display_name']}={state}")
        lines.append(
            "ENGINES: "
            + (", ".join(engine_words) if engine_words else "(none declared)")
        )

    if "skills" in wanted:
        skills = static_facts.get("front_door_skills", [])
        lines.append(
            "SKILLS: "
            + (", ".join(str(s) for s in skills) if skills else "(none declared)")
        )

    if "ticks" in wanted:
        jobs = sorted(catalog.launchd_jobs, key=lambda j: j.label)
        tick_words = (
            ", ".join(f"{j.label}={j.state}" for j in jobs)
            if jobs
            else "(none declared)"
        )
        lines.append(f"TICKS: {tick_words}")

    if "lag" in wanted:
        plugins = sorted(catalog.plugins, key=lambda p: p.name)
        lagging = [
            p
            for p in plugins
            if p.available_version and p.available_version != p.installed_version
        ]
        lag_words = (
            ", ".join(
                f"{p.name} {p.installed_version}->{p.available_version}"
                for p in lagging
            )
            if lagging
            else "none"
        )
        lines.append(f"LAG: {lag_words}")

    if len(lines) > DIGEST_LINE_BUDGET:
        lines = lines[:DIGEST_LINE_BUDGET]
    return "\n".join(lines) + "\n"


def render_brief_part(catalog: Catalog, static_facts: dict[str, Any]) -> str:
    """Render the "3a.10 Lab pool and delegate engines" part pasted into every brief."""
    lines = [
        "## 3a.10 Lab pool and delegate engines",
        "",
        render_digest(catalog, static_facts).rstrip("\n"),
    ]
    return "\n".join(lines) + "\n"


def render_full_markdown(catalog: Catalog, static_facts: dict[str, Any]) -> str:
    """Render the full ``reference/capability-catalog.md`` replacement.

    the publisher headers and publishes the result; this function only renders the body.
    """
    lines: list[str] = [
        "# Capability catalog",
        "",
        f"generated_at: {catalog.generated_at}",
        "",
        "## Hosts",
        "",
        "| Host | State |",
        "| - | - |",
    ]
    for host in _sorted_hosts(catalog):
        lines.append(
            f"| {host.host_id} ({host.display_name}) | {host_state_word(host)} |"
        )

    lines += [
        "",
        "## Lanes",
        "",
        "| Host | Lane | Status | Note |",
        "| - | - | - | - |",
    ]
    for lane in _sorted_lanes(catalog):
        lines.append(
            f"| {lane.host_id} | {lane.lane_id} | {lane_status_word(lane)} | {lane.note or ''} |"
        )

    lines += ["", "## Engines", "", "| Engine | State | Invocation |", "| - | - | - |"]
    live_engines_by_id = {e.engine_id: e for e in catalog.engines}
    for entry in static_facts.get("engines", []):
        engine_id = str(entry["engine_id"])
        live = live_engines_by_id.get(engine_id)
        state = live.state.value if live is not None else ProbeState.UNKNOWN.value
        lines.append(f"| {entry['display_name']} | {state} | `{entry['invocation']}` |")

    lines += ["", "## Front-door skills", ""]
    for skill in static_facts.get("front_door_skills", []):
        lines.append(f"- `{skill}`")

    lines += [
        "",
        "## Launchd jobs",
        "",
        "| Label | State | Successor |",
        "| - | - | - |",
    ]
    for job in sorted(catalog.launchd_jobs, key=lambda j: j.label):
        lines.append(f"| {job.label} | {job.state} | {job.successor or ''} |")

    has_updates = any(p.update_command for p in catalog.plugins)
    lines += [
        "",
        "## Plugins",
        "",
        "| Plugin | Installed | Available |" + (" Update |" if has_updates else ""),
        "| - | - | - |" + (" - |" if has_updates else ""),
    ]
    for plugin in sorted(catalog.plugins, key=lambda p: p.name):
        lines.append(
            f"| {plugin.name} | {plugin.installed_version} | {plugin.available_version or ''} |"
            + (f" {plugin.update_command or ''} |" if has_updates else "")
        )

    lines += ["", "## Credential refs", "", "| Ref | Provider |", "| - | - |"]
    for ref in sorted(catalog.credential_refs, key=lambda c: c.secret_ref):
        lines.append(f"| {ref.secret_ref} | {ref.provider} |")

    return "\n".join(lines) + "\n"


def render_claude_md_block(catalog: Catalog) -> str:
    """Render the lane-table block the docs checker splices into ``CLAUDE.md`` and ``AGENTS.md``."""
    lines = [
        "<!-- GENERATED_LANE_TABLE:start -->",
        "| Host | Lane | Status |",
        "| - | - | - |",
    ]
    for lane in _sorted_lanes(catalog):
        lines.append(f"| {lane.host_id} | {lane.lane_id} | {lane_status_word(lane)} |")
    lines.append("<!-- GENERATED_LANE_TABLE:end -->")
    return "\n".join(lines) + "\n"


def find_term(catalog: Catalog, static_facts: dict[str, Any], term: str) -> list[str]:
    """Return every line naming ``term`` anywhere in the catalog or static facts (the `find` verb)."""
    needle = term.lower()
    hits: list[str] = []

    for entry in static_facts.get("engines", []):
        haystacks = [
            str(entry.get("engine_id", "")),
            str(entry.get("display_name", "")),
        ]
        if any(needle in h.lower() for h in haystacks):
            ref = entry.get("credential_ref")
            ref_text = f" credential_ref={ref}" if ref else ""
            hits.append(
                f"engine {entry['display_name']}: {entry['invocation']}{ref_text}"
            )

    for ref in catalog.credential_refs:
        if needle in ref.secret_ref.lower() or needle in ref.provider.lower():
            hits.append(f"credential_ref {ref.secret_ref} (provider={ref.provider})")

    for skill in static_facts.get("front_door_skills", []):
        if needle in str(skill).lower():
            hits.append(f"front-door skill {skill}")

    for host in catalog.hosts:
        if needle in host.host_id.lower() or needle in host.display_name.lower():
            hits.append(
                f"host {host.host_id} ({host.display_name}): {host_state_word(host)}"
            )

    for lane in catalog.lanes:
        if needle in lane.lane_id.lower():
            hits.append(f"lane {lane.host_id}/{lane.lane_id}: {lane_status_word(lane)}")

    for entry in static_facts.get("superseded_tools", []):
        if needle in str(entry.get("superseded", "")).lower():
            hits.append(f"superseded: {entry['superseded']} -> {entry['by']}")

    return hits


class HandlerCapabilityCatalogRender:
    """Render every view of one catalog. Pure: no clock, environment, network or disk write."""

    def handle(
        self, request: ModelCapabilityCatalogRenderRequest
    ) -> ModelCapabilityCatalogRenderResult:
        facts = dict(request.static_facts)
        views = {
            "capability-catalog.md": render_full_markdown(request.catalog, facts),
            "digest.txt": render_digest(request.catalog, facts),
            "brief-part.md": render_brief_part(request.catalog, facts),
        }
        if request.target == "claude-md":
            views["claude-md-block.md"] = render_claude_md_block(request.catalog)
        return ModelCapabilityCatalogRenderResult(views=views)
