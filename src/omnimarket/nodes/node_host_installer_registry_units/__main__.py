# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_registry_units < request.json``: run one request, print the result."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)


def main() -> int:
    request = ModelRegistryUnitsRequest.model_validate_json(sys.stdin.read())
    with TemporaryDirectory(prefix="host-registry-unit-") as scratch:
        input_path = Path(scratch) / "request.json"
        input_path.write_text(request.model_dump_json(), encoding="utf-8")
        runtime = RuntimeLocal(
            workflow_path=Path(__file__).with_name("contract.yaml"),
            input_path=input_path,
            state_root=Path(scratch) / "runtime",
            backend_overrides={"event_bus": "inmemory"},
            timeout=600,
        )
        runtime.run()
        if runtime.handler_result is None:
            sys.stderr.write("host installer runtime returned no result\n")
            return 1
        result = ModelRegistryUnitsResult.model_validate(runtime.handler_result)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
