# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin shell adapter: dispatch the receipt check through its contract and bus."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_request import (
    ModelPrHandoffDodReceiptRequest,
)
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_result import (
    ModelPrHandoffDodReceiptResult,
)

_CONTRACT = Path(__file__).with_name("contract.yaml")


async def _run(request: ModelPrHandoffDodReceiptRequest) -> int:
    """Use the same runtime path as onex node; never call the handler directly."""
    with TemporaryDirectory(prefix="pr-handoff-dod-") as scratch:
        root = Path(scratch)
        input_path = root / "request.json"
        input_path.write_text(request.model_dump_json(), encoding="utf-8")
        timeout_ms = contract_section(_CONTRACT, "descriptor", "timeout_ms")
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ValueError("the receipt contract requires a positive timeout_ms")
        runtime = RuntimeLocal(
            workflow_path=_CONTRACT,
            input_path=input_path,
            state_root=root / "state",
            timeout=(timeout_ms + 999) // 1000,
        )
        await runtime.run_async()
        result = runtime.handler_result
        if not isinstance(result, ModelPrHandoffDodReceiptResult):
            sys.stderr.write(f"receipt check runtime failed: {runtime.last_error}\n")
            return runtime.exit_code or 1
        sys.stdout.write(result.line + "\n")
        return result.code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket")
    parser.add_argument("head")
    parser.add_argument("--receipts-root")
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            ModelPrHandoffDodReceiptRequest(
                ticket=args.ticket, head=args.head, receipts_root=args.receipts_root
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
