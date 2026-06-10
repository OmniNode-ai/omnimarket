"""Compatibility wrapper for the installed projection API entry point."""

from omnimarket.projection.api_server import (  # noqa: F401
    PROJECTION_DATABASE_BINDING_OVERLAY_ENV,
    ModelProjectionDatabaseBinding,
    _cors_origins_from_env,
    _dsn,
    app,
    compute_freshness,
    get_pool,
    get_topic_map,
    load_projection_database_binding_overlay,
    main,
)

if __name__ == "__main__":
    main()
