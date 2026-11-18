# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the original retirement checks at their published pytest path."""

from tests.unit.test_no_retired_package_running_references import (
    test_import_scan_flags_an_import_of_the_retired_package as test_import_scan_flags_an_omnibase_compat_import,
)
from tests.unit.test_no_retired_package_running_references import (
    test_no_module_under_src_imports_the_retired_package as test_no_module_under_src_imports_omnibase_compat,
)
from tests.unit.test_no_retired_package_running_references import (
    test_no_node_metadata_lists_the_retired_dependency as test_no_node_metadata_lists_omnibase_compat_dependency,
)
from tests.unit.test_no_retired_package_running_references import (
    test_pyproject_names_no_retired_requirement_or_override as test_pyproject_names_no_omnibase_compat_requirement_or_override,
)

__all__ = [
    "test_import_scan_flags_an_omnibase_compat_import",
    "test_no_module_under_src_imports_omnibase_compat",
    "test_no_node_metadata_lists_omnibase_compat_dependency",
    "test_pyproject_names_no_omnibase_compat_requirement_or_override",
]
