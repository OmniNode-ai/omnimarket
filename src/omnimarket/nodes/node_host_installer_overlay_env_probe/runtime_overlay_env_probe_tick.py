# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the overlay environment probe on a host, the unit the host installer renders (OMN-26549).

The unit passes ``--omni-home`` and ``--state-dir`` and sets ``LAB_WORK_HOST_NAME`` and ``ONEX_SKILL_OVERLAY_ROOTS``.
The tick probes the node overlays found under those roots for this host: every node with an ``overlay.yaml`` is a
node the host may run, and the names set on the host are the names of this process's environment whose value is not
blank (``set_names_in``). A line is printed per declared name and states only ``set`` or ``unset``; a value is never
read into a request, a line or ``last-run.json``. The exit code is the probe's (0 on success, 65 when an overlay is
unreadable); 3 when the host or the overlay roots are not named.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.handlers.handler_overlay_env_probe import (
    set_names_in,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelOverlayEnvProbeRequest,
    ModelOverlayEnvProbeResult,
    ModelProbeHost,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.node import (
    NodeHostInstallerOverlayEnvProbe,
)

NODE_NAME = "node_host_installer_overlay_env_probe"
HOST_ENV = "LAB_WORK_HOST_NAME"
EXIT_INDETERMINATE = 3


def overlay_roots(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The roots of ``ONEX_SKILL_OVERLAY_ROOTS`` in order."""
    return tuple(
        root for root in environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if root
    )


def nodes_with_overlays(roots: tuple[str, ...]) -> tuple[str, ...]:
    """The nodes whose ``<root>/<node>/overlay.yaml`` exists under any root, sorted, each once."""
    found: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if (child / OVERLAY_FILE_NAME).is_file():
                found.add(child.name)
    return tuple(sorted(found))


def build_request(environ: Mapping[str, str]) -> ModelOverlayEnvProbeRequest:
    """The probe request for this host: its name, the nodes whose overlays are present, the names set here."""
    host = environ.get(HOST_ENV, "").strip()
    roots = overlay_roots(environ)
    if not host:
        raise ValueError(f"{HOST_ENV} names no host")
    if not roots:
        raise ValueError(f"{OVERLAY_ROOTS_ENV} names no overlay root")
    return ModelOverlayEnvProbeRequest(
        overlay_roots=roots,
        hosts=(
            ModelProbeHost(
                host_name=host,
                nodes=nodes_with_overlays(roots),
                set_names=set_names_in(environ),
            ),
        ),
    )


def lines(result: ModelOverlayEnvProbeResult) -> list[str]:
    """One line per probe row, then one per node the host is unavailable for. Names and states only."""
    out = [
        f"PROBE-ROW host={row.host_name} node={row.node} name={row.name} state={row.state} source={row.source}"
        for row in result.rows
    ]
    out.extend(
        f"PROBE-UNAVAILABLE host={item.host_name} node={item.node} unset={','.join(item.unset_names)}"
        for item in result.unavailable
    )
    unset = sum(1 for row in result.rows if row.state == "unset")
    out.append(
        f"PROBE-SUMMARY status={result.status} rows={len(result.rows)} unset={unset} unavailable={len(result.unavailable)}"
    )
    return out


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE_NAME}.tick", description=__doc__)
    parser.add_argument("--omni-home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    ran_at = datetime.now(UTC).isoformat()
    try:
        request = build_request(os.environ)
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps(
                {
                    "ran_at": ran_at,
                    "exit_code": EXIT_INDETERMINATE,
                    "indeterminate": str(exc),
                }
            ),
        )
        return EXIT_INDETERMINATE
    result = NodeHostInstallerOverlayEnvProbe().handle(request)
    sys.stdout.write("\n".join(lines(result)) + "\n")
    if result.status != "success":
        sys.stderr.write(f"REFUSED: {result.reason}\n")
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": result.exit_code,
                "result": result.model_dump(mode="json"),
            }
        ),
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
