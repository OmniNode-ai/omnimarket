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
    - OMN-16903 / OMN-17099: an overlay entry naming a backend_id the
      committed contract does not declare ADDS a backend, and is accepted only
      when it validates as a complete declaration; a partial one is refused
      attributably. The sibling ``routing/delegation_backend_resolution.py``
      merge path shares that rule via ``validate_overlay_added_backends``
    - OMN-18670: an overlay write over a field the committed contract already
      declares is recorded and logged with its path, through
      ``build_overlay_field_provenance`` +
      ``warn_overlay_shadowed_authoritative_fields`` — shared by both merge
      paths for the same reason the validator above is
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.model_bifrost_overlay_provenance import (
    AUTHORITATIVE_BACKEND_FIELDS,
    EnumBifrostFieldSource,
    ModelBifrostFieldProvenance,
    ModelBifrostOverlayProvenance,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
    ModelDelegationBackendConfig,
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


class OverlayBackendIncompleteError(ValueError):
    """A site overlay ADDS a backend but the entry is not a complete declaration.

    Raised by BOTH overlay merge paths (this loader and the routing authority's
    ``load_bifrost_backends``) through :func:`validate_overlay_added_backends`,
    so one input class produces one outcome regardless of which loader a caller
    reaches for (the OMN-16903 parity property, kept by OMN-17099).

    Subclasses ``ValueError`` to preserve this module's documented failure
    contract for existing callers that catch ``ValueError`` around config load.

    Attributes:
        overlay_source: the overlay path or secret-store key the entries came
            from, verbatim.
        missing_fields_by_backend: offending ``backend_id`` (or a positional
            placeholder when the entry carries none) mapped to the fields that
            are missing or invalid, in declaration order.
    """

    def __init__(
        self,
        *,
        overlay_source: str,
        missing_fields_by_backend: Mapping[str, tuple[str, ...]],
    ) -> None:
        self.overlay_source = overlay_source
        self.missing_fields_by_backend = dict(missing_fields_by_backend)
        details = "; ".join(
            f"{backend_id!r} missing or invalid: {', '.join(fields)}"
            for backend_id, fields in self.missing_fields_by_backend.items()
        )
        super().__init__(
            f"Bifrost delegation overlay {overlay_source} adds backend(s) the "
            f"committed contract does not declare, but not completely: {details}. "
            "An overlay entry that ADDS a backend must validate on its own as a "
            "complete backend declaration and state every field the routing "
            "authority would otherwise default — "
            f"{', '.join(OVERLAY_ADDED_BACKEND_REQUIRED_FIELDS)} (secret_ref as "
            "a ref string, or null for an explicit none). Complete the entry in "
            f"{overlay_source}, remove it, or declare the backend in "
            f"{_COMMITTED_CONTRACT_RELPATH} (OMN-17099)."
        )


#: Fields an overlay entry must state explicitly to ADD a backend (OMN-17099).
#: Every one is a field the routing authority either cannot route without or
#: would otherwise fill from a model default; an added backend is never
#: defaulted. ``secret_ref`` is required as a KEY — null is an explicit none.
OVERLAY_ADDED_BACKEND_REQUIRED_FIELDS: tuple[str, ...] = (
    "provider",
    "endpoint_url",
    "model_name",
    "tier",
    "timeout_ms",
    "max_tokens",
    "secret_ref",
)

_REQUIRED_NON_BLANK_STRINGS = ("provider", "endpoint_url", "model_name", "tier")

#: A trailing path segment that is only an API version (``/v1``, ``/v1beta``)
#: marks a bare base URL, which OMN-12815 forbids: URLs are posted verbatim.
_BARE_VERSION_SEGMENT = re.compile(r"^v\d+[a-z0-9]*$")


def _endpoint_url_is_complete(url: str) -> bool:
    """Return whether ``url`` is a COMPLETE http(s) endpoint (OMN-12815).

    Complete means an absolute http(s) URL with a host and a path naming the
    operation, not a bare host or a bare API-version base: the URL is posted
    verbatim with no path construction anywhere downstream.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return False
    return not _BARE_VERSION_SEGMENT.match(segments[-1])


def _incomplete_fields(entry: Mapping[str, Any]) -> list[str]:
    """Fields of one overlay-added entry that are missing or invalid."""
    problems: list[str] = []
    for field_name in OVERLAY_ADDED_BACKEND_REQUIRED_FIELDS:
        if field_name not in entry:
            problems.append(field_name)
            continue
        value = entry[field_name]
        if field_name in _REQUIRED_NON_BLANK_STRINGS:
            if not isinstance(value, str) or not value.strip():
                problems.append(field_name)
            elif field_name == "endpoint_url" and not _endpoint_url_is_complete(value):
                problems.append(f"endpoint_url (not a complete http(s) URL: {value!r})")
        elif field_name == "secret_ref":
            if value is not None and (not isinstance(value, str) or not value.strip()):
                problems.append("secret_ref (must be a ref string or null)")
        elif value is None:
            problems.append(field_name)
    if problems:
        return problems
    try:
        ModelDelegationBackendConfig.model_validate(dict(entry))
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<entry>"
            problems.append(f"{location} ({error['msg']})")
    return problems


def validate_overlay_added_backends(
    committed_backends: Sequence[Any],
    overlay_backends: Sequence[Any],
    *,
    overlay_source: str,
) -> list[dict[str, Any]]:
    """Return the overlay entries that ADD a backend, each proven complete.

    OMN-17099 replaces the OMN-16903 blanket refusal of overlay-only
    ``backend_id``s with contract validation. A row naming a backend the
    committed contract declares is an OVERRIDE and merges field-by-field, as
    before. A row naming any other ``backend_id`` ADDS a backend, and is
    accepted only when it validates on its own as a complete
    :class:`ModelDelegationBackendConfig` and explicitly states every field in
    :data:`OVERLAY_ADDED_BACKEND_REQUIRED_FIELDS` — a non-empty ``provider``,
    ``model_name`` and ``tier``, a COMPLETE ``endpoint_url``, ``timeout_ms``,
    ``max_tokens``, and the ``secret_ref`` key (a ref, or null for an explicit
    none).

    The OMN-16903 hazard this keeps closed: a hand-written partial row used to
    be appended and then fail whole-config validation with a pydantic
    list-index message naming nothing the operator wrote. Such a row is still
    refused, now naming the backend, the source and the missing fields.

    Every offending entry is named in one error — including an entry with no
    ``backend_id`` at all — so a stale overlay is fixed in one edit. Nothing is
    silently dropped and nothing is defaulted.

    Args:
        committed_backends: the ``backends`` entries from the committed contract.
        overlay_backends: the ``backends`` entries from the overlay.
        overlay_source: the overlay's path or secret-store key, carried
            verbatim into the error.

    Returns:
        The validated added entries, deep-copied, in overlay order.

    Raises:
        OverlayBackendIncompleteError: if any added entry is missing or invalid.
    """
    committed_ids = {
        item["backend_id"]
        for item in committed_backends
        if isinstance(item, Mapping) and "backend_id" in item
    }

    added: list[dict[str, Any]] = []
    offending: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(overlay_backends):
        if not isinstance(item, Mapping) or not item.get("backend_id"):
            offending[f"<overlay backends entry {index}>"] = ("backend_id",)
            continue
        backend_id = str(item["backend_id"])
        if backend_id in committed_ids:
            continue
        problems = _incomplete_fields(item)
        if problems:
            offending[backend_id] = tuple(problems)
            continue
        added.append(copy.deepcopy(dict(item)))

    if offending:
        raise OverlayBackendIncompleteError(
            overlay_source=overlay_source,
            missing_fields_by_backend=offending,
        )
    return added


def build_overlay_field_provenance(
    committed_backends: Sequence[Any],
    overlay_backends: Sequence[Any],
    *,
    contract_source: str,
    overlay_source: str | None,
) -> ModelBifrostOverlayProvenance:
    """Record, per backend field, WHICH authority supplied the resolved value.

    Pure compute over two already-read YAML backend lists — no filesystem, no
    environment, no logging. It mirrors the field-by-field merge both loaders
    perform, so the record describes the config those loaders actually resolve
    rather than a reconstruction of it.

    A field is attributed to the OVERLAY when the overlay row for that
    ``backend_id`` carries the key at all, and to the COMMITTED CONTRACT
    otherwise. When the overlay carries a key the committed contract had
    already declared with a non-null value, the committed value is recorded as
    ``shadowed_value`` — which is what makes the override nameable downstream.

    The distinction that matters is null-vs-declared, and the committed
    contract draws it deliberately: it leaves ``endpoint_url`` null so a site
    overlay can supply the local endpoint, and it declares ``model_name`` so
    the attribution guard has something to reconcile against. Filling a null is
    the documented bootstrap fallback; writing over a declared value is not
    (``docs/architecture/tutorials/overlays.md`` — on-disk overlays are "a
    bootstrap fallback only — used when nothing higher resolves and only with
    logged provenance").

    Args:
        committed_backends: ``backends`` entries from the committed contract.
        overlay_backends: ``backends`` entries from the active overlay, empty
            when no overlay was merged.
        contract_source: path of the committed contract, carried verbatim into
            messages.
        overlay_source: path or store key of the overlay, or None when no
            overlay was merged at all (the deployed-pod case).

    An overlay row naming a ``backend_id`` the committed contract does NOT
    declare ADDS a backend (OMN-17099). Every one of its fields is attributed
    to the overlay with no shadowed value — there is no committed value to
    shadow — and those records follow the committed ones, in overlay order,
    matching where both merge paths append the added backend.

    Returns:
        A :class:`ModelBifrostOverlayProvenance` covering every field of every
        backend the committed contract declares, in contract order, followed by
        every field of every overlay-added backend, in overlay order.
    """
    overlay_by_id: dict[str, Mapping[str, Any]] = {
        row["backend_id"]: row
        for row in overlay_backends
        if isinstance(row, Mapping) and "backend_id" in row
    }

    records: list[ModelBifrostFieldProvenance] = []
    for committed in committed_backends:
        if not isinstance(committed, Mapping) or "backend_id" not in committed:
            continue
        backend_id = str(committed["backend_id"])
        overlay_row = overlay_by_id.get(backend_id, {})
        for field_name in (*committed.keys(), *overlay_row.keys()):
            if field_name == "backend_id":
                continue
            if any(
                r.backend_id == backend_id and r.field_name == field_name
                for r in records
            ):
                continue
            committed_value = committed.get(field_name)
            if field_name in overlay_row:
                overlay_value = overlay_row[field_name]
                shadowed = (
                    _render(committed_value) if committed_value is not None else None
                )
                records.append(
                    ModelBifrostFieldProvenance(
                        backend_id=backend_id,
                        field_name=str(field_name),
                        value=_render(overlay_value),
                        source=EnumBifrostFieldSource.OVERLAY,
                        # ``overlay_source`` cannot be None here: a row only
                        # reaches ``overlay_by_id`` when an overlay was merged.
                        source_ref=overlay_source or "<overlay>",
                        shadowed_value=shadowed,
                        shadowed_source_ref=(
                            contract_source if shadowed is not None else None
                        ),
                    )
                )
                continue
            records.append(
                ModelBifrostFieldProvenance(
                    backend_id=backend_id,
                    field_name=str(field_name),
                    value=_render(committed_value),
                    source=EnumBifrostFieldSource.COMMITTED_CONTRACT,
                    source_ref=contract_source,
                )
            )

    committed_ids = {
        str(row["backend_id"])
        for row in committed_backends
        if isinstance(row, Mapping) and "backend_id" in row
    }
    for backend_id, overlay_row in overlay_by_id.items():
        if str(backend_id) in committed_ids:
            continue
        for field_name, overlay_value in overlay_row.items():
            if field_name == "backend_id":
                continue
            records.append(
                ModelBifrostFieldProvenance(
                    backend_id=str(backend_id),
                    field_name=str(field_name),
                    value=_render(overlay_value),
                    source=EnumBifrostFieldSource.OVERLAY,
                    source_ref=overlay_source or "<overlay>",
                )
            )

    return ModelBifrostOverlayProvenance(
        contract_source=contract_source,
        overlay_source=overlay_source,
        fields=tuple(records),
    )


def warn_overlay_shadowed_authoritative_fields(
    provenance: ModelBifrostOverlayProvenance,
) -> None:
    """Log one WARN per overlay write over a committed authoritative field.

    Called by BOTH merge paths on EVERY load — deliberately not deduplicated
    and not once-per-process. A warning that fires only on the first load of a
    long-lived runtime is indistinguishable from no warning at all to the
    operator who starts reading the log after boot, and "indistinguishable from
    no warning" is the exact defect being closed (OMN-18670).

    This warns rather than refuses. The override is outside the overlay's
    documented bootstrap-fallback role, but the delegation config ADR
    (``docs/adr/2026-06-18-delegation-config-authority-and-budget-aware-tier-cost.md``
    D1) keeps committed values as "defaults, overridable per tenant, never the
    tenant's effective truth", so refusing the merge would break a legitimate
    per-deployment override and take every task type down on a host whose only
    sin is a stale line. What doctrine forbids is the override being SILENT
    (``docs/architecture/tutorials/overlays.md`` — "a bootstrap fallback is
    allowed ... but it must be logged"), and the fail-closed attribution guard
    at the call boundary (OMN-16419) remains the surface that actually stops
    the call.
    """
    for shadow in provenance.shadows():
        logger.warning(
            "bifrost_overlay_shadows_authoritative_field: %s. This overlay is a "
            "bootstrap fallback and the committed contract owns %s — remove the "
            "key from the overlay rather than repointing it, or the next "
            "contract repoint will be silently reverted on this host "
            "(OMN-18670; retire the overlay precedence path: OMN-17989).",
            shadow.describe(),
            shadow.field_name,
        )


def _render(value: Any) -> str | None:
    """Render a YAML scalar for a provenance record; None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


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
        # OMN-17099 (was OMN-16903's blanket refusal): an overlay entry that
        # ADDS a backend is validated as a complete declaration BEFORE merging,
        # so a partial one names its id, this overlay's path and the missing
        # fields instead of the pydantic ``backends.<index>.tier`` message an
        # appended partial entry used to produce below. The sibling
        # routing-authority merge path (``load_bifrost_backends``) calls this
        # same helper, so both paths agree on this input class.
        validate_overlay_added_backends(
            data.get("backends") or [],
            overlay_data.get("backends") or [],
            overlay_source=str(overlay),
        )
        # OMN-18670: record and announce an overlay write over a field the
        # committed contract already declares, BEFORE the merge erases the
        # distinction. After ``deep_merge_bifrost_delegation_config`` the two
        # values are one value and nothing downstream can tell them apart —
        # which is why the 2026-09-18 refusal could name the literal but not
        # the file that supplied it.
        warn_overlay_shadowed_authoritative_fields(
            build_overlay_field_provenance(
                data.get("backends") or [],
                overlay_data.get("backends") or [],
                contract_source=str(resolved),
                overlay_source=str(overlay),
            )
        )
        data = deep_merge_bifrost_delegation_config(data, overlay_data)

    return _validate_bifrost_delegation_config(data, source=str(resolved))


def reject_backends_off_a_declared_provider_surface(
    backends: Sequence[Mapping[str, Any]],
    provider_rules: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> None:
    """Refuse a RESOLVED backend set that addresses a provider on the wrong surface.

    OMN-6790 / OMN-17314. ``provider_quota_policy.providers[].required_path_prefix``
    is the contract's declaration that a given provider host serves the product
    we hold on ONE path prefix. This is the shared rejector for that rule —
    same module, same shape and same attributable-message contract as
    :func:`validate_overlay_added_backends`, and for the same reason: one input
    class must produce one outcome no matter which loader a caller reached for.

    It operates on the MERGED/RESOLVED backends, not on committed bytes. That
    distinction is the whole point. The two OMN-6790 regression tests are
    source-tree scanners, and on 2026-08-31 both were green on ``origin/dev``
    while this workstation still POSTed GLM to the pay-as-you-go surface: the
    canonical clone sat detached at a pre-fix commit, ``BIFROST_CONTRACT_PATH``
    pointed at that clone's working tree, and the venv was installed from the
    same clone HEAD (omnimarket @ 66b7131a3 — pyproject version "0.4.11", NOT
    release tag v0.4.11, which was cut later and does carry the fix). Nothing
    that reads a checkout can see any of that.

    Fails closed: a mismatched surface raises rather than being logged, because
    the alternative is a provider error whose own text names a cause that is not
    real ("Insufficient balance. Please recharge") and which has now been
    misread three times (OMN-14625, OMN-16891, OMN-17193).

    Args:
        backends: the resolved backend mappings (post-overlay-merge).
        provider_rules: ``provider_quota_policy.providers`` entries as mappings.
        source: what produced this backend set — a config path, an overlay path,
            or a store key. Required and keyword-only so no call site can refuse
            (or admit) a config it cannot attribute.

    Raises:
        ProviderSurfaceMismatchError: naming every offending ``backend_id``, its
            URL, the required prefix, the declared hint, and ``source``.
    """
    rules = [r for r in provider_rules if r.get("required_path_prefix")]
    if not rules:
        return

    offenders: list[str] = []
    for rule in rules:
        prefix = cast(str, rule["required_path_prefix"])
        host = rule.get("match_endpoint_host")
        hint = rule.get("required_path_prefix_hint") or ""
        for backend in backends:
            url = backend.get("endpoint_url")
            if not isinstance(url, str) or not url:
                continue
            parsed = urlparse(url)
            if parsed.hostname != host:
                continue
            if parsed.path.startswith(prefix):
                continue
            offenders.append(
                f"backend {backend.get('backend_id')!r} -> {url} "
                f"(provider {rule.get('provider_id')!r} requires path prefix "
                f"{prefix!r})" + (f" — {hint}" if hint else "")
            )

    if offenders:
        msg = (
            "Delegation routing authority resolves backend(s) onto the WRONG "
            f"surface of a provider host (source: {source}):\n  "
            + "\n  ".join(offenders)
            + "\nThis config is refused rather than resolved. If the committed "
            "contract is correct, then the overlay, the BIFROST_CONTRACT_PATH "
            "tree, or the installed build resolving it on THIS host is stale — "
            "reconcile that, do not edit the prefix."
        )
        raise ProviderSurfaceMismatchError(msg)


def _reject_backends_off_a_declared_provider_surface(
    config: ModelBifrostDelegationConfig,
    *,
    source: str,
) -> None:
    """Typed-config adapter over :func:`reject_backends_off_a_declared_provider_surface`."""
    policy = config.provider_quota_policy
    if policy is None:
        return
    reject_backends_off_a_declared_provider_surface(
        [b.model_dump(mode="python") for b in config.backends],
        [p.model_dump(mode="python") for p in policy.providers],
        source=source,
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {path}, got {type(data).__name__}"
        raise ValueError(msg)

    return data


def _read_yaml_mapping_bytes(payload: bytes, *, source: str) -> dict[str, Any]:
    """Parse an explicit in-memory YAML mapping for offline contract checks."""
    try:
        raw = payload.decode("utf-8")
        data = yaml.safe_load(raw)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        msg = f"Expected UTF-8 YAML mapping from {source}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {source}, got {type(data).__name__}"
        raise ValueError(msg)
    return data


def load_bifrost_delegation_config_payload(
    contract_payload: bytes,
    overlay_payload: bytes | None,
    *,
    contract_source: str,
    overlay_source: str | None = None,
) -> ModelBifrostDelegationConfig:
    """Validate explicit Bifrost contract bytes with the canonical merge rules.

    This is the in-memory counterpart to :func:`load_bifrost_delegation_config`.
    It deliberately accepts neither paths nor environment-derived defaults, so
    offline validators can replay a supplied contract/overlay pair without
    resolving a deployment binding.
    """
    base = _read_yaml_mapping_bytes(contract_payload, source=contract_source)
    if overlay_payload is not None:
        if overlay_source is None:
            raise ValueError(
                "overlay_source is required when overlay_payload is supplied"
            )
        overlay = _read_yaml_mapping_bytes(overlay_payload, source=overlay_source)
        validate_overlay_added_backends(
            base.get("backends") or [],
            overlay.get("backends") or [],
            overlay_source=overlay_source,
        )
        warn_overlay_shadowed_authoritative_fields(
            build_overlay_field_provenance(
                base.get("backends") or [],
                overlay.get("backends") or [],
                contract_source=contract_source,
                overlay_source=overlay_source,
            )
        )
        base = deep_merge_bifrost_delegation_config(base, overlay)
    return _validate_bifrost_delegation_config(base, source=contract_source)


def _validate_bifrost_delegation_config(
    data: dict[str, Any], *, source: str
) -> ModelBifrostDelegationConfig:
    """Apply the canonical schema and cross-reference checks to resolved data."""
    try:
        config = ModelBifrostDelegationConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"Bifrost delegation config schema validation failed: {exc}"
        raise ValueError(msg) from exc

    declared_backend_ids = {backend.backend_id for backend in config.backends}
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
    _reject_backends_off_a_declared_provider_surface(config, source=source)
    rule_ids = [rule.rule_id for rule in config.routing_rules]
    if len(rule_ids) != len(set(rule_ids)):
        counts: dict[object, int] = {}
        for rule_id in rule_ids:
            counts[rule_id] = counts.get(rule_id, 0) + 1
        duplicates = [rule_id for rule_id, count in counts.items() if count > 1]
        msg = f"Duplicate rule_id(s) detected: {duplicates}"
        raise ValueError(msg)
    logger.info(
        "Loaded Bifrost delegation config v%s: %d backends, %d rules",
        config.config_version,
        len(config.backends),
        len(config.routing_rules),
    )
    return config


def deep_merge_bifrost_delegation_config(
    default_config: dict[str, Any],
    overlay_config: dict[str, Any],
) -> dict[str, Any]:
    """Return ``default_config`` deep-merged with ``overlay_config``.

    This function is pure compute: callers provide already-read YAML mappings,
    and no file system or environment access happens here. Lists of mappings
    keyed by ``backend_id`` or ``rule_id`` merge by identity, preserving default
    ordering and appending overlay-only entries.

    For ``backends`` the append IS the delegation-config contract for an
    overlay entry that adds a backend, but only once proven complete: both
    loaders call ``validate_overlay_added_backends`` first, so a partial
    overlay-only entry never reaches this merge (OMN-16903 / OMN-17099). The
    same append is also the generic merge for other config shapes (e.g.
    ``adk_invoke.yaml`` via ``adapters/adk/adapter_adk_invoke.py``), which
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
    "AUTHORITATIVE_BACKEND_FIELDS",
    "OVERLAY_ADDED_BACKEND_REQUIRED_FIELDS",
    "OverlayBackendIncompleteError",
    "ProviderSurfaceMismatchError",
    "build_overlay_field_provenance",
    "deep_merge_bifrost_delegation_config",
    "load_bifrost_delegation_config",
    "load_bifrost_delegation_config_payload",
    "reject_backends_off_a_declared_provider_surface",
    "validate_overlay_added_backends",
    "warn_overlay_shadowed_authoritative_fields",
]
