# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared vocabulary for archiving event-bus topics to cold storage and back.

Two nodes use it: ``node_topic_archive_effect`` writes one compressed file per
topic, partition and UTC day with a manifest, and verifies each file by reading
it back and re-reading the source offsets; ``node_topic_archive_replay_effect``
reads a verified file back onto a replay topic. Neither imports the other; both
import this package.
"""
