# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-failure alarm's wire models, re-exported from the shared silent-failure models package (OMN-21663).

The models live in ``omnimarket.models.silent_failure_alarm`` so the effect node reads them without reaching into this
node's package. The contract's ``decide_install_failure`` routing names this module, the same way every other
operation of this node names its own ``models`` module.
"""

from omnimarket.models.silent_failure_alarm.model_install_failure_alarm import (
    ModelInstallFailureAlarmRequest as ModelInstallFailureAlarmRequest,
)
from omnimarket.models.silent_failure_alarm.model_install_failure_alarm import (
    ModelInstallFailureAlarmResult as ModelInstallFailureAlarmResult,
)
from omnimarket.models.silent_failure_alarm.model_install_failure_alarm import (
    ModelInstallFailureRow as ModelInstallFailureRow,
)
from omnimarket.models.silent_failure_alarm.model_install_failure_alarm import (
    ModelInstallFailureState as ModelInstallFailureState,
)
