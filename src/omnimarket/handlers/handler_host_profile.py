# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node handler: derive a host's clones, venvs, prepush rows and liveness rows from its one profile."""

import yaml

from omnimarket.models.model_host_profile import (
    ModelDerivedClone,
    ModelDerivedPrepushRow,
    ModelDerivedVenv,
    ModelHostProfile,
    ModelHostProfileDerived,
    ModelHostProfileDeriveRequest,
    ModelProfilePrepush,
    ModelProfilePrepushTable,
    ModelProfileUnit,
)

VENV_DIRNAME = ".venv"


def parse_host_profile(text: str, host: str) -> ModelHostProfile:
    """Parse one host's profile out of a profiles YAML text keyed by host name."""
    data = yaml.safe_load(text)
    hosts = data["hosts"]
    if host not in hosts:
        raise KeyError(f"host {host!r} has no profile; declared: {sorted(hosts)}")
    return ModelHostProfile.model_validate({"host": host, **hosts[host]})


def prepush_row(
    host: str, prepush: ModelProfilePrepush, table: ModelProfilePrepushTable
) -> str:
    """One tab-separated pre-push host table row, in the existing table's column order."""
    cells = [
        host,
        prepush.role,
        prepush.hostname,
        prepush.ssh_target,
        str(prepush.cores),
        prepush.uv_abs_path,
        prepush.uv_min_version,
        prepush.workroot,
        prepush.slot_mode,
        str(table.slots),
        prepush.repos_denied,
        prepush.mode,
        *table.extra_columns,
        table.note,
    ]
    return "\t".join(cells)


def liveness_row(host: str, unit: ModelProfileUnit) -> dict[str, object]:
    """One automation-liveness overlay process entry, in the overlay's shape."""
    row: dict[str, object] = {
        "process_id": f"{host}/{unit.name}",
        "owner_repo": unit.owner_repo,
        "host": host,
        "trigger": {"kind": unit.trigger_kind, "native_id": unit.native_id},
        "expected_interval_seconds": unit.expected_interval_seconds,
    }
    if unit.max_runtime_seconds is not None:
        row["max_runtime_seconds"] = unit.max_runtime_seconds
    if unit.max_silence_seconds is not None:
        row["max_silence_seconds"] = unit.max_silence_seconds
    evidence: dict[str, str] = {
        "source": unit.evidence.source,
        "locator": unit.evidence.locator,
    }
    if unit.evidence.completion_record:
        evidence["completion_record"] = unit.evidence.completion_record
    row.update(
        {
            "alarm_deadline_seconds": unit.alarm_deadline_seconds,
            "emitter": "observer",
            "run_record": unit.run_record,
            "evidence": evidence,
            "real_work": unit.real_work,
            "demand": "none",
            "positive_control": {
                "kind": "class_drill",
                "ref": f"class-drill/{unit.trigger_kind}",
            },
            "state": unit.state,
        }
    )
    return row


def liveness_rows_text(profile: ModelHostProfile) -> str:
    """The text a host's liveness rows target holds."""
    rows = (
        [liveness_row(profile.host, u) for u in profile.liveness.units]
        if profile.liveness
        else []
    )
    return yaml.safe_dump(rows, sort_keys=False)


def prepush_target_files(profile: ModelHostProfile) -> dict[str, str]:
    """Resolved target path -> row line, for every pre-push table the host has a row in."""
    if profile.prepush is None:
        return {}
    base = profile.resolve(profile.prepush.rows_target).rstrip("/")
    return {
        f"{base}/{t.file}": prepush_row(profile.host, profile.prepush, t)
        for t in profile.prepush.tables
    }


def declared_units(profile: ModelHostProfile) -> tuple[str, ...]:
    """Every systemd unit the profile declares, sorted."""
    units = profile.liveness.units if profile.liveness else ()
    found = {u.native_id for u in units if u.trigger_kind.startswith("systemd")}
    return tuple(sorted(found | set(profile.systemd_units)))


class HandlerHostProfileCompute:
    def handle(self, request: ModelHostProfileDeriveRequest) -> ModelHostProfileDerived:
        profile = request.profile
        clones = tuple(
            ModelDerivedClone(repo=c.repo, ref=c.ref, path=profile.resolve(c.path))
            for c in profile.clones
        )
        clone_rows = tuple(
            ModelDerivedClone(repo=c.repo, ref=c.ref, path=c.path)
            for c in profile.clones
            if c.lab_run_row
        )
        venvs = tuple(
            ModelDerivedVenv(
                clone_path=profile.resolve(c.path),
                venv_path=f"{profile.resolve(c.path).rstrip('/')}/{VENV_DIRNAME}",
                python=profile.python,
            )
            for c in profile.clones
            if c.venv
        )
        prepush = profile.prepush
        prepush_rows = (
            tuple(
                ModelDerivedPrepushRow(
                    file=t.file,
                    target=f"{profile.resolve(prepush.rows_target).rstrip('/')}/{t.file}",
                    line=prepush_row(profile.host, prepush, t),
                )
                for t in prepush.tables
            )
            if prepush
            else ()
        )
        units = profile.liveness.units if profile.liveness else ()
        return ModelHostProfileDerived(
            host=profile.host,
            clone_rows=clone_rows,
            clones=clones,
            venvs=venvs,
            prepush_rows=prepush_rows,
            prepush_workroot=profile.prepush.workroot if profile.prepush else None,
            liveness_rows=tuple(liveness_row(profile.host, u) for u in units),
            liveness_rows_target=(
                profile.resolve(profile.liveness.rows_target)
                if profile.liveness
                else None
            ),
            liveness_rows_text=liveness_rows_text(profile),
            liveness_native_ids=tuple(u.native_id for u in units),
            systemd_units=declared_units(profile),
        )
