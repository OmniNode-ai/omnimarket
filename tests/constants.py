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
# New models added in OMN-12492 (2026-05-30 refresh)
MODEL_DS_V4_FLASH: str = "ds-v4-flash"
# OMN-12937: retargeted from gemini-2.0-flash (free tier 429) to gemini-2.5-flash-lite.
MODEL_GEMINI_2_5_FLASH_LITE: str = "gemini-2.5-flash-lite"
MODEL_OPENROUTER_QWEN3_CODER_480B: str = "openrouter-qwen3-coder-480b"
MODEL_QWEN3_35B_A3B: str = "Qwen3.6-35B-A3B"
MODEL_QWEN3_27B_MTP: str = "Qwen3.6-27B-MTP-IQ4_XS.gguf"

# Convenience aliases for the most common test pairings
MODEL_LOCAL_PRIMARY: str = MODEL_QWEN3_CODER_30B
MODEL_CLOUD_BASELINE: str = MODEL_CLAUDE_OPUS_4_6
MODEL_CLOUD_FAST: str = MODEL_CLAUDE_SONNET_4_6
MODEL_LOCAL_FAST: str = MODEL_DEEPSEEK_R1_14B
MODEL_LOCAL_DS: str = MODEL_DS_V4_FLASH
MODEL_CLOUD_GEMINI: str = MODEL_GEMINI_2_5_FLASH_LITE
