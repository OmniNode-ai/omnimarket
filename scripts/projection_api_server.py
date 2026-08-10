"""Compatibility wrapper for the installed projection API entry point."""

from omnimarket.projection.api_server import (  # noqa: F401
    _cors_origins_from_env,
    app,
    compute_freshness,
    get_snapshot_cache,
    get_topic_map,
    main,
    resolve_effective_limit,
    topic_supports_correlation_id_filter,
)

if __name__ == "__main__":
    main()
