# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_compact_package_read_effect - read an installed compaction out of a session transcript.

The implementation is ``handlers/handler_compact_package_read.py``
(``HandlerCompactPackageRead``) with its models under ``models/``; tests are
``tests/test_compact_package_read_effect.py`` and
``tests/test_golden_chain_compact_package_read_effect.py``.

This package module deliberately imports nothing: the onex plugin's content
capture loads the handler by file path, and any import here would run on that path.
"""
