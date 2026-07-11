"""EFFECT handler: run mypy on a code artifact and return typed diagnostics.

This is an EFFECT node — it owns the subprocess and filesystem I/O: it writes
the artifact to a temporary file (when given source text) and shells out to
``mypy``. ``mypy`` is already a repo dev tool, so no third-party runtime
dependency is added. mypy never mutates the checked target (it writes only to a
scratch cache under the temp dir), so the side effect is read-only.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from omnimarket.nodes.node_mypy_check_effect.models.model_mypy_check_request import (
    ModelMypyCheckRequest,
)
from omnimarket.nodes.node_mypy_check_effect.models.model_mypy_check_result import (
    ModelMypyCheckResult,
    ModelMypyDiagnostic,
)

# A mypy diagnostic line: "path:line:col: severity: message  [code]". The column
# is present because we pass --show-column-numbers; the trailing "  [code]" is
# present because we pass --show-error-codes (both optional in the regex so a
# stray line without them still parses defensively).
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>[a-z]+): "
    r"(?P<message>.*?)(?:  \[(?P<code>[a-z][a-z0-9-]*)\])?$"
)

# Fixed mypy flags. --follow-imports=silent + --ignore-missing-imports keep an
# isolated snippet from failing on unresolved third-party imports; the goal is
# to surface type errors in the generated code itself, not stub gaps.
_MYPY_FLAGS: tuple[str, ...] = (
    "--no-error-summary",
    "--show-column-numbers",
    "--show-error-codes",
    "--no-color-output",
    "--no-incremental",
    "--follow-imports=silent",
)


def _parse_diagnostics(output: str) -> tuple[ModelMypyDiagnostic, ...]:
    """Parse mypy stdout lines into typed diagnostics."""
    diagnostics: list[ModelMypyDiagnostic] = []
    for raw_line in output.splitlines():
        match = _DIAGNOSTIC_RE.match(raw_line.strip())
        if match is None:
            continue
        diagnostics.append(
            ModelMypyDiagnostic(
                line=int(match.group("line")),
                column=int(match.group("col")),
                severity=match.group("severity"),
                message=match.group("message").strip(),
                code=match.group("code"),
            )
        )
    return tuple(diagnostics)


def _run_mypy(
    target: Path, cache_dir: Path, ignore_missing_imports: bool
) -> tuple[bool, str]:
    """Run mypy on ``target``. Returns ``(mypy_available, stdout)``.

    ``mypy_available`` is False only when the mypy module cannot be launched at
    all (not installed) or exits with a usage/internal error before producing
    diagnostics — never merely because type errors were found (exit code 1).
    """
    args = [
        sys.executable,
        "-m",
        "mypy",
        *_MYPY_FLAGS,
        f"--cache-dir={cache_dir}",
    ]
    if ignore_missing_imports:
        args.append("--ignore-missing-imports")
    args.append(str(target))
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, ""
    # Exit 0 => clean, 1 => type errors reported on stdout, 2+ => usage/internal
    # error. Treat a 2+ exit that produced no diagnostics as "unavailable".
    if completed.returncode >= 2 and not completed.stdout.strip():
        return False, ""
    return True, completed.stdout


def check_artifact(request: ModelMypyCheckRequest) -> ModelMypyCheckResult:
    """Type-check a code artifact with mypy and return typed diagnostics."""
    with tempfile.TemporaryDirectory(prefix="mypy_check_") as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / ".mypy_cache"
        if request.source_text is not None:
            target = tmp_path / "artifact.py"
            target.write_text(request.source_text)
        elif request.path is not None:
            target = Path(request.path)
        else:  # pragma: no cover - the model validator guarantees one is set
            raise ValueError("no target provided")
        available, output = _run_mypy(target, cache_dir, request.ignore_missing_imports)

    if not available:
        return ModelMypyCheckResult(
            success=False,
            error_count=0,
            diagnostics=(),
            mypy_available=False,
        )

    diagnostics = _parse_diagnostics(output)
    error_count = sum(1 for diag in diagnostics if diag.severity == "error")
    return ModelMypyCheckResult(
        success=error_count == 0,
        error_count=error_count,
        diagnostics=diagnostics,
        mypy_available=True,
    )


class HandlerMypyCheck:
    """EFFECT: run mypy on a code artifact, returning typed diagnostics."""

    def handle(self, request: ModelMypyCheckRequest) -> ModelMypyCheckResult:
        return check_artifact(request)


__all__ = ["HandlerMypyCheck", "check_artifact"]
