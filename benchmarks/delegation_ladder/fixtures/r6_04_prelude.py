"""Adapted from omnibase_infra src/omnibase_infra/runtime/service_terminal_event_consumer.py
at the commit immediately BEFORE d703ca7fdf4ee97974a1f7e75730344660679bcf. The Kafka poll
loop and JSON decoding are reduced to a direct call; the correlator's two cooperating
methods below are the merged pre-fix text."""

from __future__ import annotations
