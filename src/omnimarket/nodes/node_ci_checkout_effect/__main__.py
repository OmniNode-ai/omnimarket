# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ci_checkout_effect serve|unit`` (OMN-24287)."""

from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_serve import (
    main,
)

if __name__ == "__main__":
    main()
