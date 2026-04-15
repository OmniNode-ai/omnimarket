"""Entry-point resolution prober.

For every onex.nodes entry-point in installed packages:
1. Import the declared module
2. If a class name is present, verify it is accessible in the module

Entry-point values may use either form:
- Package form (omnimarket convention): ``module.path`` — no colon, imports the package
- Class form: ``module.path:ClassName`` — imports module and resolves the class

Both forms are valid. omnimarket uses package form exclusively, enforced by
``tests/test_all_node_entry_points_are_package_form.py``. Other packages may use
class form. Neither form is flagged as malformed by this prober.

Uses importlib.metadata.entry_points() to discover all onex.nodes entry-points.
"""

from __future__ import annotations

import importlib
import traceback
from importlib.metadata import entry_points

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    ModelHealthFinding,
    ModelSubsystemResult,
    aggregate_status,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


def probe_entry_points(
    entry_point_specs: list[dict[str, str]] | None = None,
) -> ModelSubsystemResult:
    """Check all onex.nodes entry-points import cleanly.

    Args:
        entry_point_specs: Optional explicit list of {"name": ..., "module": ..., "class": ...}
            dicts for testing. When None, discovers via importlib.metadata.
    """
    findings: list[ModelHealthFinding] = []

    specs = _discover_entry_points() if entry_point_specs is None else entry_point_specs

    for spec in specs:
        name = spec.get("name", "unknown")
        module = spec.get("module", "")
        cls = spec.get("class", "")
        ok, error = _try_import_handler(module, cls)
        if not ok:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.ENTRY_POINTS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=name,
                    message=f"Entry-point '{name}' failed to import: {error[:200]}",
                    evidence=f"importlib.import_module('{module}')",
                )
            )

    if not specs:
        findings.append(
            ModelHealthFinding(
                subsystem=EnumSubsystem.ENTRY_POINTS,
                severity=EnumHealthFindingSeverity.WARN,
                subject="discovery",
                message="No onex.nodes entry-points discovered — package may not be installed",
                evidence="importlib.metadata.entry_points(group='onex.nodes')",
            )
        )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.ENTRY_POINTS,
        status=status,
        check_count=len(specs),
        valid_zero=False,  # zero entry-points = nothing discovered = WARN-worthy
        findings=findings,
        evidence_source="importlib.metadata.entry_points(group='onex.nodes')",
    )


def _discover_entry_points() -> list[dict[str, str]]:
    specs = []
    eps = entry_points(group="onex.nodes")
    for ep in eps:
        # Both entry-point forms are valid:
        #   class form:   "module.path:ClassName"  (colon-separated)
        #   package form: "module.path"            (no colon — omnimarket convention)
        value = ep.value
        if ":" in value:
            module, cls = value.rsplit(":", 1)
        else:
            module, cls = value, ""
        specs.append({"name": ep.name, "module": module, "class": cls})
    return specs


def _try_import_handler(module: str, class_name: str) -> tuple[bool, str]:
    """Attempt to import module and access class. Returns (success, error_message)."""
    try:
        mod = importlib.import_module(module)
        if class_name:
            getattr(mod, class_name)
        return True, ""
    except Exception:
        return False, traceback.format_exc(limit=3)
