"""node_env_parity_collect_effect - live runtime-lane env collection + parity.

Read-only EFFECT front-end for env parity (OMN-13925): snapshots the real
runtime lanes over ssh (``docker ps`` + ``docker inspect .Config.Env``,
never a mutation) and evaluates the shared env-parity engine over the freshly
collected snapshots. Fails fast when no live collection input is available.
"""

from omnimarket.nodes.node_env_parity_collect_effect.handlers.handler_env_parity_collect import (
    HandlerEnvParityCollect,
)


class NodeEnvParityCollectEffect(HandlerEnvParityCollect):
    """ONEX entry-point wrapper for HandlerEnvParityCollect."""


__all__ = ["HandlerEnvParityCollect", "NodeEnvParityCollectEffect"]
