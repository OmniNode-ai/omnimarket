# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus binding with an ordered fallback: durable bus first, then the in-memory bus (OMN-20275).

Operator ruling 2026-10-27: when the durable bus is not working the runtime degrades, falls back to the
in-memory event bus with an alarm, and recovers on its own, so the whole system does not crash.
"""
