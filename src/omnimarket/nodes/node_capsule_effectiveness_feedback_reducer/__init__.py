# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_capsule_effectiveness_feedback_reducer (OMN-12845 / M5).

The M5 feedback edge: consume a scored runtime ROI row and write its
effectiveness onto the durable M2 capsule store (for controlled-intervention
rows) so M3 selection re-ranks on the live-updated score; record an
observational row only as a hypothesis (never a measured claim).
"""

from __future__ import annotations
