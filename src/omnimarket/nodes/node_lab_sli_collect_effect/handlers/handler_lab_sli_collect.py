# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Collect one interval of lab indicator readings from the sources that exist.

Canonical definition-B shape: ``handle(ModelLabSliCollectRequest) ->
ModelLabSliReadingBatch``. Every read goes through the injected probe port.
A source that cannot be read becomes a ``source_errors`` line and its
indicators carry no reading, so node_lab_sli_compute evaluates them unknown;
nothing is defaulted to healthy. This node holds no target: it measures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lab_sli import (
    ModelLabSliReading,
    ModelLabSliReadingBatch,
    ModelLabSliSubjectReading,
)
from omnimarket.nodes.node_lab_sli_collect_effect.models import (
    ModelLabSliCollectRequest,
)
from omnimarket.nodes.node_lab_sli_collect_effect.protocols import (
    LocalLabSliProbe,
    ProbeError,
    ProtocolLabSliProbe,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_GB = 1024**3


class ModelLabSliCollectConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    merge_window_minutes: int = Field(ge=1)
    launchd_label_prefixes: tuple[str, ...]
    fseventsd_dir_name: str = Field(min_length=1)


def load_collect_config(path: Path = CONTRACT_PATH) -> ModelLabSliCollectConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = (raw.get("config") or {}).get("lab_sli_collect")
    if not isinstance(block, dict):
        raise ValueError(f"{path}: config.lab_sli_collect is missing")
    return ModelLabSliCollectConfig.model_validate(block)


@lru_cache(maxsize=1)
def _contract_config() -> ModelLabSliCollectConfig:
    return load_collect_config()


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _one(
    sli_id: str, subject: str, value: float, detail: str = ""
) -> ModelLabSliReading:
    return ModelLabSliReading(
        sli_id=sli_id,
        subjects=(ModelLabSliSubjectReading(subject=subject, value=round(value, 3)),),
        detail=detail,
    )


def read_pr_watcher(
    state: dict[str, Any], now: datetime, merge_window_minutes: int
) -> list[ModelLabSliReading]:
    """Oldest green unlanded PR per repo, and merges per hour for repos with green work."""
    oldest: dict[str, tuple[float, str]] = {}
    for key, pr in (state.get("prs") or {}).items():
        facts = pr.get("facts") or {}
        ci = pr.get("ci")
        if (
            facts.get("state") != "OPEN"
            or facts.get("draft")
            or not isinstance(ci, dict)
        ):
            continue
        if ci.get("verdict") != "GREEN" or not str(pr.get("cls", "")).startswith(
            "green"
        ):
            continue
        finished = [
            t
            for t in (_parse_ts(run[3]) for run in ci.get("runs") or [] if len(run) > 3)
            if t
        ]
        if not finished:
            continue
        minutes = (now - max(finished)).total_seconds() / 60
        repo = str(facts.get("repo") or key.split("#")[0])
        if repo not in oldest or minutes > oldest[repo][0]:
            oldest[repo] = (minutes, str(key))
    readings: list[ModelLabSliReading] = []
    if oldest:
        readings.append(
            ModelLabSliReading(
                sli_id="oldest_green_unlanded_minutes",
                subjects=tuple(
                    ModelLabSliSubjectReading(subject=name, value=round(minutes, 1))
                    for minutes, name in sorted(oldest.values(), reverse=True)
                ),
            )
        )
    else:
        readings.append(
            ModelLabSliReading(
                sli_id="oldest_green_unlanded_minutes",
                applicable=False,
                detail="no green unlanded PR",
            )
        )
    since = now - timedelta(minutes=merge_window_minutes)
    merges: dict[str, int] = dict.fromkeys(oldest, 0)
    for merge in (state.get("merges") or {}).values():
        repo = str(merge.get("repo", ""))
        merged_at = _parse_ts(merge.get("merged_at"))
        if repo in merges and merged_at is not None and merged_at >= since:
            merges[repo] += 1
    hours = merge_window_minutes / 60
    if merges:
        readings.append(
            ModelLabSliReading(
                sli_id="merges_per_hour_per_repo",
                subjects=tuple(
                    ModelLabSliSubjectReading(
                        subject=repo, value=round(count / hours, 3)
                    )
                    for repo, count in sorted(merges.items())
                ),
                detail=f"repos with green work waiting, merges in the last {merge_window_minutes} min",
            )
        )
    else:
        readings.append(
            ModelLabSliReading(
                sli_id="merges_per_hour_per_repo",
                applicable=False,
                detail="no repo has green work waiting",
            )
        )
    return readings


def _darwin_free_gb(vm_stat: str) -> float:
    size = re.search(r"page size of (\d+) bytes", vm_stat)
    if not size:
        raise ProbeError("vm_stat has no page size")
    page = int(size.group(1))
    pages = 0
    for name in (
        "Pages free",
        "Pages inactive",
        "Pages speculative",
        "Pages purgeable",
    ):
        match = re.search(rf"{name}:\s+(\d+)", vm_stat)
        if match:
            pages += int(match.group(1))
    return pages * page / _GB


def _darwin_swap_gb(swapusage: str) -> float:
    match = re.search(r"used = ([\d.]+)([MG])", swapusage)
    if not match:
        raise ProbeError(f"vm.swapusage unparsed: {swapusage.strip()[:80]}")
    value = float(match.group(1))
    return value / 1024 if match.group(2) == "M" else value


def _meminfo(text: str, field: str) -> float:
    match = re.search(rf"^{field}:\s+(\d+) kB", text, re.MULTILINE)
    if not match:
        raise ProbeError(f"/proc/meminfo has no {field}")
    return int(match.group(1)) * 1024 / _GB


def _launchd_failures(listing: str, prefixes: tuple[str, ...]) -> list[str]:
    failing = []
    for line in listing.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].startswith(prefixes):
            continue
        status = parts[1].strip()
        if status not in ("0", "-"):
            failing.append(f"{parts[2]}={status}")
    return sorted(failing)


class HandlerLabSliCollect:
    """Read the host's sources once and return the interval's readings."""

    def __init__(
        self,
        probe: ProtocolLabSliProbe | None = None,
        config: ModelLabSliCollectConfig | None = None,
    ) -> None:
        self._probe: ProtocolLabSliProbe = (
            probe if probe is not None else LocalLabSliProbe()
        )
        self._config = config if config is not None else _contract_config()

    def handle(self, request: ModelLabSliCollectRequest) -> ModelLabSliReadingBatch:
        readings: list[ModelLabSliReading] = []
        errors: list[str] = []
        host = request.collector_host

        def attempt(source: str, read: Callable[[], list[ModelLabSliReading]]) -> None:
            try:
                readings.extend(read())
            except (ProbeError, ValueError, KeyError, AttributeError) as exc:
                errors.append(f"{source}: {exc}")

        if request.pr_watcher_state_path:
            attempt(
                "pr-watcher-state",
                lambda: read_pr_watcher(
                    json.loads(self._probe.read_text(request.pr_watcher_state_path)),
                    request.now,
                    self._config.merge_window_minutes,
                ),
            )
        system = self._probe.system()
        if system == "Darwin":
            attempt(
                "vm_stat",
                lambda: [
                    _one(
                        "host_free_memory_gb",
                        host,
                        _darwin_free_gb(self._probe.run(("vm_stat",))),
                    )
                ],
            )
            attempt(
                "vm.swapusage",
                lambda: [
                    _one(
                        "host_swap_used_gb",
                        host,
                        _darwin_swap_gb(
                            self._probe.run(("sysctl", "-n", "vm.swapusage"))
                        ),
                    )
                ],
            )
            attempt(
                "kern.memorystatus_level",
                lambda: [
                    _one(
                        "host_memory_pressure_psi",
                        host,
                        100
                        - float(
                            self._probe.run(
                                ("sysctl", "-n", "kern.memorystatus_level")
                            ).strip()
                        ),
                        "Darwin: 100 - kern.memorystatus_level",
                    )
                ],
            )
            attempt("fseventsd", lambda: [self._fseventsd(host)])
            attempt(
                "launchctl",
                lambda: [self._scheduler(host, self._probe.run(("launchctl", "list")))],
            )
        elif system == "Linux":
            attempt(
                "/proc/meminfo",
                lambda: self._linux_memory(
                    host, self._probe.read_text("/proc/meminfo")
                ),
            )
            attempt(
                "/proc/pressure/memory",
                lambda: [
                    self._linux_psi(
                        host, self._probe.read_text("/proc/pressure/memory")
                    )
                ],
            )
            readings.append(
                ModelLabSliReading(
                    sli_id="host_fseventsd_gb",
                    applicable=False,
                    detail="Linux has no fseventsd",
                )
            )
            attempt(
                "systemctl",
                lambda: [
                    self._systemd(
                        host,
                        self._probe.run(
                            ("systemctl", "--failed", "--plain", "--no-legend")
                        ),
                    )
                ],
            )
        else:
            errors.append(f"host-probe: no reader for {system}")
        if request.disk_paths:
            attempt(
                "disk",
                lambda: [
                    ModelLabSliReading(
                        sli_id="host_disk_free_gb",
                        subjects=tuple(
                            ModelLabSliSubjectReading(
                                subject=f"{host}:{path}",
                                value=round(self._probe.disk_free_bytes(path) / _GB, 1),
                            )
                            for path in request.disk_paths
                        ),
                    )
                ],
            )
        return ModelLabSliReadingBatch(
            interval_start=request.interval_start,
            collected_at=request.now,
            collector_host=host,
            readings=tuple(readings),
            source_errors=tuple(errors),
        )

    def _fseventsd(self, host: str) -> ModelLabSliReading:
        roots = ["/"]
        problems: list[str] = []
        try:
            roots.extend(
                re.findall(r" on (\S+/Data) \(apfs", self._probe.run(("mount",)))
            )
        except ProbeError as exc:
            problems.append(str(exc))
        for root in reversed(roots):
            path = str(Path(root) / self._config.fseventsd_dir_name)
            try:
                return _one(
                    "host_fseventsd_gb", host, self._probe.tree_bytes(path) / _GB, path
                )
            except ProbeError as exc:
                problems.append(str(exc))
        raise ProbeError("; ".join(problems))

    def _scheduler(self, host: str, listing: str) -> ModelLabSliReading:
        failing = _launchd_failures(listing, self._config.launchd_label_prefixes)
        return _one(
            "scheduled_job_failures",
            host,
            float(len(failing)),
            "nonzero last exit: " + ", ".join(failing)
            if failing
            else "every unit exited 0",
        )

    @staticmethod
    def _systemd(host: str, listing: str) -> ModelLabSliReading:
        failing = sorted(
            line.split()[0] for line in listing.splitlines() if line.strip()
        )
        return _one(
            "scheduled_job_failures",
            host,
            float(len(failing)),
            "failed units: " + ", ".join(failing) if failing else "no failed unit",
        )

    @staticmethod
    def _linux_memory(host: str, meminfo: str) -> list[ModelLabSliReading]:
        return [
            _one("host_free_memory_gb", host, _meminfo(meminfo, "MemAvailable")),
            _one(
                "host_swap_used_gb",
                host,
                _meminfo(meminfo, "SwapTotal") - _meminfo(meminfo, "SwapFree"),
            ),
        ]

    @staticmethod
    def _linux_psi(host: str, pressure: str) -> ModelLabSliReading:
        match = re.search(r"^some .*avg60=([\d.]+)", pressure, re.MULTILINE)
        if not match:
            raise ProbeError("no 'some avg60' line")
        return _one(
            "host_memory_pressure_psi", host, float(match.group(1)), "PSI some avg60"
        )


__all__ = ["HandlerLabSliCollect", "load_collect_config", "read_pr_watcher"]
