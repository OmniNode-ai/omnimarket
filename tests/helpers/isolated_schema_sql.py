"""Apply a node migration inside a test's own schema (OMN-21970).

The real-Postgres suites migrate a disposable schema and set ``search_path`` to
``<schema>, public``, so a migration's unqualified names land in that schema.
node_projection_delegation's 0065 and 0066 name ``public.delegation_events``
and their other relations explicitly, because omnibase_infra's application SQL
gate requires a schema-qualified relation and the files are vendored there byte
for byte. Applied as written into a test schema, they would look for
``public.delegation_events`` and fail. Dropping the qualifier resolves each name
through the same ``search_path`` as every older migration, which in a test is
the test's schema and in a deployment is ``public``: the same relations.

A catalogue assertion that filters ``table_schema = 'public'`` is rewritten to
``current_schema()`` for the same reason: it must count the test schema's tables.
"""

from __future__ import annotations

import re

_PUBLIC_QUALIFIER = re.compile(r"\bpublic\.(?=[A-Za-z_\"])")
_PUBLIC_SCHEMA_FILTER = re.compile(r"\btable_schema\s*=\s*'public'")


def for_isolated_schema(sql: str) -> str:
    """Return ``sql`` with ``public.`` qualifiers and ``public`` catalogue filters scoped to the test schema."""
    return _PUBLIC_SCHEMA_FILTER.sub(
        "table_schema = current_schema()", _PUBLIC_QUALIFIER.sub("", sql)
    )
