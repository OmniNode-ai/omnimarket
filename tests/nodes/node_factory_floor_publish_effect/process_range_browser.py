# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manual browser proof of the shipped Process page, using synthetic compute data.

Run with Playwright and its Chromium dependencies installed::

    uv run --with playwright python tests/nodes/node_factory_floor_publish_effect/process_range_browser.py --output /tmp/process-range-proof

This exercises touch, keyboard, ranges, Eastern input, missing data, embedded mode
and dark mode. It writes real screenshots and structured observations; it neither
reads live operational data nor publishes a site.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from omnimarket.nodes.node_process_health_compute.handlers.handler_process_health import (
    HandlerProcessHealth,
)
from omnimarket.nodes.node_process_health_compute.tests.fixtures import request


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture and exercise the Process page with synthetic compute fixtures"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    html = (
        root
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/reports/process-health/index.html"
    ).read_text()
    data = HandlerProcessHealth().handle(request()).model_dump_json()
    observations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for label, width, height in [
            ("phone", 390, 844),
            ("tablet", 768, 1024),
            ("desktop", 1440, 1000),
        ]:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                has_touch=True,
                timezone_id="Pacific/Auckland",
            )
            page = context.new_page()
            page.clock.set_fixed_time("2026-11-04T18:00:00Z")
            errors = []
            page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))

            def route(r):
                if r.request.url.endswith("process-health.json"):
                    r.fulfill(content_type="application/json", body=data)
                else:
                    r.fulfill(content_type="text/html", body=html)

            page.route("**/*", route)
            page.goto("http://process-graph.test/reports/process-health/")
            page.wait_for_selector("#range-counts")
            assert not errors, errors
            assert (
                page.locator("[data-range=reset]").get_attribute("aria-pressed")
                == "true"
            )
            initial = page.locator("#range-counts").inner_text()
            fit = page.evaluate(
                '({width:innerWidth,scroll:document.documentElement.scrollWidth,control:document.querySelector(".segments").getBoundingClientRect().toJSON(),graph:document.querySelector("#timeline").getBoundingClientRect().toJSON()})'
            )
            assert fit["scroll"] <= width, fit
            assert fit["control"]["right"] <= width, fit
            assert fit["graph"]["width"] >= 270, fit
            page.set_viewport_size({"width": width, "height": 1600})
            controls = page.locator("#controls").bounding_box()
            page.screenshot(
                path=str(out / f"{label}.png"),
                clip={"x": 0, "y": controls["y"], "width": width, "height": 1100},
            )
            page.set_viewport_size({"width": width, "height": height})
            for choice in ("1d", "3d", "7d", "month", "reset"):
                page.locator(f'[data-range="{choice}"]').click()
                assert (
                    page.locator(f'[data-range="{choice}"]').get_attribute(
                        "aria-pressed"
                    )
                    == "true"
                )
                assert not errors, errors
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator('[data-range="1d"]').click()
            changed = page.locator("#range-counts").inner_text()
            assert changed != initial, (changed, initial)
            # The graph is inspected with touch, and the range input supports keyboard.
            graph = page.locator("#timeline")
            graph.tap(position={"x": 75, "y": 110})
            first = page.locator("#point-detail").inner_text()
            page.locator("#point-index").focus()
            page.keyboard.press("End")
            last = page.locator("#point-detail").inner_text()
            assert first != last, (first, last)
            page.select_option("#measure", "merges.total")
            assert page.locator("#graph-title").inner_text().lower() == "merges"
            page.locator("[data-range=custom]").click()
            page.fill("#range-start", "2026-10-28T20:00")
            page.fill("#range-end", "2026-10-29T20:00")
            page.get_by_role("button", name="Apply range").click()
            assert (
                "Wed 28 Oct, 8:00 pm ET"
                in page.locator("#comparison-windows").inner_text()
            )
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.fill("#range-start", "2026-10-28T20:00")
            page.fill("#range-end", "2026-10-28T21:00")
            page.get_by_role("button", name="Apply range").click()
            assert "shorter" in page.locator("#graph-description").inner_text()
            assert page.locator("[data-count=improved] b").inner_text() == "0"
            assert page.locator("[data-count=worse] b").inner_text() == "0"
            assert int(page.locator("[data-count=unmeasured] b").inner_text()) > 0
            # A failed refresh clears earlier figures rather than keeping a stale verdict.
            page.route(
                "**/process-health.json", lambda r: r.fulfill(status=503, body="unread")
            )
            page.evaluate("load()")
            assert page.locator("#banner").is_visible()
            assert not page.locator("#range-counts").count()
            assert not errors, errors
            observations.append(
                {
                    "viewport": label,
                    "fit": fit,
                    "counts_change": True,
                    "touch_and_keyboard": True,
                    "custom_eastern": True,
                    "sub_bucket_unmeasured": True,
                    "stale_refresh_cleared": True,
                    "errors": errors,
                }
            )
            page.unroute("**/process-health.json")
            page.goto("http://process-graph.test/reports/process-health/?embed=1")
            page.wait_for_selector("#range-counts")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.emulate_media(color_scheme="dark")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            contrast = page.evaluate(r"""() => {
                const luminance = color => {
                    const c = color.match(/[0-9.]+/g).slice(0,3).map(x => Number(x)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);
                    return c[0]*.2126+c[1]*.7152+c[2]*.0722;
                };
                const ratio = (a,b) => (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
                const bg=luminance(getComputedStyle(document.querySelector('.timeline')).backgroundColor);
                const text=luminance(getComputedStyle(document.querySelector('#timeline text')).fill);
                const line=luminance(getComputedStyle(document.querySelector('#timeline .trace')).stroke);
                return {text:ratio(text,bg),line:ratio(line,bg)};
            }""")
            assert contrast["text"] >= 4.5, contrast
            assert contrast["line"] >= 3, contrast
            observations[-1]["dark_contrast"] = contrast
            observations[-1]["embedded_mode"] = True
            observations[-1]["dark_mode"] = True
            context.close()
        browser.close()
    (out / "observations.json").write_text(json.dumps(observations, indent=2) + "\n")
    print(json.dumps(observations, indent=2) + "\n")


if __name__ == "__main__":
    main()
