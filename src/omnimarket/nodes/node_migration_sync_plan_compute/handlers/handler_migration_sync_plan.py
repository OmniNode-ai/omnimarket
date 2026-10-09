# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide a migration-sync run from an inventory, as sync-node-migrations.sh decided it (OMN-20687).

Pure: no filesystem, no clock, no environment. The effect node reads the source
tree, the vendored tree and the manifest; this node answers what the old script
answered for them. Its messages are the old script's, verbatim, on the same streams.

Rules, in the order the script applied them:

* Every source file is expected in the vendored tree. In check mode a missing or
  different copy is drift, unless the copy exists and the manifest declares it (the
  declared bytes are frozen history). In write mode that case is kept; a copy that
  is missing but declared is restored from the source only when the source matches
  the manifest's checksum, and is drift otherwise.
* Every manifest row under ``nodes/`` must exist in the vendored tree once the
  copies are done.
* In write mode drift ends the run before any removal, with exit 1.
* A vendored file with no source is stale: drift in check mode, removed in write
  mode, unless the manifest declares it.
"""

from __future__ import annotations

from omnimarket.models.migration_sync import (
    ModelMigrationSyncAction,
    ModelMigrationSyncLine,
)

from ..models.model_migration_sync_plan import (
    MigrationSyncVerdict,
    ModelMigrationSyncPlanRequest,
    ModelMigrationSyncPlanResult,
)

PREFIX = "[sync-node-migrations]"
_MANIFEST_NODES_PREFIX = "nodes/"


class HandlerMigrationSyncPlan:
    """Plan one vendoring run: which node migrations to copy, keep or remove."""

    def handle(
        self, request: ModelMigrationSyncPlanRequest
    ) -> ModelMigrationSyncPlanResult:
        inventory = request.inventory
        check = request.mode == "check"
        lines: list[ModelMigrationSyncLine] = []

        def out(text: str) -> None:
            lines.append(ModelMigrationSyncLine(stream="out", text=text))

        def err(text: str) -> None:
            lines.append(ModelMigrationSyncLine(stream="err", text=text))

        def finish(
            exit_code: int,
            verdict: MigrationSyncVerdict,
            *,
            drift: bool = False,
            actions: list[ModelMigrationSyncAction] | None = None,
            changed: int = 0,
        ) -> ModelMigrationSyncPlanResult:
            return ModelMigrationSyncPlanResult(
                exit_code=exit_code,
                verdict=verdict,
                drift=drift,
                lines=tuple(lines),
                actions=tuple(actions or ()),
                changed_count=changed,
            )

        if inventory.failure:
            err(f"{PREFIX} ERROR: {inventory.failure}")
            return finish(3, "inventory_failed")

        if not inventory.resolved:
            err(f"{PREFIX} ERROR: could not resolve omnimarket source tree.")
            err(
                "  Set OMNIMARKET_SRC=<omnimarket repo root>, or OMNI_HOME=<registry root>, "
                "or pip install omnimarket."
            )
            if check and request.skip_unresolvable:
                err(
                    f"{PREFIX} SYNC_NODE_MIGRATIONS_SKIP_UNRESOLVABLE=1 "
                    "— skipping unresolvable-source error."
                )
                return finish(0, "skipped_unresolvable")
            return finish(2, "source_unresolvable")

        out(f"{PREFIX} omnimarket nodes: {inventory.nodes_dir}")
        out(f"{PREFIX} vendoring into:   {inventory.dest_root}")
        out(f"{PREFIX} selection:        all marketplace node migrations")

        declared: dict[str, list[str]] = {}
        for row in inventory.manifest_rows:
            declared.setdefault(row.artifact_path, []).append(row.declared_sha256)

        def legacy_declared(relative_path: str) -> bool:
            return inventory.manifest_present and (
                _MANIFEST_NODES_PREFIX + relative_path in declared
            )

        vendored = {f.relative_path: f.sha256 for f in inventory.vendored_files}
        expected: set[str] = set()
        actions: list[ModelMigrationSyncAction] = []
        drift = False
        changed = 0

        for source in inventory.source_files:
            key = source.relative_path
            expected.add(key)
            differs = key not in vendored or vendored[key] != source.sha256
            if not differs:
                continue
            exists = key in vendored
            if check:
                if exists and legacy_declared(key):
                    out(f"{PREFIX} legacy-declared (OMN-16705), not rewritten: {key}")
                else:
                    err(f"{PREFIX} DRIFT: {key}")
                    drift = True
            elif exists and legacy_declared(key):
                out(f"{PREFIX}   kept legacy-declared (OMN-16705) {key}")
            else:
                if legacy_declared(key):
                    declared_sha = "\n".join(declared[_MANIFEST_NODES_PREFIX + key])
                    if source.sha256 != declared_sha:
                        err(
                            f"{PREFIX} DRIFT: declared migration checksum mismatch {key}"
                        )
                        drift = True
                        continue
                actions.append(
                    ModelMigrationSyncAction(
                        kind="copy", relative_path=key, source_path=source.source_path
                    )
                )
                vendored[key] = source.sha256
                out(f"{PREFIX}   vendored {key}")
                changed += 1

        if inventory.manifest_present:
            for row in inventory.manifest_rows:
                if not row.artifact_path.startswith(_MANIFEST_NODES_PREFIX):
                    continue
                declared_file = row.artifact_path[len(_MANIFEST_NODES_PREFIX) :]
                if declared_file not in vendored:
                    err(f"{PREFIX} DRIFT: missing declared migration {declared_file}")
                    drift = True

        if not check and drift:
            err(
                f"{PREFIX} declared node migration history is incomplete; "
                "restore the missing checked-in files."
            )
            return finish(1, "drift", drift=True, actions=actions, changed=changed)

        for extra in sorted(vendored):
            if extra in expected:
                continue
            if check:
                if legacy_declared(extra):
                    out(f"{PREFIX} legacy-declared (OMN-15717), not stale: {extra}")
                else:
                    err(f"{PREFIX} DRIFT: stale vendored migration {extra}")
                    drift = True
            elif legacy_declared(extra):
                out(f"{PREFIX}   kept legacy-declared (OMN-15717) {extra}")
            else:
                actions.append(
                    ModelMigrationSyncAction(kind="remove", relative_path=extra)
                )
                out(f"{PREFIX}   removed stale {extra}")
                changed += 1

        if check:
            if drift:
                err(
                    f"{PREFIX} node migration vendor tree is OUT OF SYNC with omnimarket."
                )
                err("  Run: scripts/sync-node-migrations.sh  then commit the diff.")
                return finish(1, "drift", drift=True)
            out(f"{PREFIX} check: in sync.")
            return finish(0, "in_sync")

        out(f"{PREFIX} done: {changed} file(s) updated.")
        return finish(0, "updated", actions=actions, changed=changed)
