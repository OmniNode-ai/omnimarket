# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the parity receipt writer tests: the recorded receipt rows and a configured world on disk."""

from __future__ import annotations

import stat
from pathlib import Path

from omnimarket.nodes.node_work_ledger_parity_receipt_effect.models.model_parity_receipt import (
    ModelParityReceiptRequest,
)

NOW = "2026-10-05T00:40:00Z"
DAY = "2026-10-04"
# The receipt the old ledger-publish tests recorded (OMN-20536), clean.
CLEAN = (
    "2026-10-05T00:40:07Z | STATUS | lane=work-ledger-parity | actor=script:work-ledger-parity | "
    "model=none | day=2026-10-04 | file_rows=10 | projection_rows=10 | missing=0 | extra=0 | "
    "state_mismatches=0 | unexplained=0 | backfilled=0 | exact=yes | "
    "Work-ledger parity receipt for UTC day 2026-10-04"
)
UNHEALTHY = CLEAN.replace("missing=0", "missing=1").replace("exact=yes", "exact=no")
UNEXPLAINED = CLEAN.replace("unexplained=0", "unexplained=2").replace(
    "exact=yes", "exact=no"
)
FRESH = CLEAN.replace("2026-10-05T00:40:07Z", "2026-10-05T01:40:07Z")
LEDGER_HEAD = "# ledger\n2026-10-04T23:00:00Z | STATUS | lane=a | x\n"


def write_world(
    tmp_path: Path, ledger_rows: list[str], receipt_out: str = FRESH, parity_rc: int = 0
) -> tuple[Path, dict[str, str]]:
    """A ledger holding the rows, the overlay and env file of the DSN, and an executable stub for the parity interpreter.

    Returns the ledger path and the request fields that point at them."""
    ledger = tmp_path / "ledger" / "rows.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        LEDGER_HEAD + "".join(f"{row}\n" for row in ledger_rows), encoding="utf-8"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        'ssh_host_env: LAB_HOST\ndatabase: "analytics"\ndb_user: postgres\n',
        encoding="utf-8",
    )
    env_file = tmp_path / "lab.env"
    env_file.write_text(
        "export LAB_HOST=someone@db.lab.example\nPOSTGRES_PASSWORD='s3cret'\n",
        encoding="utf-8",
    )
    python = tmp_path / "parity-python"
    python.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/parity-argv.txt"\n'
        f'printf "%s\\n" "$WL_DSN" > "{tmp_path}/parity-dsn.txt"\n'
        f"cat <<'EOT'\n{receipt_out}\nEOT\nexit {parity_rc}\n",
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return ledger, {
        "parity_python": str(python),
        "dsn_overlay_path": str(overlay),
        "dsn_env_file": str(env_file),
        "pg_port": "5436",
    }


def request_for(
    tmp_path: Path, ledger: Path, fields: dict[str, str], **extra: object
) -> ModelParityReceiptRequest:
    return ModelParityReceiptRequest.model_validate(
        {
            "registry_root": str(tmp_path / "registry"),
            "ledger_path": str(ledger),
            "now": NOW,
            **fields,
            **extra,
        }
    )
