# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-only host reconciliation decisions, without filesystem or process I/O."""

from omnimarket.models.model_host_reconcile import (
    ModelHostReconcileDecisions,
    ModelHostReconcileEvaluateRequest,
    ModelSurfaceDecision,
    ModelSurfaceFact,
)


class HandlerHostReconcileCompute:
    def handle(
        self, request: ModelHostReconcileEvaluateRequest
    ) -> ModelHostReconcileDecisions:
        surfaces = []
        for fact in request.facts:
            verdict, detail = self._verdict(fact, request.workspace_root)
            surfaces.append(
                ModelSurfaceDecision(
                    surface=fact.surface,
                    verdict=verdict,
                    dispatch_premise=self._premise(fact.surface),
                    remedy=""
                    if verdict in {"MOVED", "ALREADY_AT_TARGET"}
                    else self._remedy(fact.surface, verdict, request),
                    owner=fact.owner,
                    detail=detail,
                )
            )
        failures = [
            s for s in surfaces if s.verdict not in {"MOVED", "ALREADY_AT_TARGET"}
        ]
        dispatch_failures = [s for s in failures if s.dispatch_premise]
        stamp_floor = request.mode == "repair" and not dispatch_failures
        lines = []
        if failures:
            if stamp_floor:
                lines.append(
                    "The dispatch premise IS proven; the unrelated failures do not block onex delegate."
                )
            else:
                lines.append(
                    "The floor marker was NOT stamped; the previous proven floor is retained."
                )
                for failure in dispatch_failures:
                    lines.append(
                        f"blocks onex delegate: {failure.surface}: {failure.verdict}"
                    )
            lines.append(
                f"VERDICT: FAILED — {len(failures)} surface(s) could not be proven at target."
            )
            lines.append(f"  receipt: {request.receipt_path}")
            for surface in failures:
                lines.append(
                    f"  {surface.surface}: {surface.verdict} — {surface.detail}"
                )
                lines.append(f"    clears with: {surface.remedy}")
        else:
            lines.append(
                "VERDICT: IN_SYNC (check mode; nothing mutated, floor untouched)"
                if request.mode == "check"
                else "VERDICT: IN_SYNC — every surface proven at target."
            )
        return ModelHostReconcileDecisions(
            surfaces=tuple(surfaces),
            failures=len(failures),
            dispatch_premise_failures=len(dispatch_failures),
            exit_code=2 if failures else 0,
            stamp_floor=stamp_floor,
            verdict_lines=tuple(lines),
        )

    @staticmethod
    def _verdict(fact: ModelSurfaceFact, workspace_root: str) -> tuple[str, str]:
        if fact.kind == "movement":
            before, after, target = fact.before, fact.after, fact.target
            if not after:
                return (
                    "INDETERMINATE",
                    "post-reconcile state is unreadable; refusing to assume it is correct",
                )
            if not target:
                return (
                    "INDETERMINATE",
                    "no target to compare against; a surface with no target cannot be attested",
                )
            if after != target:
                return "DID_NOT_MOVE", f"observed {after} but target is {target}" + (
                    f" (unchanged from {before})" if before == after else ""
                )
            if before == after:
                return "ALREADY_AT_TARGET", f"already at {target}"
            return "MOVED", f"{before or '<absent>'} -> {after}"
        if fact.kind == "unhealthy":
            return "UNHEALTHY", fact.reason
        if fact.kind == "uncovered":
            if fact.layer == "clone":
                return (
                    "UNCOVERED",
                    f"no clone reconciler at {fact.delegate} — the deploy-source clones on this host are reconciled by nobody",
                )
            return (
                "UNCOVERED",
                f"no venv reconciler at {fact.delegate} — the installed layers on this host are reconciled by nobody",
            )
        if fact.kind == "indeterminate":
            return "INDETERMINATE", fact.detail
        if fact.kind == "gate_purity":
            if fact.provider:
                return "IMPURE", (
                    f"{fact.provider} is installed in {fact.gate_venv}, which "
                    f"is lock-governed only — every `uv run pytest` "
                    f"in {workspace_root}/omnibase_infra is refused by the "
                    f"OMN-15620 purity gate while it is there; the provider "
                    f"layer belongs in {fact.dispatch_venv} (OMN-17819)"
                )
            return (
                "ALREADY_AT_TARGET",
                f"no undeclared omnimarket provider in {fact.gate_venv}",
            )
        if fact.kind == "path_shadow":
            if fact.is_symlink and fact.points_to_wrapper:
                return "ALREADY_AT_TARGET", (
                    f"{fact.shadow} is a symlink to the canonical wrapper "
                    f"({fact.wrapper_resolved}) — not a shadow"
                )
            found = (
                f"a symlink to {fact.resolved}" if fact.is_symlink else "a regular file"
            )
            return "SHADOWED", (
                f"{fact.shadow} exists ({found}) and outranks {fact.wrapper} "
                f"for every non-interactive onex invocation (the "
                f"interactive-shell alias never covers those) — "
                f"fix: uv tool uninstall omnibase-core"
            )
        if fact.drifted:
            return "DRIFT", (
                f"the installed canonical-clone guard at {fact.live_dir} "
                f"differs from the tracked source at {fact.source_dir}: {' '.join(fact.drifted)} "
                f"-- a guard fix merged to dev is not in force on "
                f"this host until it is installed (OMN-17291)"
            )
        return (
            "ALREADY_AT_TARGET",
            f"{fact.checked} installed guard script(s) in {fact.live_dir} are byte-identical to {fact.source_dir}",
        )

    @staticmethod
    def _premise(surface: str) -> bool:
        return surface != "venv:gate-purity" and (
            surface.startswith("venv:")
            or surface
            in {
                "clone:omnibase_infra",
                "clone:omnimarket",
                "clone-surface",
                "venv-surface",
            }
        )

    @staticmethod
    def _remedy(
        surface: str, verdict: str, request: ModelHostReconcileEvaluateRequest
    ) -> str:
        rerun, root, scripts = (
            request.rerun_command,
            request.workspace_root,
            request.scripts_dir,
        )
        if surface.startswith("clone:"):
            return (
                f"apply the repair named in the detail, then {rerun}"
                if verdict == "UNHEALTHY"
                else f"bash {root}/omniclaude/scripts/converge-canonical-clone.sh {surface[6:]} --execute, then {rerun}"
            )
        if surface.startswith("venv:"):
            return f"bash {scripts}/reconcile-workspace-venvs.sh --omni-home {root}, then {rerun}"
        if surface in {"clone-surface", "venv-surface"}:
            return f"restore the missing delegate named in the detail, then {rerun}"
        if surface == "onex-path-shadow":
            return f"uv tool uninstall omnibase-core, then {rerun}"
        if surface == "canonical-guard":
            return (
                f"bash {scripts}/install-canonical-clone-git-hooks.sh "
                f"(readback), then bash {scripts}/install-canonical-clone-git-hooks.sh "
                f"--apply <the clones the readback lists as ok>, then {rerun}"
            )
        return rerun
