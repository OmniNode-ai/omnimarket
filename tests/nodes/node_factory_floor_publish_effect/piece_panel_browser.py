# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manual browser proof of the main page's piece side panel (OMN-22810), run against one page file at a time.

Run with Playwright and its Chromium dependencies installed, once per page under test::

    uv run --with playwright python tests/nodes/node_factory_floor_publish_effect/piece_panel_browser.py \
        --page src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html --expect present --output /tmp/panel-branch
    git show <base>:src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html > /tmp/dev-simple.html
    uv run --with playwright python tests/nodes/node_factory_floor_publish_effect/piece_panel_browser.py \
        --page /tmp/dev-simple.html --expect absent --output /tmp/panel-dev

The page is served as ``index.html`` (the name the publisher gives it) next to a ``floor-simple.json`` that the real
compute handler wrote from synthetic receipts; nothing reads live data and nothing is published. The script opens the panel
from the strip at desktop and phone size, closes it by Esc, the close button, a click outside and Back, checks that the page
does not reflow, then loads ``#piece-2`` straight from the address. It writes screenshots and ``observations.json``.
``--expect present`` exits 1 when any step fails; ``--expect absent`` exits 1 when a panel exists on the page.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

ORIGIN = "http://floor.test"
SIZES = (("desktop", 1440, 900), ("phone", 390, 844))
PIECE = 2


def record() -> tuple[str, str, float]:
    from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
        HandlerFloorSimple,
    )
    from tests.nodes.node_floor_simple_compute.test_golden_and_error_chain_floor_simple_compute import (
        NOW,
    )
    from tests.nodes.node_floor_simple_compute.test_piece_panel_cells_floor_simple_compute import (
        chain,
        with_receipts,
    )

    doc = HandlerFloorSimple().handle(
        with_receipts(
            (
                chain(PIECE, "golden", 60, "a"),
                chain(PIECE, "golden", 3600, "b"),
                chain(PIECE, "error", 120, "c"),
            )
        )
    )
    name = next(p.name for p in doc.pieces if p.piece == PIECE)
    return doc.model_dump_json(), name, NOW.timestamp()


def layout(page: Page) -> dict[str, object]:
    return page.evaluate(
        """() => ({scrollWidth: document.documentElement.scrollWidth,
                   scrollHeight: document.documentElement.scrollHeight,
                   bodyOverflow: getComputedStyle(document.body).overflow,
                   bodyWidth: document.body.getBoundingClientRect().width,
                   strip: document.querySelector('.strip')?.getBoundingClientRect().toJSON() ?? null})"""
    )


def panel_open(page: Page) -> bool:
    return page.evaluate(
        "() => { const p = document.getElementById('ppanel');"
        " return !!p && p.classList.contains('open') && getComputedStyle(p).visibility === 'visible'; }"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", type=Path, required=True)
    parser.add_argument("--expect", choices=("present", "absent"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    html = args.page.read_text(encoding="utf-8")
    data, name, record_time = record()
    observations: dict[str, object] = {
        "page": str(args.page),
        "expect": args.expect,
        "piece": PIECE,
        "piece_name": name,
        "sizes": {},
    }
    failures: list[str] = []
    sizes: dict[str, dict[str, Any]] = {}
    observations["sizes"] = sizes

    def served(route: Any) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        if path in ("/", "/index.html"):
            route.fulfill(content_type="text/html", body=html)
        elif path == "/floor-simple.json":
            route.fulfill(content_type="application/json", body=data)
        else:
            route.fulfill(status=404, body="not found")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for label, width, height in SIZES:
            context = browser.new_context(
                viewport={"width": width, "height": height}, has_touch=False
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))
            # the synthetic record is stamped at record_time: read it a minute later, so the page shows a fresh record, not an expired one
            page.clock.set_fixed_time(record_time + 60)
            page.route("**/*", served)
            page.goto(f"{ORIGIN}/index.html")
            page.wait_for_selector(".strip", timeout=15000)
            seen: dict[str, Any] = {
                "panel_element": page.locator("#ppanel").count() > 0,
                "strip_links": page.locator(".strip a[data-piece]").count(),
            }
            sizes[label] = seen
            if args.expect == "absent":
                if seen["panel_element"] or seen["strip_links"]:
                    failures.append(f"{label}: the page already has a panel")
                page.screenshot(path=str(args.output / f"{label}-no-panel.png"))
                context.close()
                continue
            before = layout(page)
            link = page.locator(f'.strip a[data-piece="{PIECE}"]')
            link.click()
            seen["opened_by_click"] = panel_open(page)
            seen["address_after_click"] = page.evaluate("location.hash")
            seen["title"] = page.locator("#ptitle").inner_text()
            seen["layout_before"] = before
            seen["layout_open"] = layout(page)
            page.screenshot(path=str(args.output / f"{label}-open.png"))
            for how in ("escape", "close-button", "click-outside", "back"):
                if not panel_open(page):
                    link.click()
                if how == "escape":
                    page.keyboard.press("Escape")
                elif how == "close-button":
                    page.locator("#pclose").click()
                elif how == "click-outside":
                    # the backdrop covers the page; click where the panel is not (left of it on desktop, above it on a phone)
                    page.mouse.click(40, 40)
                else:
                    page.go_back()
                page.wait_for_timeout(400)
                seen[f"closed_by_{how}"] = not panel_open(page)
            seen["layout_closed"] = layout(page)
            seen["page_errors"] = list(errors)
            # The address alone opens the panel (AC3): a fresh load at #piece-N, no click.
            page.goto(f"{ORIGIN}/index.html#piece-{PIECE}")
            page.reload()
            page.wait_for_selector(".strip", timeout=15000)
            page.wait_for_timeout(600)
            seen["from_address_open"] = panel_open(page)
            seen["from_address_title"] = page.locator("#ptitle").inner_text()
            page.screenshot(path=str(args.output / f"{label}-from-address.png"))
            for key in (
                "opened_by_click",
                "closed_by_escape",
                "closed_by_close-button",
                "closed_by_click-outside",
                "closed_by_back",
                "from_address_open",
            ):
                if seen[key] is not True:
                    failures.append(f"{label}: {key} is {seen[key]!r}")
            if seen["address_after_click"] != f"#piece-{PIECE}":
                failures.append(f"{label}: address is {seen['address_after_click']!r}")
            for key in ("title", "from_address_title"):
                if name not in str(seen[key]):
                    failures.append(f"{label}: {key} {seen[key]!r} lacks {name!r}")
            for key in ("layout_open", "layout_closed"):
                for cell in ("scrollWidth", "scrollHeight", "bodyWidth", "strip"):
                    if seen[key][cell] != before[cell]:
                        failures.append(
                            f"{label}: {key}.{cell} {seen[key][cell]!r} != {before[cell]!r}"
                        )
            if errors:
                failures.append(f"{label}: page errors {errors}")
            context.close()
        browser.close()
    observations["failures"] = failures
    (args.output / "observations.json").write_text(
        json.dumps(observations, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(observations, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
