# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_roll_failure_capture_effect (OMN-24013, five whys plan root R2).

Consumes the deploy agent's dev-roll incident and manifest events and writes the
roll's typed terminal record, with the failed container's captured log, through
an injected artifact store.
"""
