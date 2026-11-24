# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live Valkey probes are collected only when the lab run names the store it probes (never skipped)."""

from __future__ import annotations

import os

collect_ignore_glob = [] if os.environ.get("ONEX_VALKEY_ACL_PROBE") else ["test_*.py"]
