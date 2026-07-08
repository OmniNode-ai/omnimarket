from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_admin_merge import (
    HandlerAdminMerge,
)

# OMN-13990: the contract's handler_routing points the runtime at
# HandlerPrLifecycleFixRuntime (live OCC adapters on the zero-arg construct path).
# Re-export it here so the born-path handler has explicit Python wiring evidence
# (the runtime resolves it dynamically from the contract, which the static
# unimported-handler gate cannot see).
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)


class NodePrLifecycleFixEffect(HandlerAdminMerge):
    """ONEX entry-point wrapper for HandlerAdminMerge."""


__all__ = ["HandlerPrLifecycleFixRuntime", "NodePrLifecycleFixEffect"]
