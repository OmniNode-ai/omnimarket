# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared host ports have a public home outside node-private packages."""


def test_host_profile_derive_shared_handlers_are_importable() -> None:
    from omnimarket.handlers.handler_host_profile import HandlerHostProfileCompute
    from omnimarket.handlers.handler_host_shell import HandlerHostShell

    assert callable(HandlerHostProfileCompute().handle)
    assert callable(HandlerHostShell().measure)
