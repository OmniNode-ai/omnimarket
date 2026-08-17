"""Hook/skill event capture node.

Re-exports the handler so it carries wiring evidence: the OMN-10821
unimported-handler gate treats a contract-declared Handler class that no Python
module imports as unwired, because auto-wiring cannot bind what was never
imported and the failure only surfaces at dispatch time in production.
"""

from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    HandlerHookEventCapture,
)

__all__ = ["HandlerHookEventCapture"]
