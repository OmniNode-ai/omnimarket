# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a scheduled job-log check through the deployed SCM gateway."""

from omnimarket.nodes.node_github_job_log_probe_effect.handlers.handler_job_log_probe import (
    HandlerJobLogProbe,
)


class NodeGithubJobLogProbeEffect(HandlerJobLogProbe):
    """The job-log probe's runtime node."""


__all__ = ["NodeGithubJobLogProbeEffect"]
