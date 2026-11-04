# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the cloud node's typed snapshot on the publisher or a declared reachable host."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from omnimarket.models.cloud_session_placement import ModelCloudSessionPlacementResult
from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorCloudSource,
)


def read_cloud_account_source(
    home: Path,
    source: ModelFactoryFloorCloudSource,
    command: tuple[str, ...] = (),
) -> tuple[tuple[ModelFloorCloudAccount, ...] | None, str]:
    """A command override is an argv prefix ending in ``onex node`` (SSH may precede it).

    The request carries no publisher-local filename: the existing node resolves its
    placement directory on the host that owns the state. Neither subprocess stream
    nor exception text is forwarded to the public record.
    """
    # Stay in the publisher's installed revision, including a branch installation.
    # A registry wrapper can select a different dispatch environment with an older node.
    prefix = command or (str(Path(sys.executable).with_name("onex")), "node")
    argv = [
        *prefix,
        source.node_name,
        "--input",
        "/dev/stdin",
        "--output",
        "receipt",
        "--timeout",
        str(source.timeout_seconds),
    ]
    if not command:
        argv += ["--state-root", str(home / ".onex_state")]
    try:
        result = subprocess.run(
            argv,
            input=json.dumps({"action": source.action}),
            text=True,
            capture_output=True,
            check=False,
            timeout=source.timeout_seconds + 10,
            cwd=home,
        )
        if result.returncode:
            return (
                None,
                f"cloud account node could not be read (exit {result.returncode})",
            )
        receipt = json.loads(result.stdout)
        if receipt.get("status") != "success":
            return None, "cloud account node returned no successful snapshot"
        snapshot = ModelCloudSessionPlacementResult.model_validate(receipt["result"])
        if (
            snapshot.action != source.action
            or snapshot.error
            or snapshot.floor_accounts is None
        ):
            return None, "cloud account node returned no readable snapshot"
        return snapshot.floor_accounts, ""
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.TimeoutExpired,
    ) as exc:
        return None, f"cloud account node could not be read ({type(exc).__name__})"
