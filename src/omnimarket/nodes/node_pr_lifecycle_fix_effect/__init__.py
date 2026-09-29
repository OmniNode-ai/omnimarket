# OMN-13990: the contract's handler_routing points the runtime at
# HandlerPrLifecycleFixRuntime (live OCC adapters on the zero-arg construct path).
# Re-export it here so the born-path handler has explicit Python wiring evidence
# (the runtime resolves it dynamically from the contract, which the static
# unimported-handler gate cannot see).
#
# OMN-19929: the entry-point wrapper used to subclass the admin-merge handler, the
# admin merge of stuck merge-queue PRs. That handler is removed (an admin merge
# of a PR the queue refused is a merge outside the queue); the wrapper now names
# the contract's declared handler.
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)


class NodePrLifecycleFixEffect(HandlerPrLifecycleFixRuntime):
    """ONEX entry-point wrapper for HandlerPrLifecycleFixRuntime."""


__all__ = ["HandlerPrLifecycleFixRuntime", "NodePrLifecycleFixEffect"]
