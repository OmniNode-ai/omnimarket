"""Preserved lab render edit, now a definition-B EFFECT handler (OMN-20688)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..models.model_lab_secret_store_override_request import (
    ModelLabSecretStoreOverrideRequest,
)
from ..models.model_lab_secret_store_override_result import (
    ModelLabSecretStoreOverrideResult,
)

CONFIGMAP_NAME = "onex-runtime-config"

#: The resolver-config key both this script and the committed overlay write.
RESOLVER_KEY = "ONEX_SECRET_RESOLVER_CONFIG_JSON"


def _resolver_json(document: dict[str, Any]) -> tuple[int, int, dict[str, Any]]:
    """Find the committed op that replaces the resolver config, and parse it.

    Returns the patch index, the op index, and the decoded JSON, so the caller
    can rewrite that op in place. Reading the lane's mappings out of the lane's
    own patch is what keeps this script from becoming a second place where the
    five env mappings are written down.
    """
    for patch_index, entry in enumerate(document.get("patches") or []):
        target = entry.get("target") or {}
        if target.get("kind") != "ConfigMap" or target.get("name") != CONFIGMAP_NAME:
            continue
        ops = yaml.safe_load(entry["patch"])
        if not isinstance(ops, list):
            continue
        for op_index, op in enumerate(ops):
            if isinstance(op, dict) and op.get("path") == f"/data/{RESOLVER_KEY}":
                return patch_index, op_index, json.loads(op["value"])
    raise LookupError(
        f"the kustomization declares no op replacing /data/{RESOLVER_KEY} on "
        f"{CONFIGMAP_NAME}. This script edits that op rather than adding a "
        "second one, so its absence is a change to the overlay that has to be "
        "understood, not routed around."
    )


def base_infisical_mappings(
    kustomization: Path, base_configmap: Path
) -> list[dict[str, Any]]:
    """The base's ``source_type=infisical`` mappings, read from the base ConfigMap.

    THE SIBLING OF ``base_namespaces`` BELOW, AND FOR THE SAME REASON (OMN-18307).
    A logical name the platform resolves from the store -- today
    ``database.tenant_projection.dsn`` (OMN-17556) -- is served by a ``mappings``
    entry, and the lane's committed patch carries only the base's ENV mappings.
    So the store-backed one was dropped under the binding too, and the namespace
    restore did not cover it: a namespace rule serves a ref minted at request
    time, never a fixed logical name.

    MEASURED CONSEQUENCE on the bound lab lane. ``HandlerProjectionDelegation``
    failed auto-wiring with ``Projection handler requires topology bindings with
    configured DSNs: tenant_projection:...``, the runtime health monitor reported
    ``1/1 declared projection(s) have no attached consumer and persist nothing:
    projection_delegation``, and ``delegation_events`` /
    ``projection_delegation_summary`` held zero rows in ``omnidash_analytics``
    while delegations completed on the lane. The pod stayed Ready throughout --
    this Deployment does not carry ``ONEX_WIRING_STRICT_MODE=1``, so
    ``wired=0 failed=1`` is a WARNING, not a boot failure. Green while dead.

    RESTORED ONLY UNDER THE BINDING, exactly like the namespace rules: declaring
    an Infisical source on an UNBOUND lane makes
    ``config_declares_infisical_source`` true, the runtime builds the Infisical
    handler at store construction, every bootstrap var must then be non-blank,
    and ``INFISICAL_ADDR`` is the empty string there -- which is the OMN-17779
    CrashLoopBackOff, verbatim.

    THIS DOES NOT DISTURB THE BYOK READ PATH, and the reason is worth writing
    down rather than rediscovering. omnimarket's ``_lane_infisical_handler``
    derives ONE default ``secret_path`` from the lane's house mappings, and
    deliberately does not union the namespace folders into it. The base mapping
    is folder-qualified (``/dev/onex-runtime/...``) and every namespace read is
    folder-qualified by its own template, so the two folders coexist on one lane
    -- which is the shape the real BYOK lane already runs (OMN-16944). A
    folder-LESS base mapping would be a loud refusal instead, and
    ``tests/k8s/test_onex_lab_store_bound_resolver_mappings.py`` fails on one
    rather than leaving it to a pod log.
    """
    base = (kustomization.parent / base_configmap).resolve()
    document = yaml.safe_load(base.read_text(encoding="utf-8"))
    parsed = json.loads(document["data"][RESOLVER_KEY])
    mappings = parsed.get("mappings", [])
    if not isinstance(mappings, list):
        raise TypeError(f"{base}: {RESOLVER_KEY}.mappings is not a list")
    return [
        mapping
        for mapping in mappings
        if mapping.get("source", {}).get("source_type") == "infisical"
    ]


def base_namespaces(kustomization: Path, base_configmap: Path) -> list[dict[str, Any]]:
    """The base's ref-pattern namespace rules, read from the base ConfigMap.

    WHY THIS IS HERE AT ALL. A ref minted at request time -- a tenant's
    ``cred_<tenant>_<provider>_<hex>`` -- is served by a ``namespaces`` rule,
    not by a ``mappings`` entry: the rule anchors a PATTERN and a path template
    instead of one logical name. The base declares exactly one such rule and
    the lane's committed patch carries only the base's ``mappings``, so the
    rule was dropped silently. Measured consequence on the lab lane: the
    product surface accepted a customer key and wrote it to the store, and the
    delegation that should have used it failed with "Secret resolution failed
    (configuration issue): cred_..." -- the write half working and the read half
    absent, which is worse than neither, because the catalogue row says the key
    is registered.

    It is restored ONLY under the binding. An Infisical-backed namespace rule
    makes ``config_declares_infisical_source`` true, and the runtime then builds
    the Infisical handler at store construction and requires every bootstrap
    var to be non-blank -- so declaring one on an UNBOUND lane is the OMN-17779
    CrashLoopBackOff, exactly as that patch's comment says.
    """
    base = (kustomization.parent / base_configmap).resolve()
    document = yaml.safe_load(base.read_text(encoding="utf-8"))
    raw = document["data"][RESOLVER_KEY]
    namespaces = json.loads(raw).get("namespaces") or []
    if not isinstance(namespaces, list):
        raise TypeError(f"{base}: {RESOLVER_KEY}.namespaces is not a list")
    return namespaces


def patch_entry(addr: str, environment_slug: str) -> dict[str, Any]:
    """The kustomize ``patches:`` entry for the two addressing keys, as data.

    A separate function so the test can assert its shape without running the
    file and without re-spelling the YAML.
    """
    body = "\n".join(
        [
            "- op: replace",
            "  path: /data/INFISICAL_ADDR",
            f'  value: "{addr}"',
            "- op: replace",
            "  path: /data/INFISICAL_ENVIRONMENT_SLUG",
            f'  value: "{environment_slug}"',
        ]
    )
    return {
        "target": {"version": "v1", "kind": "ConfigMap", "name": CONFIGMAP_NAME},
        "patch": body,
    }


def add_override(
    document: dict[str, Any],
    addr: str,
    environment_slug: str,
    namespaces: list[dict[str, Any]],
    infisical_mappings: list[dict[str, Any]] | None = None,
) -> None:
    """Apply both halves of the binding, in place, preserving what is there.

    Half one appends the addressing patch. Half two REWRITES the committed op
    that replaces the resolver config, adding the base's namespace rules AND the
    base's Infisical mappings to the lane's own env mappings -- it rewrites
    rather than appending a second op, because two ops replacing one path is a
    value decided by ordering.

    The Infisical mappings are APPENDED to the lane's list, never substituted
    for it (OMN-18307). The lane's committed patch is the only place its five
    env mappings are written down; rebuilding the list from the base instead
    would silently re-import whatever else the base grows, which is the failure
    mode the OMN-17779 copy exists to avoid in the first place. A base mapping
    whose logical name the lane already declares at ``source_type`` ``env`` is
    left to the lane: the lane substituted it on purpose, and an env mapping is
    a lane fact on both branches.

    ONE EXCEPTION, AND IT IS THE WHOLE REASON THIS FUNCTION HAS A PRECEDENCE
    RULE AT ALL (OMN-18364). A lane mapping at ``source_type`` ``file`` YIELDS
    to a restored base store mapping of the same logical name. A file mapping
    on this lane is never a substitution made on purpose; it exists only
    because the UNBOUND lane has no store and still has to put a value behind a
    logical name the base resolves from one -- today
    ``database.tenant_projection.dsn``, whose absence took both kernel-pod
    projection writers down under OMN-18324 strict wiring and blocked delivery
    to staging for a day. The BOUND lab lane does have a store, the applier
    files the real value into it before the apply, and that lane is the ONLY
    surface on which the store-resolved path can be proven at all -- which is
    the proof OMN-18307 was opened to obtain. Leaving the CI stand-in in place
    there would not break the lane; it would quietly replace that proof with a
    restatement of the CI lane, which is the more expensive failure because it
    looks green.

    The rule is stated by SOURCE TYPE rather than by logical name so it needs no
    list to maintain, and
    ``tests/k8s/test_onex_lab_tenant_projection_dsn_omn18364.py`` fails if the
    bound branch ever resolves that name from anything but the store.

    Refuses a ``patches`` value that is not a list rather than replacing it: a
    kustomization whose patches key is some other shape is one this script does
    not understand, and overwriting it is how the committed patches disappear.
    """
    existing = document.get("patches")
    if existing is None:
        document["patches"] = []
    elif not isinstance(existing, list):
        raise TypeError(
            "the kustomization's `patches` is not a list; refusing to edit it "
            "rather than replacing the patches this lane depends on"
        )
    patch_index, op_index, resolver = _resolver_json(document)
    resolver["namespaces"] = namespaces
    restored = {mapping["logical_name"] for mapping in infisical_mappings or []}
    lane_mappings = [
        mapping
        for mapping in resolver.get("mappings") or []
        if not (
            mapping["logical_name"] in restored
            and mapping["source"]["source_type"] == "file"
        )
    ]
    declared = {mapping["logical_name"] for mapping in lane_mappings}
    resolver["mappings"] = lane_mappings + [
        mapping
        for mapping in infisical_mappings or []
        if mapping["logical_name"] not in declared
    ]
    ops = yaml.safe_load(document["patches"][patch_index]["patch"])
    ops[op_index]["value"] = json.dumps(resolver, separators=(",", ":"))
    document["patches"][patch_index]["patch"] = yaml.safe_dump(
        ops, sort_keys=False, default_flow_style=False
    )
    document["patches"].append(patch_entry(addr, environment_slug))


class HandlerLabSecretStoreOverride:
    """Edit the ephemeral render copy; return its typed terminal result."""

    async def handle(
        self, request: ModelLabSecretStoreOverrideRequest
    ) -> ModelLabSecretStoreOverrideResult:
        path = request.kustomization
        addr = request.addr.strip()
        environment_slug = request.environment_slug.strip()
        if not addr or not environment_slug:
            return ModelLabSecretStoreOverrideResult(
                exit_code=2,
                message="error: both the store address and the environment slug are "
                "required and neither may be blank -- a blank slug is what "
                "OMN-17349 AC5 refuses.",
            )
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return ModelLabSecretStoreOverrideResult(
                exit_code=2, message=f"error: {path} is not a kustomization mapping"
            )
        before = len(document.get("patches") or [])
        try:
            namespaces = base_namespaces(path, request.base_configmap)
            infisical_mappings = base_infisical_mappings(path, request.base_configmap)
            add_override(
                document, addr, environment_slug, namespaces, infisical_mappings
            )
        except (LookupError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return ModelLabSecretStoreOverrideResult(
                exit_code=1, message=f"error: {exc}"
            )
        path.write_text(
            "# REWRITTEN by node_lab_secret_store_override_effect (OMN-20688).\n"
            "# An ephemeral render copy. Comments from the committed file are lost\n"
            "# to the round trip; the committed file itself is never touched.\n"
            + yaml.safe_dump(document, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        after = len(document["patches"])
        return ModelLabSecretStoreOverrideResult(
            exit_code=0,
            message=f"secret-store override added: patches {before} -> {after}, "
            f"resolver namespaces restored from the base: "
            f"{[rule.get('namespace') for rule in namespaces]}, "
            f"resolver infisical mappings restored from the base: "
            f"{[mapping.get('logical_name') for mapping in infisical_mappings]}",
        )
