# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Symbolic model key constants for tests (OMN-11943).

Single source of truth for model ID strings used in test fixtures and
assertions. Mirrors LogicalModelKey from omnibase_core.constants.constants_llm_refs
(OMN-11932); will be replaced by direct imports once the omnibase_core pin
is bumped to include that module.

Import these instead of bare string literals so typos become NameErrors
and renames require only one edit.
"""

from __future__ import annotations

# Registry model keys — mirror LogicalModelKey enum values
MODEL_QWEN3_CODER_30B: str = "qwen3-coder-30b"
MODEL_DEEPSEEK_R1_14B: str = "deepseek-r1-14b"
MODEL_DEEPSEEK_R1_32B: str = "deepseek-r1-32b"
MODEL_QWEN3_NEXT_80B: str = "qwen3-next-80b"
MODEL_LLAMA_3_3_70B_FREE: str = "llama-3.3-70b-free"
MODEL_CLAUDE_SONNET_4_6: str = "claude-sonnet-4-6"
MODEL_CLAUDE_OPUS_4_6: str = "claude-opus-4-6"

# Convenience aliases for the most common test pairings
MODEL_LOCAL_PRIMARY: str = MODEL_QWEN3_CODER_30B
MODEL_CLOUD_BASELINE: str = MODEL_CLAUDE_OPUS_4_6
MODEL_CLOUD_FAST: str = MODEL_CLAUDE_SONNET_4_6
MODEL_LOCAL_FAST: str = MODEL_DEEPSEEK_R1_14B
