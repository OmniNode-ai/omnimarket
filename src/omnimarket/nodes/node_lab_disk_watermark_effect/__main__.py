# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_disk_watermark_effect run --host <name>``, or ``schedule <verb>``."""

import sys

from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
