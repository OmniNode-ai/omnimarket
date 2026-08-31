# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Loader for bifrost_delegation.yaml delegation routing config.

Reads and validates the delegation routing config from disk. Endpoint-bearing
local state is stored in an overlay file and deep-merged over the repo default
at load time.

Related:
    - OMN-10637: Bifrost routing rules for delegation task classes
    - OMN-10717: Default contract + endpoint overlay merge semantics
    - OMN-16903: overlay-only backend_ids are rejected attributably, and the
      sibling ``routing/delegation_backend_resolution.py`` merge path shares
      that rule via ``reject_overlay_only_backend_ids``
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent / "configs" / "bifrost_delegation.yaml"
)
_DEFAULT_OVERLAY_PATH = (
    Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"
)

_IDENTITY_KEYS = ("backend_id", "rule_id")

_COMMITTED_CONTRACT_RELPATH = "src/omnimarket/configs/bifrost_delegation.yaml"


class ProviderSurfaceMismatchError(ValueError):
    """A backend addresses a declared provider host on the wrong path prefix.

    OMN-6790. Distinct from a plain schema ``ValueError`` because the caller
    (and the operator reading the traceback) needs the CAUSE, not the field:
    one provider host can serve two different PRODUCTS on two prefixes, and the
    wrong one answers our key with an error naming a cause that is not real.
    """


class OverlayOnlyBackendIdError(ValueError):
    """A site overlay declared a ``backend_id`` the committed contract does not.

    Raised by BOTH overlay merge paths (this loader and the routing authority's
    ``load_bifrost_backends``) so that one input class produces one outcome
    regardless of which loader a caller reaches for (OMN-16903).

    Subclasses ``ValueError`` to preserve this module's documented failure
    contract for existing callers that catch ``ValueError`` around config load.
    """


def reject_overlay_only_backend_ids(
    committed_backends: Sequence[Any],
    overlay_backends: Sequence[Any],
    *,
    overlay_source: str,
) -> None:
    """Raise if ``overlay_backends`` names a ``backend_id`` not in the contract.

    A site overlay exists to OVERRIDE fields on backends the committed contract
    already declares — typically to supply a COMPLETE site-local ``endpoint_url``
    for an entry whose repo default is null. It is not a registration surface.

    Overlay rows are hand-written and carry only ``backend_id`` / ``endpoint_url``
    / ``model_name`` — no ``tier``, ``timeout_ms`` or ``max_tokens``. Before
    OMN-16903 the two merge paths disagreed about what to do with a row naming a
    backend the contract no longer declares: this loader APPENDED it, so the
    partial entry then failed whole-config schema validation with a pydantic
    message pointing at a list index the operator never wrote, taking every task
    type down; the routing authority silently DROPPED it, quietly narrowing the
    routing table. Both now refuse, naming the offending id and its source.

    Args:
        committed_backends: the ``backends`` entries from the committed contract.
        overlay_backends: the ``backends`` entries from the overlay.
        overlay_source: human-readable provenance of the overlay — a filesystem
            path or a secret-store key. Carried verbatim into the error message
            so a stale overlay is diagnosable without reproducing the load.

    Raises:
        OverlayOnlyBackendIdError: if any overlay entry declares a ``backend_id``
            absent from ``committed_backends``.
    """
    committed_ids = {
        item["backend_id"]
        for item in committed_backends
        if isinstance(item, dict) and "backend_id" in item
    }

    offending: list[str] = []
    for item in overlay_backends:
        if not isinstance(item, dict) or "backend_id" not in item:
            continue
        backend_id = item["backend_id"]
        if backend_id not in committed_ids and backend_id not in offending:
            offending.append(backend_id)

    if not offending:
        return

    msg = (
        f"Bifrost delegation overlay {overlay_source} declares backend_id(s) "
        f"that the committed contract does not: {offending}. A site overlay may "
        "only override entries the committed contract already declares — it "
        "cannot introduce new ones, because hand-written overlay rows carry no "
        "tier/timeout_ms/max_tokens and an introduced entry fails schema "
        "validation for the WHOLE config, taking every task type down. Either "
        f"remove the stale row(s) from {overlay_source}, or declare them in "
        f"{_COMMITTED_CONTRACT_RELPATH} (OMN-16903)."
    )
    raise OverlayOnlyBackendIdError(msg)


def load_bifrost_delegation_config(
    config_path: Path | None = None,
    overlay_path: Path | None = None,
) -> ModelBifrostDelegationConfig:
    """Load and validate the bifrost delegation routing config from disk.

    Args:
        config_path: Path to the YAML config file. Defaults to the
            canonical ``src/omnimarket/configs/bifrost_delegation.yaml``.
        overlay_path: Optional endpoint overlay YAML path. When explicitly
            provided, THAT file is merged (if it exists). When omitted, the
            overlay merged depends on ``config_path``: if ``config_path`` is
            ALSO omitted, the caller has resolved neither binding and this
            function refuses outright (CLAUDE.md rule 8, see below) rather
            than falling back to a packaged default; if ``config_path`` IS
            provided, NO overlay is merged at all (see the seam-divergence
            note below) -- the packaged default overlay path
            (``~/.omninode/delegation/bifrost_overrides.yaml``) is never
            silently substituted when the caller has an explicit contract
            binding.

    Returns:
        A validated ``ModelBifrostDelegationConfig`` instance.

    Raises:
        ValueError: If neither ``config_path`` nor ``overlay_path`` is provided
            (OMN-15628 — no packaged-default fallback when the caller resolved
            neither a contract nor an overlay override; CLAUDE.md rule 8), or if
            the YAML cannot be parsed or fails schema validation.
        FileNotFoundError: If the config file does not exist.
    """
    # OMN-15628: this is the single canonical locus for the "neither bound"
    # refusal — every caller (the routing reducer, the generation consumer)
    # funnels through this loader, so the check lives here once instead of
    # being duplicated (and drifting) at each call site. A caller that has
    # resolved EITHER a contract override OR an overlay override still gets
    # the loader's own packaged default for the other half (a contract-only
    # or overlay-only install remains a valid standalone shape); only the
    # "resolved neither" case is a silent-fallback defect.
    if config_path is None and overlay_path is None:
        msg = (
            "Bifrost delegation config: neither a contract path nor an "
            "overlay path was resolved; refusing to fall back to the "
            "packaged default contract (CLAUDE.md rule 8 — no silent config "
            "fallback, OMN-15628). The caller must resolve "
            "BIFROST_CONTRACT_PATH or BIFROST_OVERLAY_PATH explicitly before "
            "calling this loader."
        )
        raise ValueError(msg)

    resolved = config_path or _DEFAULT_CONFIG_PATH

    # OMN-15628 remediation (seam-divergence finding): when the caller
    # resolved an EXPLICIT contract override but did NOT resolve an explicit
    # overlay override, do NOT fall back to the packaged default overlay path
    # (``~/.omninode/delegation/bifrost_overrides.yaml``) at all -- no overlay
    # is merged in this case. An explicit contract binding — e.g. a deployed
    # pod's ``BIFROST_CONTRACT_PATH`` — must never have its endpoints
    # silently redirected by an incidental local dev-machine overlay file
    # that happens to exist on whichever host process is running. This is
    # the single canonical locus for that rule so every caller (the routing
    # reducer's ``_load_bifrost_endpoints``, the generation consumer's
    # ``_resolve_bifrost_backend``) inherits identical behavior. Previously
    # this exclusion was duplicated only at the routing reducer's call site
    # (a local sentinel-PATH substitution -- itself a defect: a relative
    # sentinel path resolves against the process CWD, so a coincidentally
    # named file there would have made ``overlay.exists()`` true and merged
    # unintended content) while the generation consumer passed
    # ``overlay_path=None`` straight through — the two callers resolved
    # DIFFERENT overlays given the SAME env bindings. Moving the rule here as
    # an explicit ``overlay is None`` branch (no sentinel path involved)
    # closes that divergence for every current and future caller instead of
    # requiring each one to reimplement it.
    overlay: Path | None
    if config_path is not None and overlay_path is None:
        overlay = None
    else:
        overlay = overlay_path or _DEFAULT_OVERLAY_PATH

    if not resolved.exists():
        msg = f"Bifrost delegation config not found at {resolved}"
        raise FileNotFoundError(msg)

    data = _read_yaml_mapping(resolved)

    if overlay is not None and overlay.exists():
        overlay_data = _read_yaml_mapping(overlay)
        # OMN-16903: refuse an overlay-only backend_id BEFORE merging, so the
        # operator gets the offending id + this overlay's path instead of the
        # pydantic ``backends.<index>.tier`` message the appended partial entry
        # used to produce below. The sibling routing-authority merge path
        # (``load_bifrost_backends``) calls this same helper, so both paths
        # now agree on this input class.
        reject_overlay_only_backend_ids(
            data.get("backends") or [],
            overlay_data.get("backends") or [],
            overlay_source=str(overlay),
        )
        data = deep_merge_bifrost_delegation_config(data, overlay_data)

    try:
        config = ModelBifrostDelegationConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"Bifrost delegation config schema validation failed: {exc}"
        raise ValueError(msg) from exc

    declared_backend_ids = {b.backend_id for b in config.backends}

    unknown_defaults = set(config.default_backends) - declared_backend_ids
    if unknown_defaults:
        msg = f"default_backends references undeclared backend(s): {sorted(unknown_defaults)}"
        raise ValueError(msg)

    for rule in config.routing_rules:
        unknown_rule_backends = set(rule.backend_ids) - declared_backend_ids
        if unknown_rule_backends:
            msg = (
                f"Rule {rule.rule_id!s} ({rule.task_class!r}) references "
                f"undeclared backend(s): {sorted(unknown_rule_backends)}"
            )
            raise ValueError(msg)

    _reject_backends_off_a_declared_provider_surface(config, source=str(resolved))

    rule_ids = [rule.rule_id for rule in config.routing_rules]
    if len(rule_ids) != len(set(rule_ids)):
        counts: dict[object, int] = {}
        for rid in rule_ids:
            counts[rid] = counts.get(rid, 0) + 1
        duplicates = [rid for rid, count in counts.items() if count > 1]
        msg = f"Duplicate rule_id(s) detected: {duplicates}"
        raise ValueError(msg)

    logger.info(
        "Loaded bifrost delegation config v%s: %d backends, %d rules",
        config.config_version,
        len(config.backends),
        len(config.routing_rules),
    )
    return config


def _reject_backends_off_a_declared_provider_surface(
    config: ModelBifrostDelegationConfig,
    *,
    source: str,
) -> None:
    """Fail the LOAD when a backend addresses a provider on the wrong surface.

    OMN-6790. ``provider_quota_policy.providers[].required_path_prefix`` is the
    contract's declaration that a given provider host serves the product we
    hold on ONE path prefix. Enforcing it here — at the single locus every
    consumer of the routing authority funnels through — is what makes the fact
    hold on the HOST, not merely in the repo.

    That distinction is the whole point. The two OMN-6790 regression tests are
    source-tree scanners: they read the committed YAML in a checkout. On
    2026-08-31 the checkout was green and the delegation call still went to the
    pay-as-you-go surface, because the client executed an INSTALLED omnimarket
    build predating the fix (``omnibase_infra/.venv``, omnimarket @ 66b7131a3).
    No source-tree test can see that; a load-time assertion in the code that
    ships WITH the contract can, because a stale build carries a stale contract
    and this check travels with it. The same assertion also covers a site
    overlay or a lane ``BIFROST_CONTRACT_PATH`` that repoints the backend.

    Fails closed: a mismatched surface raises rather than being logged, because
    the alternative is a provider error whose text names a cause that is not
    real ("Insufficient balance") and which has now been misread three times.
    """
    policy = config.provider_quota_policy
    if policy is None:
        return

    rules = [p for p in policy.providers if p.required_path_prefix]
    if not rules:
        return

    offenders: list[str] = []
    for rule in rules:
        prefix = cast(str, rule.required_path_prefix)
        for backend in config.backends:
            url = backend.endpoint_url
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.hostname != rule.match_endpoint_host:
                continue
            if parsed.path.startswith(prefix):
                continue
            hint = rule.required_path_prefix_hint or ""
            offenders.append(
                f"backend {backend.backend_id!r} -> {url} "
                f"(provider {rule.provider_id!r} requires path prefix {prefix!r})"
                + (f" — {hint}" if hint else "")
            )

    if offenders:
        msg = (
            "Bifrost delegation config declares backend(s) on the WRONG surface "
            f"of a provider host (source: {source}):\n  "
            + "\n  ".join(offenders)
            + "\nThis config is refused rather than loaded. If the committed "
            "contract is correct, the build or overlay resolving it on THIS host "
            "is stale — reconcile the installed package / overlay, do not edit "
            "the prefix."
        )
        raise ProviderSurfaceMismatchError(msg)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {path}, got {type(data).__name__}"
        raise ValueError(msg)

    return data


def deep_merge_bifrost_delegation_config(
    default_config: dict[str, Any],
    overlay_config: dict[str, Any],
) -> dict[str, Any]:
    """Return ``default_config`` deep-merged with ``overlay_config``.

    This function is pure compute: callers provide already-read YAML mappings,
    and no file system or environment access happens here. Lists of mappings
    keyed by ``backend_id`` or ``rule_id`` merge by identity, preserving default
    ordering and appending overlay-only entries.

    The generic append above is NOT the delegation-config contract for
    ``backends``: ``load_bifrost_delegation_config`` calls
    ``reject_overlay_only_backend_ids`` first, so an overlay-only ``backend_id``
    never reaches this merge (OMN-16903). The append behaviour is retained here
    because this function is also the generic merge for other config shapes
    (e.g. ``adk_invoke.yaml`` via ``adapters/adk/adapter_adk_invoke.py``), which
    declare no ``backends`` at all.
    """
    return cast(dict[str, Any], _deep_merge(default_config, overlay_config))


def _deep_merge(default_value: Any, overlay_value: Any) -> Any:
    if isinstance(default_value, dict) and isinstance(overlay_value, dict):
        merged = copy.deepcopy(default_value)
        for key, value in overlay_value.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    if isinstance(default_value, list) and isinstance(overlay_value, list):
        return _merge_lists(default_value, overlay_value)

    return copy.deepcopy(overlay_value)


def _merge_lists(default_items: list[Any], overlay_items: list[Any]) -> list[Any]:
    identity_key = _list_identity_key(default_items, overlay_items)
    if identity_key is None:
        return copy.deepcopy(overlay_items)

    merged = copy.deepcopy(default_items)
    index_by_id = {
        item[identity_key]: index
        for index, item in enumerate(merged)
        if isinstance(item, dict) and identity_key in item
    }

    for overlay_item in overlay_items:
        if not isinstance(overlay_item, dict) or identity_key not in overlay_item:
            merged.append(copy.deepcopy(overlay_item))
            continue
        item_id = overlay_item[identity_key]
        if item_id in index_by_id:
            existing_index = index_by_id[item_id]
            merged[existing_index] = _deep_merge(merged[existing_index], overlay_item)
        else:
            index_by_id[item_id] = len(merged)
            merged.append(copy.deepcopy(overlay_item))

    return merged


def _list_identity_key(
    default_items: list[Any], overlay_items: list[Any]
) -> str | None:
    mapping_items = [
        item for item in [*default_items, *overlay_items] if isinstance(item, dict)
    ]
    if not mapping_items:
        return None

    for key in _IDENTITY_KEYS:
        if all(key in item for item in mapping_items):
            return key
    return None


__all__: list[str] = [
    "OverlayOnlyBackendIdError",
    "ProviderSurfaceMismatchError",
    "deep_merge_bifrost_delegation_config",
    "load_bifrost_delegation_config",
    "reject_overlay_only_backend_ids",
]
