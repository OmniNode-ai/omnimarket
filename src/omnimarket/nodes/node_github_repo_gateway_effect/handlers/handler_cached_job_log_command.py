# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Expose a strictly cache-only view through the existing SCM bus adapter."""

from collections.abc import Callable

import click

from omnimarket.models.scm import EnumScmNode, ModelScmVerb


def cached_job_log_command(
    factory: Callable[[ModelScmVerb], click.Command],
) -> click.Command:
    command = factory(
        ModelScmVerb(
            verb="job-log-cached",
            node=EnumScmNode.GATEWAY,
            operation="job_log_tail",
            replaces="host-shared completed job log",
        )
    )
    callback = command.callback
    assert callback is not None

    def cached(**fields: object) -> object:
        # Neither arguments nor environment can turn this verb into a live read.
        fields["cache_only"] = True
        return callback(**fields)

    command.callback = cached
    command.help = "A cached job's tail, window or step; a miss makes no GitHub call."
    return command
