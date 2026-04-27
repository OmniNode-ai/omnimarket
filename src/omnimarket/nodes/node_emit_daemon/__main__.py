# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Standalone runner for the emit daemon.

Usage:
    python -m omnimarket.nodes.node_emit_daemon start --event-registry path/to/registry.yaml
    python -m omnimarket.nodes.node_emit_daemon stop
    python -m omnimarket.nodes.node_emit_daemon health

Socket path resolution: CLI arg > env var > XDG_RUNTIME_DIR > /tmp fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import logging
import logging.handlers
import os
import shutil
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DAEMON_LOGGER_NAME = "omnimarket.emit_daemon"


class _GzipRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that gzip-compresses rotated backup files.

    After doRollover renames baseFilename → baseFilename.1 (and shifts older
    backups), this subclass compresses the freshly-rotated file in-place so
    backups end up as ``<log>.1.gz``, ``<log>.2.gz``, etc.
    """

    def doRollover(self) -> None:  # noqa: N802
        # Close the current stream before rotation bookkeeping.
        if self.stream:
            self.stream.close()
            self.stream = None

        # Shift existing gz backups first (log.N.gz → log.(N+1).gz).
        if self.backupCount > 0:
            for _i in range(self.backupCount - 1, 0, -1):
                src = f"{self.baseFilename}.{_i}.gz"
                dst = f"{self.baseFilename}.{_i + 1}.gz"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.rename(src, dst)

            # Compress the current log into log.1.gz.
            src_plain = self.baseFilename
            dst_gz = f"{self.baseFilename}.1.gz"
            if os.path.exists(dst_gz):
                os.remove(dst_gz)
            if os.path.exists(src_plain):
                with open(src_plain, "rb") as f_in, gzip.open(dst_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                # Truncate the source file so the new stream starts clean.
                Path(src_plain).write_bytes(b"")

        # Reopen the base log file for continued writing.
        if not self.delay:
            self.stream = self._open()


def _configure_logging(
    log_path: str,
    max_bytes: int = 100 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Attach a gzip-rotating file handler to the daemon-specific logger.

    Uses a named logger (not root) to avoid polluting test or library logging.
    Returns the configured logger so callers can emit startup messages
    immediately after configuration.
    """
    daemon_logger = logging.getLogger(_DAEMON_LOGGER_NAME)
    daemon_logger.setLevel(logging.DEBUG)

    # Remove any stale handlers from a previous call (e.g. in tests).
    for h in list(daemon_logger.handlers):
        daemon_logger.removeHandler(h)
        h.close()

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    handler = _GzipRotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    daemon_logger.addHandler(handler)

    # Do not propagate to root to keep the daemon's file handler isolated.
    daemon_logger.propagate = False

    return daemon_logger


def _resolve_socket_path(cli_arg: str | None) -> str:
    """Resolve socket path: CLI arg > env > XDG_RUNTIME_DIR > /tmp."""
    if cli_arg:
        return cli_arg
    env_path = os.environ.get("ONEX_EMIT_SOCKET_PATH")
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "onex", "emit.sock")
    return "/tmp/onex-emit.sock"


def _resolve_pid_path(cli_arg: str | None) -> str:
    """Resolve PID path: CLI arg > env > XDG_RUNTIME_DIR > /tmp."""
    if cli_arg:
        return cli_arg
    env_path = os.environ.get("ONEX_EMIT_PID_PATH")
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "onex", "emit.pid")
    return "/tmp/onex-emit.pid"


def _resolve_spool_dir(cli_arg: str | None) -> str:
    """Resolve spool directory: CLI arg > env > XDG_RUNTIME_DIR > /tmp."""
    if cli_arg:
        return cli_arg
    env_path = os.environ.get("ONEX_EMIT_SPOOL_DIR")
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "onex", "event-spool")
    return "/tmp/onex-event-spool"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ONEX Emit Daemon - Portable Event Publisher",
        prog="python -m omnimarket.nodes.node_emit_daemon",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    start_parser = sub.add_parser("start", help="Start the daemon")
    start_parser.add_argument(
        "--socket-path",
        default=None,
        help="Unix socket path (default: auto-resolve)",
    )
    start_parser.add_argument(
        "--pid-path",
        default=None,
        help="PID file path (default: auto-resolve)",
    )
    start_parser.add_argument(
        "--spool-dir",
        default=None,
        help="Event spool directory (default: auto-resolve)",
    )
    start_parser.add_argument(
        "--kafka-bootstrap-servers",
        default=None,
        help="Kafka bootstrap servers (host:port). If not set, runs without Kafka.",
    )
    start_parser.add_argument(
        "--event-registry",
        default=None,
        help="Path to event registry YAML file",
    )
    start_parser.add_argument(
        "--log-path",
        default=None,
        help="Path for the rotating log file. If omitted, logging goes to stderr only.",
    )
    start_parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=100 * 1024 * 1024,
        help="Maximum log file size in bytes before rotation (default: 100 MB).",
    )
    start_parser.add_argument(
        "--log-backup-count",
        type=int,
        default=5,
        help="Number of gzip-compressed backup files to retain (default: 5).",
    )

    # stop
    stop_parser = sub.add_parser("stop", help="Stop the daemon")
    stop_parser.add_argument("--pid-path", default=None, help="PID file path")

    # health
    health_parser = sub.add_parser("health", help="Check daemon health")
    health_parser.add_argument("--socket-path", default=None, help="Socket path")

    return parser.parse_args(argv)


def _do_start(args: argparse.Namespace) -> int:
    if args.log_path:
        _configure_logging(
            args.log_path,
            max_bytes=args.log_max_bytes,
            backup_count=args.log_backup_count,
        )

    from omnimarket.nodes.node_emit_daemon.event_queue import BoundedEventQueue
    from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
    from omnimarket.nodes.node_emit_daemon.handlers.handler_emit_daemon import (
        HandlerEmitDaemon,
    )
    from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    socket_path = _resolve_socket_path(args.socket_path)
    pid_path = Path(_resolve_pid_path(args.pid_path))
    spool_dir = Path(_resolve_spool_dir(args.spool_dir))

    # Check for existing daemon
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return 1
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)

    # Load event registry
    registry: EventRegistry
    if args.event_registry:
        registry = EventRegistry.from_yaml(Path(args.event_registry))
        logger.info(
            f"Loaded event registry from {args.event_registry} "
            f"({len(registry)} event types)"
        )
    else:
        # Try default registry location
        default_registry = Path(__file__).parent / "registries" / "topics.yaml"
        if default_registry.exists():
            registry = EventRegistry.from_yaml(default_registry)
            logger.info(f"Loaded default event registry ({len(registry)} event types)")
        else:
            registry = EventRegistry()
            logger.warning("No event registry found, starting with empty registry")

    # Create components
    handler = HandlerEmitDaemon()
    queue = BoundedEventQueue(spool_dir=spool_dir)

    # Create publisher loop with optional Kafka
    async def _noop_publish(
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str],
    ) -> None:
        logger.debug(f"[no-kafka] Would publish to {topic} ({len(value)} bytes)")

    publish_fn = _noop_publish
    if args.kafka_bootstrap_servers:
        logger.info(f"Kafka publishing enabled: {args.kafka_bootstrap_servers}")
        # Kafka integration will be wired here when available

    publisher = KafkaPublisherLoop(queue=queue, publish_fn=publish_fn)

    # Wire publisher into socket server so health endpoint can report circuit state
    server = EmitSocketServer(
        socket_path=socket_path,
        queue=queue,
        registry=registry,
        publisher_loop=publisher,
    )

    # Write PID file
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    shutdown_event = asyncio.Event()

    async def _run() -> None:
        handler.transition_to_binding(socket_path, os.getpid())

        try:
            await queue.load_spool()
            await server.start()
            await publisher.start()
            handler.transition_to_listening()
        except Exception as e:
            handler.transition_to_failed(str(e))
            raise

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_event.set)

        logger.info(f"Emit daemon running (socket={socket_path}, pid={os.getpid()})")
        await shutdown_event.wait()

        handler.transition_to_draining()
        await server.stop()
        await publisher.stop()

        drained = await queue.drain_to_spool()
        if drained > 0:
            logger.info(f"Drained {drained} events to spool")

        handler.transition_to_stopped(
            events_published=publisher.events_published,
            events_dropped=publisher.events_dropped,
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        pid_path.unlink(missing_ok=True)

    return 0


def _do_stop(args: argparse.Namespace) -> int:
    pid_path = Path(_resolve_pid_path(args.pid_path))
    if not pid_path.exists():
        return 0

    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
        return 0
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return 0
    except Exception:
        return 1


def _do_health(args: argparse.Namespace) -> int:
    import json as _json

    from omnimarket.nodes.node_emit_daemon.client import EmitClient

    socket_path = _resolve_socket_path(args.socket_path)
    client = EmitClient(socket_path=socket_path, timeout=2.0)
    try:
        health = client.health_sync()
        sys.stdout.write(_json.dumps(health, indent=2, default=str) + "\n")
        return 0 if health.get("healthy") else 1
    except Exception:
        sys.stdout.write('{"healthy": false, "error": "daemon unreachable"}\n')
        return 1
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    args = _parse_args(argv)

    if args.command == "start":
        return _do_start(args)
    if args.command == "stop":
        return _do_stop(args)
    if args.command == "health":
        return _do_health(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
