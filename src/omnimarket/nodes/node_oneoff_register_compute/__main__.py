# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the one-off register and print its JSON reading (``python -m omnimarket.nodes.node_oneoff_register_compute``)."""

from omnimarket.nodes.node_oneoff_register_compute.runtime_oneoff_register import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
