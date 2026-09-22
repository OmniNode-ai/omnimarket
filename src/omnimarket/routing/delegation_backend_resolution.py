# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical routing-authority resolve step for delegation backend selection.

``HandlerLlmDelegationCall`` REQUIRES a resolved ``model_id`` + ``endpoint_ref``
as inputs — it executes exactly one LLM call and never resolves backend authority
itself. This module is the routing-authority home that resolves those two values
from the bifrost delegation contract (``bifrost_delegation.yaml`` + the installer
overlay) BEFORE the effect handler is invoked.

It replaces the hand-rolled ``_load_bifrost_config`` / ``_select_backend`` that
previously lived inside ``port_direct_curl_dispatch`` (OMN-13160). No config
loading lives in a port anymore; the orchestrator calls this resolve step and
hands the result to the effect handler.

OMN-12815 / OMN-13159: every resolved ``endpoint_url`` is the COMPLETE final URL
(incl. the full ``/v1/chat/completions`` path). It is carried verbatim into the
effect handler's ``endpoint_ref`` and posted with no construction. A bare base or
non-http(s) value fails closed downstream — this resolver does not construct
paths.

OMN-13232 (ADR D2, plan A3): the store overlay replaces the local-file overlay as
the primary authority for site-specific endpoint URLs. Pass a ``ProtocolSecretStore``
to ``load_bifrost_backends`` / ``resolve_delegation_backend``; the store key
``BIFROST_OVERLAY_STORE_KEY`` holds the overlay YAML blob. When the store has no
entry for that key the legacy file overlay (``~/.omninode/delegation/
bifrost_overrides.yaml``) is used as a DEV-ONLY fallback with a deprecation
warning. New deployments should populate the store key; file overlay support will
be removed in a future release.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from queue import Queue
from typing import Any, Final

import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    build_overlay_field_provenance,
    reject_backends_off_a_declared_provider_surface,
    reject_overlay_only_backend_ids,
    warn_overlay_shadowed_authoritative_fields,
)
from omnimarket.inference.delegation_config_provenance import (
    resolve_bifrost_path_binding,
)
from omnimarket.models.delegation.model_bifrost_overlay_provenance import (
    ModelBifrostOverlayProvenance,
)

logger = logging.getLogger(__name__)

_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
_BIFROST_CONFIG_PATH = _CONFIGS_DIR / "bifrost_delegation.yaml"

# DEV-ONLY fallback: the local file overlay path.  New deployments must use the
# store overlay (see BIFROST_OVERLAY_STORE_KEY). This path is retained only for
# standalone installs and local dev; it will be removed once all lanes supply the
# store overlay (OMN-13232).
_OVERLAY_PATH = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"

#: Store key under which the bifrost overlay YAML blob is stored (OMN-13232).
#:
#: The value is a YAML-encoded dict with a ``backends`` list, identical in
#: structure to ``bifrost_overrides.yaml``.  Each entry is merged field-by-field
#: onto the matching ``backend_id`` entry from the committed contract::
#:
#:     backends:
#:       - backend_id: local-coder
#:         endpoint_url: "https://lane-a.example:8000/v1/chat/completions"
#:         model_name: "Qwen3.6-35B-A3B"
#:
#: Endpoint URLs stored here MUST be COMPLETE (incl. the full chat path);
#: bare-base values fail closed at the resolution boundary (OMN-12815).
BIFROST_OVERLAY_STORE_KEY: Final[str] = "delegation.bifrost.overlay"


def _resolve_effective_bifrost_paths(
    config_path: Path | None,
    overlay_path: Path | None,
) -> tuple[Path, Path | None]:
    """Resolve the contract + file-overlay paths this load will actually read.

    OMN-18676. This is where the routing authority — the loader the LOCAL
    DISPATCH path reaches through ``resolve_delegation_backend`` — joins the
    single binding seam :func:`resolve_bifrost_path_binding`, which the routing
    reducer and the generation consumer already resolve their bindings through.
    Before this, the two ``Path`` defaults below were captured in
    ``__kwdefaults__`` at ``def`` time and ``BIFROST_OVERLAY_PATH`` was consulted
    on this path nowhere at all, so identical bindings selected DIFFERENT
    overlays on the two paths and a deployment binding its overlay outside
    ``$HOME`` had that binding silently ignored here.

    Precedence, highest first:

    1. an explicit argument — a caller that names a path means that path;
    2. the environment binding for that half, through the shared seam;
    3. the packaged/machine-local default, read from the module attribute at
       CALL time (not captured at ``def`` time) so it is the same default the
       rest of the module names.

    The returned overlay is ``None`` when a contract was resolved from (1) or
    (2) while the overlay was resolved from neither. That is the OMN-15628 rule
    the canonical loader ``load_bifrost_delegation_config`` already applies to
    the reducer's call site, mirrored here rather than restated: an explicit
    contract binding must never have its endpoints redirected by whatever
    overlay happens to sit in the home directory of whichever process is
    running. With NEITHER key bound and no arguments, both defaults apply
    exactly as they did before — the standalone-install shape is unchanged.
    """
    if config_path is not None and overlay_path is not None:
        return config_path, overlay_path

    binding = resolve_bifrost_path_binding()
    resolved_config = config_path if config_path is not None else binding.contract_path
    resolved_overlay = (
        overlay_path if overlay_path is not None else binding.overlay_path
    )

    if resolved_overlay is None and resolved_config is not None:
        # Contract named, overlay not: merge no file overlay at all.
        return resolved_config, None
    if resolved_overlay is None:
        # Neither named: the pre-existing standalone-install pair.
        return _BIFROST_CONFIG_PATH, _OVERLAY_PATH
    return (resolved_config or _BIFROST_CONFIG_PATH), resolved_overlay


class ModelResolvedDelegationBackend(BaseModel):
    """Routing-authority resolution of one delegation backend for a task type.

    The two load-bearing fields the effect handler requires are ``model_id`` and
    ``endpoint_ref`` (the COMPLETE endpoint URL, carried verbatim). ``backend_id``
    and ``tier`` are provenance carried for telemetry and inference-protocol
    shaping; ``provider_request_options`` and ``extra_headers`` are the outbound
    request-shaping carried from the inference protocol config + backend config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    endpoint_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "COMPLETE resolved chat-completions URL, posted verbatim by the "
            "effect handler (OMN-12815/OMN-13159)."
        ),
    )
    tier: str = Field(default="unknown")
    max_tokens: int = Field(
        ...,
        ge=1,
        description=(
            "Per-backend output-token budget/ceiling resolved from the routing "
            "contract (overlay-overridable). The orchestrator uses this as the "
            "effective max_tokens when the request omits one and as the hard cap "
            "when the request supplies an explicit value (OMN-13161)."
        ),
    )
    timeout_ms: int = Field(
        ...,
        ge=1,
        description=(
            "Per-backend HTTP request timeout in milliseconds resolved from the "
            "routing contract (overlay-overridable). The orchestrator threads this "
            "(÷1000) into the effect handler's transport so large generations are "
            "not capped by a hardcoded transport default (OMN-13170)."
        ),
    )
    extra_headers: dict[str, str] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        default=None,
        description=(
            "Logical secret reference (e.g. ``llm.glm.api_key``) the effect "
            "boundary resolves to the literal API key via ProtocolSecretStore "
            "(OMN-12824). Only the reference name is carried here; the value is "
            "never resolved in the routing authority."
        ),
    )
    supports_response_format_json_schema: bool = Field(
        default=False,
        description=(
            "OMN-18989: whether the resolved backend honours OpenAI-style "
            "``response_format: {'type': 'json_schema', ...}``. Carried onto "
            "the RESOLVED backend, not read from the binding at the call "
            "site, so the capability travels with the backend the request is "
            "actually going to -- an escalation that changes rung changes "
            "this with it. False means NOT DECLARED and no directive is sent."
        ),
    )
    api_key_env: str | None = Field(
        default=None,
        description=(
            "OMN-13943: the backend's own contract-declared literal env-var "
            "NAME (e.g. ``GEMINI_API_KEY``), distinct from ``secret_ref``'s "
            "dotted convention. Resolved as an ADDITIONAL fallback at the "
            "effect boundary when the ``secret_ref`` convention mapping misses "
            "— never a code-hardcoded alias, always sourced from the bifrost "
            "backend config."
        ),
    )
    model_id_source: str = Field(
        default="",
        description=(
            "OMN-18670: one line naming WHICH artifact supplied ``model_id`` "
            "and at which key — the committed contract, or an overlay (file "
            "path or store key) together with the committed value it wrote "
            "over. Threaded to the effect boundary so the fail-closed "
            "``model_attribution_mismatch`` refusal names its source instead "
            "of only the literal. Empty when the caller supplied a pre-merged "
            "backend list, in which case there is no merge to attribute — an "
            "honest blank, never a fabricated path."
        ),
    )


async def _load_store_overlay_async(
    store: ProtocolSecretStore,
) -> list[dict[str, Any]] | None:
    """Read the bifrost overlay from the store as a YAML blob (async).

    Returns ``None`` when the store has no entry for ``BIFROST_OVERLAY_STORE_KEY``
    (the key is absent or resolves to an empty string). Returns the parsed
    ``backends`` list otherwise.

    The YAML structure mirrors ``bifrost_overrides.yaml``::

        backends:
          - backend_id: local-coder
            endpoint_url: "<store-provided chat-completions URL>"

    Endpoint URLs in the store MUST be COMPLETE — no path construction is
    performed here; a bare-base entry is carried verbatim and will fail closed
    at the ``resolve_delegation_backend`` boundary (OMN-12815).
    """
    raw = await store.get_secret(BIFROST_OVERLAY_STORE_KEY)
    if not raw or not raw.strip():
        return None
    parsed = yaml.safe_load(raw) or {}
    return list(parsed.get("backends", []))


def _load_store_overlay(store: ProtocolSecretStore) -> list[dict[str, Any]] | None:
    """Synchronous wrapper for :func:`_load_store_overlay_async`.

    When called from inside a running event loop the resolution is offloaded to
    a daemon thread (the same pattern ``secret_store_resolver`` uses for
    ``api_key_ref_available``). Otherwise ``asyncio.run`` is used directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True

    if not running_loop:
        return asyncio.run(_load_store_overlay_async(store))

    # Called from inside an event loop — offload to a thread so we can drive the
    # coroutine from a fresh event loop without nesting.
    result_q: Queue[tuple[list[dict[str, Any]] | None, BaseException | None]] = Queue(
        maxsize=1
    )

    def _runner() -> None:
        try:
            value = asyncio.run(_load_store_overlay_async(store))
        except BaseException as exc:
            result_q.put((None, exc))
        else:
            result_q.put((value, None))

    t = threading.Thread(
        target=_runner,
        name="omnimarket-bifrost-overlay-store-read",
        daemon=True,
    )
    t.start()
    t.join()
    value, exc = result_q.get()
    if exc is not None:
        raise exc
    return value


def _merge_overlay(
    backends: list[dict[str, Any]],
    overlay_backends: list[dict[str, Any]],
    *,
    overlay_source: str,
    provider_rules: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Merge overlay entries field-by-field onto matching ``backend_id`` entries.

    OMN-16903: an overlay entry naming a ``backend_id`` the committed contract
    does not declare is REJECTED here, naming the id and ``overlay_source``.
    This function used to drop such an entry silently (it only ever iterates the
    committed list), while the sibling merge path in
    ``adapters/llm/bifrost/config_loader_bifrost_delegation.py`` appended it and
    hard-failed whole-config validation. Both now share one rule via
    ``reject_overlay_only_backend_ids``. ``overlay_source`` is keyword-only and
    required so no call site can merge an overlay it cannot attribute.
    """
    reject_overlay_only_backend_ids(
        backends, overlay_backends, overlay_source=overlay_source
    )
    overlay_by_id = {b["backend_id"]: b for b in overlay_backends}
    merged = list(backends)
    for i, backend in enumerate(merged):
        override = overlay_by_id.get(backend["backend_id"])
        if override is not None:
            merged[i] = {**backend, **override}
    # OMN-17314: the overlay merge is field-by-field, so an overlay row carrying
    # only ``{backend_id, endpoint_url}`` silently REPLACES a contract-declared
    # endpoint. reject_overlay_only_backend_ids above rejects an unknown id;
    # nothing inspected the overridden VALUE. Enforce the contract's declared
    # provider surface on the MERGED result, through the same shared rejector
    # the sibling loader calls, so one input class produces one outcome
    # regardless of which merge path a caller reached for (the OMN-16903
    # pattern).
    reject_backends_off_a_declared_provider_surface(
        merged, provider_rules, source=overlay_source
    )
    return merged


def load_bifrost_backends_with_provenance(
    *,
    config_path: Path | None = None,
    overlay_path: Path | None = None,
    store: ProtocolSecretStore | None = None,
) -> tuple[list[dict[str, Any]], ModelBifrostOverlayProvenance]:
    """Merge the contract and overlay, and return the per-field provenance too.

    Routes the merge itself through :func:`load_bifrost_backends` rather than
    reimplementing it, so the seam a dozen existing tests monkeypatch keeps
    intercepting. When it IS patched the sink stays empty and this returns an
    empty provenance record — the honest outcome, because a fabricated backend
    list has no file merge to attribute and inventing one here would let a
    refusal name a path that supplied nothing.
    """
    sink: list[ModelBifrostOverlayProvenance] = []
    merged = load_bifrost_backends(
        config_path=config_path,
        overlay_path=overlay_path,
        store=store,
        provenance_sink=sink,
    )
    if sink:
        return merged, sink[0]
    # The seam was patched out, so there is no file merge to attribute. Name the
    # contract this call WOULD have read rather than a raw ``None`` (OMN-18676).
    resolved_config_path, _ = _resolve_effective_bifrost_paths(
        config_path, overlay_path
    )
    return merged, ModelBifrostOverlayProvenance(
        contract_source=str(resolved_config_path)
    )


def load_bifrost_backends(
    *,
    config_path: Path | None = None,
    overlay_path: Path | None = None,
    store: ProtocolSecretStore | None = None,
    provenance_sink: list[ModelBifrostOverlayProvenance] | None = None,
) -> list[dict[str, Any]]:
    """Load and merge bifrost_delegation.yaml with the active overlay.

    ``provenance_sink`` is an optional out-parameter: when a list is passed,
    the per-field provenance record for this merge is appended to it
    (OMN-18670). It is spelled as a sink rather than a second return value
    deliberately — this function is the seam a dozen test modules already
    monkeypatch with a plain ``lambda **_: [...]``, and changing its return
    shape would break every one of them. A patched seam simply leaves the sink
    empty, which is the correct answer: there was no file merge to attribute.

    **Primary authority (OMN-13232):** when a ``ProtocolSecretStore`` is
    provided, the overlay is read from the store under ``BIFROST_OVERLAY_STORE_KEY``
    (a YAML blob). This is the production path. If the store has no entry for
    that key the file overlay is used as a DEV-ONLY fallback (see below).

    **Dev-only fallback:** when no store is provided, or when the store has no
    entry for ``BIFROST_OVERLAY_STORE_KEY``, the local file overlay is consulted.
    A deprecation warning is logged whenever the file fallback is used so that
    drift from store-backed config is visible in the runtime logs.

    **Which files (OMN-18676):** ``config_path`` and ``overlay_path`` default to
    ``None``, meaning "resolve from the binding" —
    :func:`_resolve_effective_bifrost_paths` consults the same
    ``BIFROST_CONTRACT_PATH`` / ``BIFROST_OVERLAY_PATH`` seam the routing
    reducer and the generation consumer resolve theirs through. An explicit
    argument still wins outright. With neither key bound and no arguments the
    pair is the packaged contract plus
    ``~/.omninode/delegation/bifrost_overrides.yaml``, exactly as before. These
    were ``Path`` defaults captured at ``def`` time until OMN-18676, which is
    why the local dispatch path ignored the overlay binding entirely.

    Overlay entries are merged onto matching ``backend_id`` entries field-by-field.
    The overlay supplies COMPLETE endpoint URLs for site-specific local backends
    that are ``null`` in the committed repo default (OMN-12815).

    Returns:
        The merged backend list, and a :class:`ModelBifrostOverlayProvenance`
        naming which authority supplied each field (OMN-18670). The provenance
        is produced from the pre-merge inputs, so it cannot disagree with the
        merge it describes; any overlay write over a field the committed
        contract already declared is also logged at WARN here, on every load.

    Raises:
        OverlayOnlyBackendIdError: if the active overlay (store or file) declares
            a ``backend_id`` the committed contract does not. Previously such an
            entry was dropped silently here while the sibling loader appended it
            and hard-failed the whole config; both paths now refuse identically,
            naming the offending id and the overlay source (OMN-16903).
    """
    # OMN-18676: the SINGLE binding seam. Explicit arguments win; otherwise the
    # environment's BIFROST_CONTRACT_PATH / BIFROST_OVERLAY_PATH bindings decide,
    # resolved through the same surface the routing reducer and the generation
    # consumer resolve theirs through. ``overlay_path`` comes back ``None`` when
    # no file overlay is to be merged at all.
    config_path, overlay_path = _resolve_effective_bifrost_paths(
        config_path, overlay_path
    )

    backends: list[dict[str, Any]] = []
    provider_rules: list[Mapping[str, Any]] = []
    committed_backends: list[dict[str, Any]] = []
    if config_path.is_file():
        base = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        backends = list(base.get("backends", []))
        # OMN-18670: keep the PRE-merge committed rows. After the field-by-field
        # merge below the two authorities are one dict and nothing can tell them
        # apart, which is exactly why the 2026-09-18 refusal could name the
        # stale literal but not the file that supplied it.
        committed_backends = list(backends)
        # OMN-17314: the declared surface policy travels with the contract that
        # declared the backends, so an overlay cannot both repoint a backend and
        # delete the rule that would refuse the repoint.
        provider_rules = list(
            (base.get("provider_quota_policy") or {}).get("providers") or []
        )
        reject_backends_off_a_declared_provider_surface(
            backends, provider_rules, source=str(config_path)
        )

    # --- Primary authority: store overlay (OMN-13232 / ADR D2) -----------------
    if store is not None:
        store_overlay = _load_store_overlay(store)
        if store_overlay is not None:
            store_source = f"store key {BIFROST_OVERLAY_STORE_KEY!r}"
            backends = _merge_overlay(
                backends,
                store_overlay,
                overlay_source=store_source,
                provider_rules=provider_rules,
            )
            _record_overlay_provenance(
                committed_backends,
                store_overlay,
                contract_source=str(config_path),
                overlay_source=store_source,
                sink=provenance_sink,
            )
            return backends
        # Store is configured but has no overlay key → fall through to file with
        # a deprecation warning.
        logger.warning(
            "delegation_backend_resolution: store is configured but "
            "BIFROST_OVERLAY_STORE_KEY=%r is absent; falling back to "
            "DEV-ONLY file overlay at %s. "
            "Populate the store key to silence this warning (OMN-13232).",
            BIFROST_OVERLAY_STORE_KEY,
            overlay_path if overlay_path is not None else "<no file overlay>",
        )
    else:
        # No store provided: log deprecation for the file overlay path.
        if overlay_path is not None and overlay_path.is_file():
            logger.warning(
                "delegation_backend_resolution: using DEV-ONLY file overlay at %s "
                "(bifrost_overrides.yaml). "
                "Pass a ProtocolSecretStore and populate %r in the store for "
                "production deployments (OMN-13232).",
                overlay_path,
                BIFROST_OVERLAY_STORE_KEY,
            )

    # --- Dev-only file fallback (deprecated) ------------------------------------
    if overlay_path is not None and overlay_path.is_file():
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        file_overlay_backends = list(overlay.get("backends", []))
        backends = _merge_overlay(
            backends,
            file_overlay_backends,
            overlay_source=str(overlay_path),
            provider_rules=provider_rules,
        )
        _record_overlay_provenance(
            committed_backends,
            file_overlay_backends,
            contract_source=str(config_path),
            overlay_source=str(overlay_path),
            sink=provenance_sink,
        )
        return backends

    _record_overlay_provenance(
        committed_backends,
        [],
        contract_source=str(config_path),
        overlay_source=None,
        sink=provenance_sink,
    )
    return backends


def _record_overlay_provenance(
    committed_backends: list[dict[str, Any]],
    overlay_backends: list[dict[str, Any]],
    *,
    contract_source: str,
    overlay_source: str | None,
    sink: list[ModelBifrostOverlayProvenance] | None,
) -> None:
    """Build the per-field provenance and announce any authoritative shadow.

    Shared by all three exit paths of :func:`load_bifrost_backends` so a store
    overlay, a file overlay and no overlay at all each produce a record and a
    warning of the same shape — a surface that only describes one of the three
    is the kind of partial instrument that made the file overlay invisible in
    the first place. Delegates the rule itself to the canonical helpers in the
    sibling loader module, so one input class produces one outcome no matter
    which loader a caller reached for (the OMN-16903 precedent).
    """
    provenance = build_overlay_field_provenance(
        committed_backends,
        overlay_backends,
        contract_source=contract_source,
        overlay_source=overlay_source,
    )
    # The WARN fires on EVERY load, whether or not anybody asked for the
    # record — that is AC2, and it is why this is not gated on ``sink``.
    warn_overlay_shadowed_authoritative_fields(provenance)
    if sink is not None:
        sink.append(provenance)


def _select_backend(
    backends: list[dict[str, Any]], task_type: str
) -> dict[str, Any] | None:
    """Select the best backend for ``task_type`` from the merged config.

    Prefers a backend whose ``capabilities`` or ``use_for`` lists the task type
    and that has a populated ``endpoint_url``; otherwise falls back to the first
    backend with any populated ``endpoint_url``.
    """
    for backend in backends:
        if not backend.get("endpoint_url"):
            continue
        capabilities = backend.get("capabilities", [])
        use_for = backend.get("use_for", [])
        if task_type in capabilities or task_type in use_for:
            return backend
    for backend in backends:
        if backend.get("endpoint_url"):
            return backend
    return None


def _select_backend_by_id(
    backends: list[dict[str, Any]], backend_id: str
) -> dict[str, Any] | None:
    """Select the backend with ``backend_id`` that has a populated endpoint_url."""
    for backend in backends:
        if backend.get("backend_id") == backend_id and backend.get("endpoint_url"):
            return backend
    return None


def resolve_delegation_backend(
    task_type: str,
    *,
    backend_id: str | None = None,
    backends: list[dict[str, Any]] | None = None,
    config_path: Path | None = None,
    overlay_path: Path | None = None,
    store: ProtocolSecretStore | None = None,
) -> ModelResolvedDelegationBackend:
    """Resolve ``model_id`` + ``endpoint_ref`` for ``task_type`` from bifrost.

    When ``backend_id`` is supplied the resolver targets that exact backend (it
    must carry a populated COMPLETE ``endpoint_url``) instead of selecting by
    ``task_type`` capability. This is the path the LLM-judge uses to pin a
    concrete cloud backend with a committed verbatim endpoint URL — it never
    passes a TIER name to the inference layer (OMN-13470).

    The ``store`` parameter (OMN-13232) is the primary overlay authority: when
    provided, the store is consulted for a YAML overlay blob under
    ``BIFROST_OVERLAY_STORE_KEY`` before the file overlay is tried. Pass the
    lane-configured ``ProtocolSecretStore`` here; the same store instance is
    used for secret resolution later in the effect handler.

    ``config_path`` / ``overlay_path`` default to ``None`` — the contract and
    overlay are then resolved from the ``BIFROST_CONTRACT_PATH`` /
    ``BIFROST_OVERLAY_PATH`` bindings through the one seam every bifrost caller
    shares (OMN-18676). This is the entrypoint the LOCAL DISPATCH path reaches,
    and the binding it used to ignore.

    Fails closed when no backend carries a populated ``endpoint_url`` — the
    overlay (store or file) is responsible for supplying COMPLETE local endpoint
    URLs.
    """
    # OMN-18670: resolve WITH provenance so the resolved backend can name which
    # artifact supplied its ``model_id``. A caller that hands in a pre-merged
    # ``backends`` list has already performed (or bypassed) the merge, so there
    # is nothing here to attribute — that case carries an empty source rather
    # than a guessed path.
    provenance: ModelBifrostOverlayProvenance | None = None
    if backends is not None:
        merged = backends
    else:
        merged, provenance = load_bifrost_backends_with_provenance(
            config_path=config_path,
            overlay_path=overlay_path,
            store=store,
        )
    # OMN-18676: a refusal must name the overlay this resolution ACTUALLY read,
    # not the module-level default it used to be spelled with — under a
    # BIFROST_OVERLAY_PATH binding those were different files, which is the
    # whole defect. The provenance record is produced from the merge's own
    # inputs, so it cannot disagree with the merge it describes.
    overlay_reference = (
        provenance.overlay_source
        if provenance is not None and provenance.overlay_source is not None
        else "no overlay was merged"
    )
    if backend_id is not None:
        backend = _select_backend_by_id(merged, backend_id)
        if backend is None:
            raise RuntimeError(
                f"No delegation backend {backend_id!r} with a populated "
                "endpoint_url found in the bifrost config. Declare a COMPLETE "
                "endpoint_url for it in the committed config or the overlay "
                f"(store key {BIFROST_OVERLAY_STORE_KEY!r}; this resolution read "
                f"{overlay_reference})."
            )
    else:
        backend = _select_backend(merged, task_type)
        if backend is None:
            raise RuntimeError(
                "No delegation backend with a populated endpoint_url found in the "
                "bifrost config. Supply COMPLETE local endpoint URLs in the overlay "
                f"(store key {BIFROST_OVERLAY_STORE_KEY!r}; this resolution read "
                f"{overlay_reference})."
            )

    endpoint_url = backend.get("endpoint_url")
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} resolved a non-string or "
            "empty endpoint_url; the overlay must supply the COMPLETE URL."
        )

    model_name = backend.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no model_name; the "
            "overlay must resolve the model identifier at deploy time."
        )

    # OMN-13161: the per-backend output-token budget is contract-resolved — there
    # is no Python constant or env-var fallback for the value. A backend that omits
    # max_tokens (or sets a non-positive one) fails closed rather than silently
    # falling back to a magic number.
    raw_max_tokens = backend.get("max_tokens")
    if not isinstance(raw_max_tokens, int) or isinstance(raw_max_tokens, bool):
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no integer max_tokens; the "
            "routing contract (bifrost_delegation.yaml + overlay) must declare a "
            "per-backend output-token budget (OMN-13161)."
        )
    if raw_max_tokens < 1:
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} declares max_tokens="
            f"{raw_max_tokens}; the per-backend output-token budget must be >= 1."
        )

    # OMN-13170: the per-backend HTTP timeout is contract-resolved — there is no
    # Python constant or transport default that silently overrides it. A backend
    # that omits timeout_ms (or sets a non-positive one) fails closed rather than
    # falling back to the old hardcoded 120s transport cap.
    raw_timeout_ms = backend.get("timeout_ms")
    if not isinstance(raw_timeout_ms, int) or isinstance(raw_timeout_ms, bool):
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no integer timeout_ms; the "
            "routing contract (bifrost_delegation.yaml + overlay) must declare a "
            "per-backend HTTP timeout (OMN-13170)."
        )
    if raw_timeout_ms < 1:
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} declares timeout_ms="
            f"{raw_timeout_ms}; the per-backend HTTP timeout must be >= 1."
        )

    raw_headers = backend.get("extra_headers") or {}
    extra_headers = {str(k): str(v) for k, v in raw_headers.items()}

    raw_secret_ref = backend.get("secret_ref")
    secret_ref = (
        str(raw_secret_ref)
        if isinstance(raw_secret_ref, str) and raw_secret_ref.strip()
        else None
    )

    # OMN-13943: the RAW api_key_env field, carried alongside secret_ref (not
    # instead of it) so the effect boundary can fall back to the backend's own
    # literal env var when the secret_ref convention mapping misses.
    raw_api_key_env = backend.get("api_key_env")
    api_key_env = (
        str(raw_api_key_env)
        if isinstance(raw_api_key_env, str) and raw_api_key_env.strip()
        else None
    )

    resolved_backend_id = str(backend["backend_id"])

    # OMN-18670: name the artifact and key that supplied ``model_id``. This one
    # string is what the fail-closed attribution refusal was missing on
    # 2026-09-18 — it named the stale literal and the endpoint but not the
    # file, so three lanes re-derived the resolution path by hand to find an
    # untracked overlay in ``$HOME`` that no repository grep can see.
    model_id_source = ""
    if provenance is not None:
        record = provenance.source_for(resolved_backend_id, "model_name")
        if record is not None:
            model_id_source = record.describe()

    return ModelResolvedDelegationBackend(
        backend_id=resolved_backend_id,
        model_id=model_name,
        endpoint_ref=endpoint_url,
        tier=str(backend.get("tier", "unknown")),
        max_tokens=raw_max_tokens,
        timeout_ms=raw_timeout_ms,
        extra_headers=extra_headers,
        secret_ref=secret_ref,
        api_key_env=api_key_env,
        supports_response_format_json_schema=bool(
            backend.get("supports_response_format_json_schema", False)
        ),
        model_id_source=model_id_source,
    )


def resolve_effective_max_tokens(
    *, requested: int | None, backend_max_tokens: int
) -> int:
    """Resolve the effective output-token budget for one delegation call.

    The per-backend ``backend_max_tokens`` is the contract-resolved ceiling
    (OMN-13161). When the request omits ``max_tokens`` the backend value is used
    verbatim; when it supplies an explicit value the result is capped at the
    backend ceiling (``min(requested, backend_max_tokens)``) so a caller can ask
    for fewer tokens but never more than the backend allows.
    """
    if backend_max_tokens < 1:
        raise ValueError(f"backend_max_tokens must be >= 1, got {backend_max_tokens}")
    if requested is None:
        return backend_max_tokens
    if requested < 1:
        raise ValueError(f"requested max_tokens must be >= 1, got {requested}")
    return min(requested, backend_max_tokens)


def resolve_timeout_seconds(*, backend_timeout_ms: int) -> float:
    """Convert the contract-resolved per-backend timeout (ms) to seconds.

    The per-backend ``backend_timeout_ms`` is the contract-resolved HTTP timeout
    (OMN-13170). The effect handler's transport takes seconds, so the orchestrator
    converts ms → seconds here. Fails closed on a non-positive value rather than
    falling back to the old hardcoded 120s transport cap.
    """
    if backend_timeout_ms < 1:
        raise ValueError(f"backend_timeout_ms must be >= 1, got {backend_timeout_ms}")
    return backend_timeout_ms / 1000.0


__all__ = [
    "BIFROST_OVERLAY_STORE_KEY",
    "ModelResolvedDelegationBackend",
    "load_bifrost_backends",
    "resolve_delegation_backend",
    "resolve_effective_max_tokens",
    "resolve_timeout_seconds",
]
