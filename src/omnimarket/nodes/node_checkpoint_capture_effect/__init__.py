# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_checkpoint_capture_effect - journal a written session checkpoint as a content record.

The implementation is ``handlers/handler_checkpoint_capture.py``
(``HandlerCheckpointCapture``) with its models under ``models/``; tests are
``tests/test_checkpoint_capture_effect.py`` and
``tests/test_golden_chain_checkpoint_capture_effect.py``.

This package module deliberately imports nothing: the session checkpoint writer
loads the handler by file path, and any import here would run on that path.
"""
