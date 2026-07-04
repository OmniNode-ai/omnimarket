"""Live runtime-lane env collection + parity evaluation (OMN-13925).

Read-only EFFECT: snapshots each contract-declared runtime lane's runtime
container env over ssh (``docker ps`` to resolve the lane container, then
``docker inspect .Config.Env`` — configuration reads only, never a mutation,
never a ``docker exec``), then evaluates the shared env-parity engine over the
freshly collected snapshots.

Fail-fast guarantees (the OMN-13925 defect class):

- No ssh target (request field AND contract-declared env var both unset) →
  typed ``error`` result stating that no live collection input was provided.
  The handler NEVER substitutes sample/static lane data.
- ssh/docker probe failure → typed ``error`` result carrying the probe error.
- Zero lane runtime containers collected → typed ``error`` result (a parity
  verdict over zero live snapshots would be vacuous).

Raw env values never leave the handler: the receipt carries per-lane
provenance (container name/id, env var count, UTC timestamps) and the parity
verdict (presence booleans + redacted fingerprints) only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_env_parity_collect_effect.models.model_env_parity_collect_request import (
    ModelEnvParityCollectRequest,
)
from omnimarket.nodes.node_env_parity_collect_effect.models.model_env_parity_collect_result import (
    ModelEnvParityCollectResult,
    ModelEnvParityLaneCollection,
)
from omnimarket.nodes.node_env_parity_collect_effect.models.model_lane_collection_config import (
    ModelLaneCollectionConfig,
    ModelLaneCollectionLane,
)
from omnimarket.parity.engine_env_parity import evaluate_env_parity
from omnimarket.parity.model_env_parity import (
    ModelEnvParityComputeRequest,
    ModelEnvParityContractConfig,
)

_LIVE_COLLECTION_SOURCE = "live-ssh-docker-inspect"
_SAFE_CONTAINER_NAME = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9_.-]*$")


class HandlerEnvParityCollect:
    """Collect live lane env snapshots read-only and evaluate parity."""

    def __init__(self, contract_path: Path | None = None) -> None:
        self._contract_path = contract_path or Path(__file__).resolve().parents[1] / (
            "contract.yaml"
        )

    def handle(
        self, request: ModelEnvParityCollectRequest
    ) -> ModelEnvParityCollectResult:
        contract_result = self._load_contract()
        if isinstance(contract_result, str):
            return _error_result(request, contract_result)
        parity_config, collection_config = contract_result

        ssh_target = request.ssh_target or (
            os.environ.get(collection_config.ssh_target_env_var, "").strip() or None
        )
        if ssh_target is None:
            return _error_result(
                request,
                "no live collection input was provided: pass ssh_target or set "
                f"{collection_config.ssh_target_env_var}. This node never "
                "substitutes sample/static lane data for live collection.",
            )

        lanes_by_name = {lane.name: lane for lane in collection_config.lanes}
        requested_lanes = request.lanes or list(lanes_by_name)
        unknown_lanes = [name for name in requested_lanes if name not in lanes_by_name]
        if unknown_lanes:
            return _error_result(
                request,
                "requested lanes are not declared by contract lane_collection: "
                + ", ".join(unknown_lanes),
                ssh_target=ssh_target,
            )

        ps_result = self._probe_lane_containers(
            ssh_target,
            collection_config.runtime_service_label,
            request.connect_timeout_seconds,
        )
        if isinstance(ps_result, str):
            return _error_result(request, ps_result, ssh_target=ssh_target)
        containers_by_project = ps_result

        targets: dict[str, tuple[ModelLaneCollectionLane, str, str]] = {}
        for name in requested_lanes:
            lane = lanes_by_name[name]
            found = containers_by_project.get(lane.compose_project)
            if found is not None:
                targets[name] = (lane, found[0], found[1])

        if not targets:
            return _error_result(
                request,
                "live collection found zero running lane runtime containers "
                f"(service label {collection_config.runtime_service_label!r}, "
                f"lanes: {', '.join(requested_lanes)}) — refusing to emit a "
                "parity verdict over zero live snapshots.",
                ssh_target=ssh_target,
            )

        inspect_result = self._probe_container_env(
            ssh_target,
            [container_name for (_, container_name, _) in targets.values()],
            request.connect_timeout_seconds,
        )
        if isinstance(inspect_result, str):
            return _error_result(request, inspect_result, ssh_target=ssh_target)
        env_by_container = inspect_result

        collected_at = datetime.now(UTC)
        env_by_lane: dict[str, dict[str, str | None]] = {}
        lane_collections: list[ModelEnvParityLaneCollection] = []
        for name in requested_lanes:
            lane = lanes_by_name[name]
            target = targets.get(name)
            if target is None:
                lane_collections.append(
                    ModelEnvParityLaneCollection(
                        lane=name,
                        compose_project=lane.compose_project,
                        collected=False,
                        optional=lane.optional,
                        detail=(
                            "no running runtime container for compose project "
                            f"{lane.compose_project}"
                        ),
                    )
                )
                continue
            _, container_name, container_id = target
            lane_env = env_by_container.get(container_name)
            if lane_env is None:
                return _error_result(
                    request,
                    f"docker inspect returned no env for {container_name}",
                    ssh_target=ssh_target,
                )
            env_by_lane[name] = dict(lane_env)
            lane_collections.append(
                ModelEnvParityLaneCollection(
                    lane=name,
                    compose_project=lane.compose_project,
                    container_name=container_name,
                    container_id=container_id,
                    env_var_count=len(lane_env),
                    collected=True,
                    optional=lane.optional,
                    collected_at=collected_at,
                )
            )

        # Optional lanes that are down are provenance-only; required lanes
        # that are down stay in the parity lane set so the engine reports
        # lane_missing gaps for them.
        parity_lanes = [
            name
            for name in requested_lanes
            if name in env_by_lane or not lanes_by_name[name].optional
        ]
        parity = evaluate_env_parity(
            ModelEnvParityComputeRequest(
                correlation_id=request.correlation_id,
                scope=request.scope,
                lanes=parity_lanes,
                variable_names=request.variable_names,
                env_by_lane=env_by_lane,
            ),
            parity_config,
        )
        return ModelEnvParityCollectResult(
            status=parity.status,
            parity_ok=parity.parity_ok,
            scope=request.scope,
            collection_source=_LIVE_COLLECTION_SOURCE,
            ssh_target=ssh_target,
            collected_at=collected_at,
            lane_collections=lane_collections,
            parity=parity,
            correlation_id=request.correlation_id,
        )

    def _load_contract(
        self,
    ) -> tuple[ModelEnvParityContractConfig, ModelLaneCollectionConfig] | str:
        try:
            raw_contract = yaml.safe_load(
                self._contract_path.read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            return f"failed to read env parity collect contract: {exc}"
        if not isinstance(raw_contract, dict):
            return "env parity collect contract must parse to a mapping"
        raw_parity = raw_contract.get("env_parity")
        raw_collection = raw_contract.get("lane_collection")
        if not isinstance(raw_parity, dict):
            return "contract is missing env_parity config"
        if not isinstance(raw_collection, dict):
            return "contract is missing lane_collection config"
        try:
            parity_config = ModelEnvParityContractConfig.model_validate(raw_parity)
            collection_config = ModelLaneCollectionConfig.model_validate(raw_collection)
        except ValidationError as exc:
            return "invalid env parity collect contract config: " + exc.json(
                include_url=False
            )
        declared = set(parity_config.lanes)
        collectable = {lane.name for lane in collection_config.lanes}
        if declared != collectable:
            return (
                "contract env_parity.lanes and lane_collection.lanes disagree: "
                f"{sorted(declared)} vs {sorted(collectable)}"
            )
        return parity_config, collection_config

    def _probe_lane_containers(
        self, ssh_target: str, service_label: str, timeout_seconds: int
    ) -> dict[str, tuple[str, str]] | str:
        remote_command = (
            "docker ps --filter "
            f"label=com.docker.compose.service={service_label} "
            "--format '{{.Names}}\\t{{.ID}}\\t"
            '{{.Label "com.docker.compose.project"}}\''
        )
        output = self._run_ssh(ssh_target, remote_command, timeout_seconds)
        if isinstance(output, _SshError):
            return f"live container listing failed: {output.message}"
        containers_by_project: dict[str, tuple[str, str]] = {}
        for line in output.splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 3:
                continue
            container_name, container_id, compose_project = parts
            if not _SAFE_CONTAINER_NAME.match(container_name):
                return f"unsafe container name from docker ps: {container_name!r}"
            containers_by_project[compose_project] = (container_name, container_id)
        return containers_by_project

    def _probe_container_env(
        self, ssh_target: str, container_names: list[str], timeout_seconds: int
    ) -> dict[str, dict[str, str | None]] | str:
        # NOTE: `docker inspect --format` does NOT interpret \t escapes (unlike
        # `docker ps --format`), so the separator here is a single space —
        # container names cannot contain spaces and the env JSON starts at the
        # first "[" after it.
        remote_command = (
            "docker inspect --format '{{.Name}} {{json .Config.Env}}' "
            + " ".join(container_names)
        )
        output = self._run_ssh(ssh_target, remote_command, timeout_seconds)
        if isinstance(output, _SshError):
            return f"live env inspection failed: {output.message}"
        env_by_container: dict[str, dict[str, str | None]] = {}
        for line in output.splitlines():
            name, _, env_json = line.strip().partition(" ")
            if not env_json:
                continue
            try:
                entries = json.loads(env_json)
            except json.JSONDecodeError as exc:
                return f"docker inspect env parse failure for {name}: {exc}"
            if not isinstance(entries, list):
                return f"docker inspect env for {name} is not a list"
            env_map: dict[str, str | None] = {}
            for entry in entries:
                key, _, value = str(entry).partition("=")
                if key:
                    env_map[key] = value
            env_by_container[name.lstrip("/")] = env_map
        return env_by_container

    def _run_ssh(
        self, ssh_target: str, remote_command: str, timeout_seconds: int
    ) -> str | _SshError:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout_seconds}",
            ssh_target,
            remote_command,
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _SshError(f"ssh probe timed out after {timeout_seconds + 60}s")
        except OSError as exc:
            return _SshError(f"ssh invocation failed: {exc}")
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip().splitlines()[-1:]
            detail = stderr_tail[0] if stderr_tail else "no stderr"
            return _SshError(f"ssh exited {completed.returncode}: {detail}")
        return completed.stdout


class _SshError:
    """Internal sentinel carrying a probe failure message."""

    def __init__(self, message: str) -> None:
        self.message = message


def _error_result(
    request: ModelEnvParityCollectRequest, message: str, *, ssh_target: str = ""
) -> ModelEnvParityCollectResult:
    return ModelEnvParityCollectResult(
        status="error",
        parity_ok=False,
        scope=request.scope,
        ssh_target=ssh_target,
        correlation_id=request.correlation_id,
        error=message,
    )


__all__ = ["HandlerEnvParityCollect"]
