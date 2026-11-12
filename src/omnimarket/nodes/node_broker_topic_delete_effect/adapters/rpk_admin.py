# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker's live topic evidence and topic deletes as the dev broker's superuser (OMN-23948).

The dev lane's machine login cannot Describe most unused topics, so it reads them back as absent. The
superuser can, but its credential lives only in the broker host's source container. This adapter
reads it the way the broker permission tool does (``node_broker_acl_reconcile_effect``): the script
prologue is that tool's ``build_remote_script``, so the credential is read on the broker host, held in
two shell variables there, and never reaches this process, a command line or a log. The lane is
``dev`` only; any other lane refuses before a single remote call.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence

from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.driver_broker_acl import (
    AclConnection,
    DeclarationError,
    build_remote_script,
    run_remote,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelLiveTopicEvidence,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    ModelTopicDeleteOverlay,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import BrokerAdminError

#: The only lane whose superuser this adapter reads.
DEV_LANE = "dev"
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_CREDENTIAL_MARKER = "no lane superuser credentials"
_Runner = Callable[..., str]


def _valid_names(names: Sequence[str]) -> None:
    for name in names:
        if not _TOPIC_NAME.fullmatch(name):
            raise BrokerAdminError(f"topic_name_invalid:{name}")


def _rpk_function(overlay: ModelTopicDeleteOverlay) -> str:
    """The shell function ``R`` that runs rpk in the broker container with the host-held login."""
    access = overlay.superuser
    assert access is not None
    return (
        f'R() {{ docker exec {shlex.quote(access.broker_container)} rpk "$@" '
        f"-X brokers={shlex.quote(access.rpk_brokers)} "
        f"-X sasl.mechanism={shlex.quote(access.sasl_mechanism)} "
        '-X user="$U" -X pass="$P"; }'
    )


class RpkSuperuserTopicAdmin:
    """Live evidence and deletes of named topics as the dev broker superuser, through the broker host."""

    def __init__(
        self,
        overlay: ModelTopicDeleteOverlay,
        environ: Mapping[str, str],
        *,
        runner: _Runner = run_remote,
    ) -> None:
        access = overlay.superuser
        if access is None:
            raise BrokerAdminError("superuser_not_declared")
        if access.lane != DEV_LANE:
            raise BrokerAdminError(f"superuser_lane_not_dev:{access.lane}")
        if access.lane != overlay.bus_lane:
            raise BrokerAdminError(
                f"superuser_lane_mismatch:{access.lane}:{overlay.bus_lane}"
            )
        target = environ.get(access.ssh_target_env, "").strip()
        if not target:
            raise BrokerAdminError(f"ssh_target_unset:{access.ssh_target_env}")
        if target.startswith("-") or any(char.isspace() for char in target):
            raise BrokerAdminError("ssh_target_invalid")
        try:
            self._connection = AclConnection.from_mapping(
                {
                    "lane": access.lane,
                    "host": access.host,
                    "compose_project": access.compose_project,
                    "broker_container": access.broker_container,
                    "superuser_source_container": access.superuser_source_container,
                },
                target,
            )
        except DeclarationError:
            raise BrokerAdminError("ssh_target_invalid") from None
        self._overlay = overlay
        self._target = target
        self._timeout = access.script_timeout_seconds
        self._runner = runner

    def _script(self, lines: Sequence[str]) -> str:
        prologue = build_remote_script(self._connection, [])
        return (
            "\n".join([prologue.rstrip("\n"), _rpk_function(self._overlay), *lines])
            + "\n"
        )

    def _run(self, lines: Sequence[str]) -> str:
        try:
            return str(
                self._runner(self._target, self._script(lines), timeout=self._timeout)
            )
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            if isinstance(exc, RuntimeError) and _CREDENTIAL_MARKER in str(exc):
                raise BrokerAdminError("broker_credential_unresolved") from None
            raise BrokerAdminError(f"remote_failed:{type(exc).__name__}") from None

    # -- parsing --------------------------------------------------------------------------------

    @staticmethod
    def _sections(text: str) -> dict[str, list[str]]:
        """Output lines by marker: ``LIST``, ``TOPIC <name>``, ``GROUPS`` (the lines after each ``##``)."""
        sections: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in text.splitlines():
            if line.startswith("##"):
                current = sections.setdefault(line[2:].strip(), [])
            elif current is not None:
                current.append(line)
        return sections

    @staticmethod
    def _listed(lines: Sequence[str]) -> dict[str, int]:
        listed: dict[str, int] = {}
        for line in lines[1:] if lines and lines[0].startswith("NAME") else lines:
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                listed[fields[0]] = int(fields[1])
            elif fields:
                raise BrokerAdminError("rpk_output_unparsable:list")
        return listed

    @staticmethod
    def _watermarks(lines: Sequence[str]) -> list[tuple[int, int, int]]:
        """(partition, low, high) per row of ``rpk topic describe -p``."""
        rows: list[tuple[int, int, int]] = []
        for line in lines:
            fields = line.split()
            if not fields or fields[0] == "PARTITION":
                continue
            try:
                rows.append((int(fields[0]), int(fields[-2]), int(fields[-1])))
            except (ValueError, IndexError):
                raise BrokerAdminError("rpk_output_unparsable:describe") from None
        return rows

    @staticmethod
    def _groups(
        lines: Sequence[str], names: frozenset[str]
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """Groups by requested topic: with a live member on a partition, and with a committed offset."""
        members: dict[str, set[str]] = {}
        committed: dict[str, set[str]] = {}
        group = ""
        in_table = False
        for line in lines:
            fields = line.split()
            if not fields:
                in_table = False
            elif fields[0] == "GROUP" and len(fields) >= 2:
                group = fields[1]
                in_table = False
            elif fields[0] == "TOPIC" and "PARTITION" in fields:
                in_table = True
            elif in_table and fields[0] in names and len(fields) >= 6:
                if fields[2] != "-":
                    committed.setdefault(fields[0], set()).add(group)
                if len(fields) >= 9:
                    members.setdefault(fields[0], set()).add(group)
        return members, committed

    # -- the protocol ---------------------------------------------------------------------------

    def read_evidence(self, names: Sequence[str]) -> dict[str, ModelLiveTopicEvidence]:
        _valid_names(names)
        lines = ['echo "##LIST"', "R topic list"]
        for name in names:
            lines += [
                f'echo "##TOPIC {name}"',
                f"R topic describe {shlex.quote(name)} -p",
            ]
        lines += [
            'echo "##GROUPS"',
            "G=\"$(R group list | awk 'NR>1{print $2}')\"",
            '[ -z "$G" ] || R group describe $G',
        ]
        sections = self._sections(self._run(lines))
        if "LIST" not in sections:
            raise BrokerAdminError("rpk_output_unparsable:list")
        listed = self._listed(sections["LIST"])
        members, committed = self._groups(sections.get("GROUPS", []), frozenset(names))
        marks: dict[str, list[tuple[int, int, int]]] = {}
        for name in names:
            if name in listed:
                key = f"TOPIC {name}"
                if key not in sections:
                    raise BrokerAdminError("rpk_output_unparsable:describe")
                marks[name] = self._watermarks(sections[key])
        newest = self._newest_records(marks)
        evidence: dict[str, ModelLiveTopicEvidence] = {}
        for name in names:
            if name not in listed:
                evidence[name] = ModelLiveTopicEvidence(
                    exists=False,
                    partitions=0,
                    high_watermark_total=0,
                    retained_records=0,
                    consumer_groups=(),
                    offset_groups=(),
                    last_record_ms=None,
                )
                continue
            rows = marks[name]
            evidence[name] = ModelLiveTopicEvidence(
                exists=True,
                partitions=listed[name],
                high_watermark_total=sum(high for _p, _l, high in rows),
                retained_records=sum(max(high - low, 0) for _p, low, high in rows),
                consumer_groups=tuple(sorted(members.get(name, ()))),
                offset_groups=tuple(sorted(committed.get(name, ()))),
                last_record_ms=newest.get(name),
            )
        return evidence

    def _newest_records(
        self, marks: Mapping[str, Sequence[tuple[int, int, int]]]
    ) -> dict[str, int]:
        """Newest retained record's time (ms) per topic that holds records; one more remote call, only then."""
        wanted = [
            (name, partition, high)
            for name, rows in marks.items()
            for partition, low, high in rows
            if high > low
        ]
        if not wanted:
            return {}
        lines: list[str] = []
        for name, partition, high in wanted:
            lines += [
                f'echo "##REC {name} {partition}"',
                f"R topic consume {shlex.quote(name)} -p {partition} "
                f"-o {high - 1}:{high} -n 1 -f '%d\\n'",
            ]
        newest: dict[str, int] = {}
        for key, body in self._sections(self._run(lines)).items():
            parts = key.split()
            if len(parts) != 3 or parts[0] != "REC":
                continue
            stamps = [int(line) for line in body if line.strip().isdigit()]
            if stamps:
                newest[parts[1]] = max(newest.get(parts[1], 0), max(stamps))
        return newest

    def delete(self, names: Sequence[str]) -> dict[str, str]:
        if not names:
            raise BrokerAdminError("delete_names_empty")
        _valid_names(names)
        output = self._run(
            [f"R topic delete {' '.join(shlex.quote(name) for name in names)}"]
        )
        outcome: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] in names:
                outcome[fields[0]] = "" if fields[1] == "OK" else "_".join(fields[1:])
        return {name: outcome.get(name, "delete_unconfirmed") for name in names}


__all__ = ["DEV_LANE", "RpkSuperuserTopicAdmin"]
