"""Compatibility wrapper for the installed projection API entry point."""

from omnimarket.projection.api_server import (  # noqa: F401
    _dsn,
    app,
    compute_freshness,
    get_pool,
    get_topic_map,
    main,
)

if __name__ == "__main__":
    main()
