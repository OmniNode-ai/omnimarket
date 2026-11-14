# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime facts of the merged-change reading, read from the runtime container's log and the units' state.

``parse_runtime_log`` is pure: log text in, loaded nodes, joined consumers and published results out, by the patterns
the effect's contract declares. ``SourceRuntimeFacts`` fetches the text and the unit state over ssh and names every
fact group it could not read, so a reader outage is an unread verdict downstream and never a not-running one.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.model_merged_change_reading import (
    ModelMcrLoadedNode,
    ModelMcrPublished,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
    ModelMcrSubscription,
    ModelMcrUnitRun,
)
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrLogLabels,
    ModelMcrUnitHost,
)

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this reader; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


def _stamp(text: str, labels: ModelMcrLogLabels) -> datetime:
    return datetime.strptime(text, labels.time_format).replace(tzinfo=UTC)


def parse_runtime_log(text: str, labels: ModelMcrLogLabels) -> ModelMcrRuntimeFacts:
    """Loaded nodes, consumers that joined, and the last output event per topic, from the runtime's log text."""
    loaded_re = re.compile(labels.loaded_pattern)
    joined_re = re.compile(labels.subscribed_pattern)
    published_re = re.compile(labels.published_pattern)
    loaded: dict[str, datetime] = {}
    joined: dict[tuple[str, str], datetime] = {}
    published: dict[str, tuple[datetime, int]] = {}
    for line in text.splitlines():
        hit = loaded_re.search(line)
        if hit:
            loaded.setdefault(hit["node"], _stamp(hit["ts"], labels))
            continue
        hit = joined_re.search(line)
        if hit:
            joined[(hit["node"], hit["topic"])] = _stamp(hit["ts"], labels)
            continue
        hit = published_re.search(line)
        if hit:
            at = _stamp(hit["ts"], labels)
            last, count = published.get(hit["topic"], (at, 0))
            published[hit["topic"]] = (max(last, at), count + 1)
    return ModelMcrRuntimeFacts(
        loaded_nodes=tuple(
            ModelMcrLoadedNode(node=n, loaded_at=at) for n, at in sorted(loaded.items())
        ),
        subscriptions=tuple(
            ModelMcrSubscription(node=n, topic=t, joined_at=at)
            for (n, t), at in sorted(joined.items())
        ),
        published=tuple(
            ModelMcrPublished(topic=t, last_at=at, count=count)
            for t, (at, count) in sorted(published.items())
        ),
    )


def parse_unit_state(
    output: str, unit: str, labels: ModelMcrLogLabels
) -> ModelMcrUnitRun:
    """``systemctl show`` lines (KEY=value) into the last run: its exit time and the result template filled in."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    stamp = values.get("ExecMainExitTimestamp", "")
    if not stamp:
        raise ValueError(f"{unit}: no exit timestamp")
    when = datetime.strptime(stamp, labels.unit_time_format).replace(tzinfo=UTC)
    return ModelMcrUnitRun(
        unit=unit,
        last_run_at=when,
        result=labels.unit_result_template.format(**values),
        ok=values.get("Result") == labels.unit_ok_result,
    )


class SourceRuntimeFacts:
    """Reads the runtime container's log and the units' last runs over ssh."""

    def __init__(
        self,
        *,
        overlay: ModelRuntimeHostReadOverlay,
        labels: ModelMcrLogLabels,
        unit_hosts: Sequence[ModelMcrUnitHost],
        environ: Mapping[str, str],
        manifest_url: str = "",
        run: Runner = run_process,
    ) -> None:
        self._manifest_url = manifest_url
        self._overlay = overlay
        self._labels = labels
        self._unit_hosts = {h.host: h for h in unit_hosts}
        self._environ = environ
        self._run = run

    def _ssh(self, env_name: str, script: str, timeout: int) -> str:
        host = self._environ.get(env_name, "").strip()
        if not host:
            raise ValueError(f"{env_name} is not set")
        done = self._run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "--",
                host,
                "bash -s",
            ],
            script.encode(),
            timeout,
        )
        if done.returncode:
            raise ValueError(f"the read on {env_name} exited {done.returncode}")
        return done.stdout.decode(errors="replace")

    def _container(self) -> str:
        o = self._overlay
        return (
            "RT=$(docker ps --filter "
            f"label=com.docker.compose.project={o.compose_project} --filter "
            f"label=com.docker.compose.service={o.provenance_service} "
            "--filter status=running --format '{{.Names}}' | head -1)\n"
            '[ -n "$RT" ]\n'
        )

    def build_started_at(self) -> datetime:
        """When the running container started, by the template the contract declares."""
        script = (
            "set -eu\n"
            + self._container()
            + "docker inspect \"$RT\" --format '{{.State.StartedAt}}'\n"
        )
        text = self._ssh(
            self._overlay.ssh_host_env, script, self._labels.docker_timeout_seconds
        ).strip()
        return datetime.strptime(text[:19], self._labels.started_at_template).replace(
            tzinfo=UTC
        )

    def manifest_nodes(self) -> tuple[str, ...]:
        """The names of the contracts the runtime serves in its introspection manifest."""
        labels = self._labels
        text = self._ssh(
            self._overlay.ssh_host_env,
            f"curl {labels.manifest_fetch_options} {shlex.quote(self._manifest_url)}\n",
            labels.docker_timeout_seconds,
        )
        document = json.loads(text)
        names = tuple(
            item[labels.manifest_name_key]
            for item in document[labels.manifest_contracts_key]
            if isinstance(item.get(labels.manifest_name_key), str)
        )
        if not names:
            raise ValueError("the manifest lists no contracts")
        return names

    def read(
        self, runners: Sequence[ModelMcrRunner]
    ) -> tuple[ModelMcrRuntimeFacts, datetime | None]:
        """The facts and the build start. A group a read failed on is named in ``unread``."""
        unread: list[str] = []
        facts = ModelMcrRuntimeFacts()
        started: datetime | None = None
        try:
            script = (
                "set -eu\n"
                + self._container()
                + f'docker logs "$RT" 2>&1 | grep -E {shlex.quote(self._labels.filter_regex)} || true\n'
            )
            text = self._ssh(
                self._overlay.ssh_host_env, script, self._labels.docker_timeout_seconds
            )
            facts = parse_runtime_log(text, self._labels)
            started = self.build_started_at()
        except (OSError, ValueError, subprocess.SubprocessError):
            unread.extend(("loaded_nodes", "subscriptions", "published"))
        if self._manifest_url:
            try:
                names = self.manifest_nodes()
                known = {item.node for item in facts.loaded_nodes}
                facts = ModelMcrRuntimeFacts(
                    loaded_nodes=(
                        *facts.loaded_nodes,
                        *(ModelMcrLoadedNode(node=n) for n in names if n not in known),
                    ),
                    subscriptions=facts.subscriptions,
                    published=facts.published,
                )
                if "loaded_nodes" in unread:
                    unread.remove("loaded_nodes")
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ):
                pass
        # A runtime that shows no loaded node is a read that lost its lines (the container log rotates), never a fact.
        if not facts.loaded_nodes and "loaded_nodes" not in unread:
            unread.append("loaded_nodes")
        units: list[ModelMcrUnitRun] = []
        wanted = sorted({(r.unit, r.host) for r in runners if r.unit})
        for unit, host in wanted:
            entry = self._unit_hosts.get(host)
            try:
                if entry is None:
                    raise ValueError(f"no ssh target is declared for host {host!r}")
                out = self._ssh(
                    entry.ssh_host_env,
                    f"systemctl show {self._labels.unit_show_options} -p {self._labels.unit_show_properties} {shlex.quote(unit)}\n",
                    self._labels.docker_timeout_seconds,
                )
                units.append(parse_unit_state(out, unit, self._labels))
            except (OSError, ValueError, subprocess.SubprocessError):
                if "unit_runs" not in unread:
                    unread.append("unit_runs")
        return (
            ModelMcrRuntimeFacts(
                loaded_nodes=facts.loaded_nodes,
                subscriptions=facts.subscriptions,
                published=facts.published,
                unit_runs=tuple(units),
                unread=tuple(unread),
            ),
            started,
        )
