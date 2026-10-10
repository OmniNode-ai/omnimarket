# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a new deployment fact in omnimarket's packaged routing configs (OMN-20287).

``HandlerDeploymentFactGate.handle(ModelDeploymentFactGateRequest) -> ModelDeploymentFactGateResult``.

Routing decisions belong in a deployment's overlay, not in the config this
package ships (operator rulings 2026-09-26T14:31:29Z and 2026-09-26T15:24:56Z,
restated in session 2026-10-09). The fields that record such a decision carry
the deployment-fact marker on their typed models
(:mod:`omnimarket.models.delegation.model_deployment_fact_marker`). For each
packaged routing config this gate counts the non-neutral values its marked
fields carry, keyed by file, path, kind and value, in the tree and at the base
revision, and refuses:

* a value whose count in the tree is above its count at the base revision: a
  backend, endpoint, model name, secret ref, provider, tier or per-class order
  that the base did not carry there, or carried fewer times;
* a key the file's typed model does not declare, that the base revision's file
  did not already carry: a new field has to be declared, and marked when it
  records a deployment's choice, before a packaged file may use it.

The base revision is the baseline. It only shrinks: a value moved out of a
packaged file is absent from every later base, so putting it back is new again.
A value in the declared neutral local-only default set is never a fact.

Fails closed: a packaged file missing from the tree, a file that is not a
mapping, and a base revision git cannot resolve all raise instead of passing.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel

from omnimarket.inference.task_class_authority import ModelTaskClassAuthority
from omnimarket.models.delegation.model_deployment_fact_marker import (
    ModelDeploymentFactOccurrence,
    extract_deployment_facts,
    undeclared_key_paths,
)
from omnimarket.models.delegation.model_packaged_routing_tiers import (
    ModelPackagedRoutingTiers,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models.model_deployment_fact_gate import (
    ModelDeploymentFactGateRequest,
    ModelDeploymentFactGateResult,
    ModelNewDeploymentFact,
    ModelNewUndeclaredKey,
    ModelPackagedConfigTexts,
)

#: Where the packaged configs live, relative to the repository root.
PACKAGED_CONFIG_DIR: Final[str] = "src/omnimarket/configs"

#: Each packaged routing config and the typed model that marks its fields.
PACKAGED_ROUTING_CONFIGS: Final[Mapping[str, type[BaseModel]]] = {
    "bifrost_delegation.yaml": ModelBifrostDelegationConfig,
    "routing_tiers.yaml": ModelPackagedRoutingTiers,
    "task_class_contracts.v1.yaml": ModelTaskClassAuthority,
}

_GIT_TIMEOUT_S: Final[int] = 60


class DeploymentFactGateError(RuntimeError):
    """The packaged configs or the base revision could not be read to judge them."""


def _load_mapping(file_name: str, text: str, where: str) -> Mapping[str, Any]:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeploymentFactGateError(
            f"{file_name} at {where} is not YAML: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise DeploymentFactGateError(f"{file_name} at {where} is not a mapping")
    return payload


def _occurrence_key(
    fact: ModelDeploymentFactOccurrence,
) -> tuple[str, str, str, str]:
    return (fact.file_name, fact.path, fact.kind.value, fact.value)


def _non_neutral(
    facts: Iterable[ModelDeploymentFactOccurrence],
) -> list[ModelDeploymentFactOccurrence]:
    return [fact for fact in facts if not fact.neutral]


def judge_packaged_configs(
    texts: Iterable[ModelPackagedConfigTexts], base_ref: str | None
) -> ModelDeploymentFactGateResult:
    """Judge packaged config texts against their base texts. Pure."""
    new_facts: list[ModelNewDeploymentFact] = []
    new_keys: list[ModelNewUndeclaredKey] = []
    inventory: list[ModelDeploymentFactOccurrence] = []
    for entry in texts:
        model = PACKAGED_ROUTING_CONFIGS.get(entry.file_name)
        if model is None:
            raise DeploymentFactGateError(
                f"{entry.file_name} is not a packaged routing config this gate judges"
            )
        head = _load_mapping(entry.file_name, entry.head_text, "the tree")
        head_facts = _non_neutral(
            extract_deployment_facts(entry.file_name, head, model)
        )
        inventory.extend(head_facts)
        if entry.base_text is None:
            base: Mapping[str, Any] = {}
        else:
            base = _load_mapping(entry.file_name, entry.base_text, f"base {base_ref}")
        base_counts = Counter(
            _occurrence_key(fact)
            for fact in _non_neutral(
                extract_deployment_facts(entry.file_name, base, model)
            )
        )
        head_counts = Counter(_occurrence_key(fact) for fact in head_facts)
        for key in sorted(head_counts):
            if head_counts[key] > base_counts[key]:
                file_name, path, _kind, value = key
                sample = next(f for f in head_facts if _occurrence_key(f) == key)
                new_facts.append(
                    ModelNewDeploymentFact(
                        file_name=file_name,
                        path=path,
                        kind=sample.kind,
                        value=value,
                        base_count=base_counts[key],
                        head_count=head_counts[key],
                    )
                )
        base_keys = undeclared_key_paths(base, model)
        for path in sorted(undeclared_key_paths(head, model) - base_keys):
            new_keys.append(ModelNewUndeclaredKey(file_name=entry.file_name, path=path))
    return ModelDeploymentFactGateResult(
        base_ref=base_ref,
        new_facts=tuple(new_facts),
        new_undeclared_keys=tuple(new_keys),
        inventory=tuple(inventory),
    )


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )


def read_packaged_config_texts(
    repo_root: Path, base_ref: str | None
) -> tuple[ModelPackagedConfigTexts, ...]:
    """Read each packaged routing config from the tree and from ``base_ref``."""
    if base_ref is not None:
        resolved = _git(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        if resolved.returncode != 0:
            raise DeploymentFactGateError(
                f"base revision {base_ref!r} does not resolve in {repo_root}: "
                f"{resolved.stderr.strip()}"
            )
    texts: list[ModelPackagedConfigTexts] = []
    for file_name in PACKAGED_ROUTING_CONFIGS:
        relative = f"{PACKAGED_CONFIG_DIR}/{file_name}"
        head_path = repo_root / relative
        if not head_path.is_file():
            raise DeploymentFactGateError(f"packaged config {relative} is missing")
        base_text: str | None = None
        if base_ref is not None:
            shown = _git(repo_root, "show", f"{base_ref}:{relative}")
            if shown.returncode == 0:
                base_text = shown.stdout
            else:
                listed = _git(
                    repo_root, "ls-tree", "--name-only", base_ref, "--", relative
                )
                if listed.returncode != 0 or listed.stdout.strip():
                    raise DeploymentFactGateError(
                        f"cannot read {relative} at {base_ref}: {shown.stderr.strip()}"
                    )
        texts.append(
            ModelPackagedConfigTexts(
                file_name=file_name,
                base_text=base_text,
                head_text=head_path.read_text(encoding="utf-8"),
            )
        )
    return tuple(texts)


class HandlerDeploymentFactGate:
    """EFFECT: read the packaged routing configs and judge them against the base revision."""

    def handle(
        self, request: ModelDeploymentFactGateRequest
    ) -> ModelDeploymentFactGateResult:
        texts = read_packaged_config_texts(Path(request.repo_root), request.base_ref)
        return judge_packaged_configs(texts, request.base_ref)


__all__: list[str] = [
    "PACKAGED_CONFIG_DIR",
    "PACKAGED_ROUTING_CONFIGS",
    "DeploymentFactGateError",
    "HandlerDeploymentFactGate",
    "judge_packaged_configs",
    "read_packaged_config_texts",
]
