# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public local-evidence composition port; never republish onto a lane bus."""

from __future__ import annotations

import logging

from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

logger = logging.getLogger(__name__)


class LocalEvidenceNoRepublishPublisher:
    """The customer profile's explicit no-transport adapter; False means not published."""

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        logger.debug(
            "local delegation evidence: snapshot delta for %s not republished (the local evidence store backs no lane projection exposure)",
            message.topic,
        )
        return False
