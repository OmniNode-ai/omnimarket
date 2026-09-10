# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a routing tier that references a backend no lane can bind.

OMN-16833. ``_load_bifrost_endpoints`` drops any backend without a complete
``endpoint_url`` **silently**, so ``routing_tiers.yaml`` can point a tier at a
backend that resolves to nothing on every lane in the fleet and no surface
anywhere says so. The rung is declared and unreachable at the same time — the
decorative-rung class the OMN-15630 routing-completeness gate exists to forbid —
and the observable effect is that the cheapest-first ladder degrades to
paid-first for the task classes that rung was the local answer for.

Two binding paths exist for a tier-referenced backend, and only two.

``tier: local`` backends are legitimately ``endpoint_url: null`` in this repo's
committed contract. They are bound per lane by the typed overlay in
``omnibase_infra`` (``docker/lane-overlays/<lane>.bifrost.yaml``, validated by
``ModelBifrostLaneBackendBinding``), which admits only unauthenticated local
``http://`` chat-completions endpoints on an authorized host table. This repo
cannot read that table, so the servable set is named here explicitly with its
probe evidence — see ``LANE_BOUND_LOCAL_BACKENDS``.

Every other backend must carry a concrete ``endpoint_url`` in the committed
contract, because the lane overlay cannot represent it and
``render_bifrost_delegation_contract`` "never reads endpoint or model bindings
from the process environment" (its own module docstring). A non-local backend
referenced by a tier with ``endpoint_url: null`` is therefore unbindable by
construction on every lane.

Parking an unbindable backend's DECLARATION is fine and often right. Leaving a
tier POINTING at it is what this validator refuses.

Run standalone (pre-commit hook ``routing-tier-backend-bindability`` and the
``routing-tier-bindability`` CI job both call this entry point):

    uv run python -m omnimarket.validators.routing_tier_backend_bindability
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import yaml

#: The base contract's own declaration that a backend is served from the lab.
LOCAL_TIER: Final[str] = "local"

_CONFIGS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "configs"
ROUTING_TIERS_PATH: Final[Path] = _CONFIGS_DIR / "routing_tiers.yaml"
BIFROST_CONTRACT_PATH: Final[Path] = _CONFIGS_DIR / "bifrost_delegation.yaml"

#: ``tier: local`` backends a lane overlay in the fleet actually binds with
#: ``serving: true``. A local backend may be referenced by a tier only if it is
#: named here, because the lane overlay — not this repo — is where a local
#: endpoint becomes concrete, and a rung bound nowhere routes nowhere.
#:
#: Every entry carries the live readback that admitted it. Re-probe before
#: adding one; a backend whose endpoint refuses a connection belongs in
#: ``NOT_SERVING_LOCAL_BACKENDS`` below, not here.
#:
#:   local-coder / local-heavy-reasoning -> .201:8000, the SGLang slot. Probed
#:   2026-09-10: ``GET .201:8000/v1/models`` -> HTTP 200. Bound
#:   ``serving: true`` by the dev, judge and lakshman lane overlays.
LANE_BOUND_LOCAL_BACKENDS: Final[frozenset[str]] = frozenset(
    {"local-coder", "local-heavy-reasoning"}
)

#: ``tier: local`` backends every lane overlay in the fleet currently marks
#: ``serving: false``, so the renderer writes ``endpoint_url: null`` and
#: ``_load_bifrost_endpoints`` skips them. Declared, dark, and therefore not
#: referenceable from a tier. Kept as a NAMED set rather than an absence so the
#: reason and the restore procedure survive with the id.
#:
#:   local-ds-v4-flash -> .200:8101. Probed 2026-09-10:
#:   ``GET .200:8101/v1/models`` -> curl exit 7 "Couldn't
#:   connect to server", http=000. Controls proving the probe ran and the host
#:   is up: ``ping .200`` -> 2/2 packets, 0.0% loss, and the same
#:   probe against .201:8000 -> HTTP 200. All three lab lane overlays (dev,
#:   judge, lakshman) declare ``serving: false`` per OMN-16999 — a stopped
#:   operator-started service on the workstation, not retired hardware.
#:   RESTORE by starting the ds4 server, re-probing, updating omnibase_infra's
#:   ``tests/fixtures/bifrost_served_models_probe.json``, flipping ``serving``
#:   to true in the lane overlays, then moving this id into
#:   ``LANE_BOUND_LOCAL_BACKENDS`` and restoring the tier entry — in that
#:   order, endpoint first and tier reference last.
NOT_SERVING_LOCAL_BACKENDS: Final[frozenset[str]] = frozenset({"local-ds-v4-flash"})


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must have a mapping root, got {type(raw).__name__}")
    return raw


def referenced_backend_ids(tiers_path: Path = ROUTING_TIERS_PATH) -> set[str]:
    """The set of ``backend_id`` values any tier in ``routing_tiers.yaml`` names."""
    data = _load_yaml_mapping(tiers_path)
    tiers = data.get("tiers")
    if not isinstance(tiers, list):
        raise ValueError(f"{tiers_path} must declare a list of tiers")

    referenced: set[str] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        for model in tier.get("models") or ():
            if isinstance(model, dict) and isinstance(model.get("backend_id"), str):
                referenced.add(model["backend_id"])
    return referenced


def declared_backends(
    contract_path: Path = BIFROST_CONTRACT_PATH,
) -> dict[str, dict[str, object]]:
    """``backend_id`` -> declaration, from the committed bifrost contract."""
    data = _load_yaml_mapping(contract_path)
    backends = data.get("backends")
    if not isinstance(backends, list):
        raise ValueError(f"{contract_path} must declare a list of backends")

    return {
        backend["backend_id"]: backend
        for backend in backends
        if isinstance(backend, dict) and isinstance(backend.get("backend_id"), str)
    }


def find_unbindable_tier_backends(
    tiers_path: Path = ROUTING_TIERS_PATH,
    contract_path: Path = BIFROST_CONTRACT_PATH,
) -> dict[str, list[str]]:
    """Classify every tier-referenced backend that no lane can bind.

    Returns a mapping of finding class -> sorted offending ``backend_id`` list.
    An empty mapping means every tier reference is bindable somewhere. The
    classes are kept distinct because the remedy differs: an undeclared id is a
    typo, a null cloud endpoint is a missing contract value, and a dark local
    rung is an endpoint that has to come back before the reference may.
    """
    referenced = referenced_backend_ids(tiers_path)
    declarations = declared_backends(contract_path)

    undeclared: list[str] = []
    cloud_without_endpoint: list[str] = []
    local_not_serving: list[str] = []

    for backend_id in sorted(referenced):
        declaration = declarations.get(backend_id)
        if declaration is None:
            undeclared.append(backend_id)
            continue

        if declaration.get("tier") == LOCAL_TIER:
            if backend_id not in LANE_BOUND_LOCAL_BACKENDS:
                local_not_serving.append(backend_id)
            continue

        endpoint_url = declaration.get("endpoint_url")
        if not (isinstance(endpoint_url, str) and endpoint_url.strip()):
            cloud_without_endpoint.append(backend_id)

    findings: dict[str, list[str]] = {}
    if undeclared:
        findings["undeclared"] = undeclared
    if cloud_without_endpoint:
        findings["cloud_endpoint_url_null"] = cloud_without_endpoint
    if local_not_serving:
        findings["local_bound_by_no_lane"] = local_not_serving
    return findings


_REMEDY: Final[dict[str, str]] = {
    "undeclared": (
        "routing_tiers.yaml names a backend_id that bifrost_delegation.yaml does "
        "not declare at all. Fix the id, or declare the backend."
    ),
    "cloud_endpoint_url_null": (
        "a non-local backend referenced by a tier carries endpoint_url: null. No "
        "lane can bind it — the typed lane overlay admits only unauthenticated "
        "local http:// backends, and the deploy-time renderer never reads "
        "endpoint bindings from the process environment, so endpoint_url_env is "
        "inert on that path. _load_bifrost_endpoints drops it silently, leaving "
        "the tier pointing at nothing. Give the backend a concrete endpoint_url "
        "in the committed contract, or stop referencing it from every tier and "
        "leave the declaration parked."
    ),
    "local_bound_by_no_lane": (
        "a tier: local backend referenced by a tier is bound by no lane overlay "
        "with serving: true, so every lane renders it as endpoint_url: null and "
        "the reducer skips it. Bring the endpoint back and add the id to "
        "LANE_BOUND_LOCAL_BACKENDS, or stop referencing it from every tier and "
        "leave the declaration parked."
    ),
}


def format_findings(findings: dict[str, list[str]]) -> str:
    """Render findings as an operator-readable report."""
    lines = [
        "routing_tiers.yaml references backend(s) no lane can bind (OMN-16833):",
        "",
    ]
    for finding_class, backend_ids in sorted(findings.items()):
        lines.append(f"  [{finding_class}] {', '.join(backend_ids)}")
        lines.append(f"      {_REMEDY[finding_class]}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    findings = find_unbindable_tier_backends()
    if not findings:
        return 0
    sys.stderr.write(format_findings(findings))
    return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
