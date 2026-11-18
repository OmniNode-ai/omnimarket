# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the secret store effect.

Without arguments, read one ModelSecretStoreRequest as JSON on stdin and write
the serialized result to stdout. For a host env file, use
``write-env-file --overlay-key <section.key> --folder <folder>``. The installed
overlay supplies the file's destination and permitted secret names.
Exit 0 on success and 3 on a refused or failed operation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.models.secret_store.model_secret_store_request import (
    ModelSecretStoreRequest,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_env_file import (
    HandlerSecretEnvFile,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_env_file import (
    ModelSecretEnvFileRequest,
)

_EXIT_NOT_OK = 3


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        write = commands.add_parser("write-env-file")
        write.add_argument("--overlay-key", required=True)
        write.add_argument("--folder", required=True)
        selected = parser.parse_args(args)
        try:
            env_request = ModelSecretEnvFileRequest(
                overlay_key=selected.overlay_key, folder=selected.folder
            )
        except ValidationError:
            sys.stderr.write("invalid env-file request\n")
            return _EXIT_NOT_OK
        env_result = asyncio.run(HandlerSecretEnvFile().handle(env_request))
        sys.stdout.write(env_result.model_dump_json() + "\n")
        return 0 if env_result.ok else _EXIT_NOT_OK

    request = ModelSecretStoreRequest.model_validate_json(sys.stdin.read())
    result = asyncio.run(HandlerSecretStore().handle(request))
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.ok else _EXIT_NOT_OK


if __name__ == "__main__":
    raise SystemExit(main())
