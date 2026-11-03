# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prepare the phase session's files and record its configured tool set."""

from __future__ import annotations

import json

from ..models.model_morning_phase_session_files import (
    ModelMorningPhaseSessionFiles,
    ModelMorningPhaseSessionFilesRequest,
)
from ..tools.phases import phase_tools
from .handler_morning_phase_tools import (
    mcp_config_for,
    refuse_unless_morning_server,
    transport_argv,
)

__all__ = ["HandlerMorningPhaseSessionFiles", "ModelMorningPhaseSessionFilesRequest"]


class HandlerMorningPhaseSessionFiles:
    """Keep session configuration and its receipt together in the morning node."""

    def handle(
        self, request: ModelMorningPhaseSessionFilesRequest
    ) -> ModelMorningPhaseSessionFiles:
        context = request.context
        label = context.phase_label
        tools = phase_tools(label)
        run_dir = context.state_dir
        if run_dir is None:
            raise ValueError(
                "the morning gateway must prepare the phase state directory"
            )
        context_file = run_dir / f"{label}.context.json"
        context_file.write_text(context.model_dump_json(indent=2), encoding="utf-8")
        config = mcp_config_for(context_file, run_dir / f"{label}.calls.jsonl")
        refuse_unless_morning_server(config)
        config_file = run_dir / f"{label}.mcp.json"
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
        schema_file = run_dir / f"{label}.schema.json"
        schema_file.write_text(json.dumps(request.schema_definition, sort_keys=True))
        # A file per phase avoids lost rows when phases start concurrently. This
        # records configuration even when the transport subsequently refuses.
        root = request.state_root or context.root.joinpath(
            ".onex_state", "morning-workflows"
        )
        receipt_dir = root / "runs" / context.date / request.correlation_id.hex
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / f"{label}.tools.json").write_text(
            json.dumps(
                {
                    "correlation_id": str(request.correlation_id),
                    "date": context.date,
                    "phase": label,
                    "phase_tools": [tool.name for tool in tools],
                    "built_in_tools_off": True,
                    "transport": "--tools-only",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return ModelMorningPhaseSessionFiles(
            run_dir=run_dir,
            config_file=config_file,
            schema_file=schema_file,
            argv=transport_argv(context.model, config_file, schema_file),
        )
