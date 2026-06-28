# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_slack_publish_effect (OMN-13723).

Usage:
    python -m omnimarket.nodes.node_slack_publish_effect --consumer

Starts the Kafka consumer loop when --consumer is passed.
"""

from __future__ import annotations

import sys


def main() -> None:
    if "--consumer" in sys.argv:
        from omnimarket.nodes.node_slack_publish_effect.consumer import (
            main as consumer_main,
        )

        consumer_main()
    else:
        import argparse

        parser = argparse.ArgumentParser(
            description="node_slack_publish_effect — generic Slack EFFECT node."
        )
        parser.add_argument(
            "--consumer",
            action="store_true",
            help="Start the Kafka consumer loop.",
        )
        parser.add_argument(
            "--import-check",
            action="store_true",
            help="Verify all imports resolve and exit 0.",
        )
        args = parser.parse_args()

        if args.import_check:
            from omnimarket.nodes.node_slack_publish_effect.handlers.handler_slack_publish_effect import (  # noqa: F401
                HandlerSlackPublishEffect,
            )
            from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (  # noqa: F401
                ModelSlackPublish,
                ModelSlackPublishResult,
            )

            sys.stdout.write("node_slack_publish_effect: import check OK\n")
            sys.exit(0)

        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
