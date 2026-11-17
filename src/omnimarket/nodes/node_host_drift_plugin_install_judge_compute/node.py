# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_plugin_install_judge_compute."""

from pathlib import Path

from .handlers.handler_plugin_install_judge import HandlerHostDriftPluginInstallJudge


class NodeHostDriftPluginInstallJudgeCompute(HandlerHostDriftPluginInstallJudge):
    """Contract-defined judgement of one plugin repair attempt."""

    contract_path = Path(__file__).with_name("contract.yaml")
