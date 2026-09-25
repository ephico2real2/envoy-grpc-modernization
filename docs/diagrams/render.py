#!/usr/bin/env python3
"""Render every .fig-scroll figure of a /visual page to light and dark PNGs, and check the page.

    python3 render.py <page.html> <out-dir> <name-1>,<name-2>,...

One name per .fig-scroll, in document order; each becomes <out-dir>/<name>.light.png and
<name>.dark.png at 2x pixel density. The page may be a fragment (an Artifact page starts at
<title>): it is wrapped in a document for rendering, never modified. Exit status is non-zero on a
page error, a name/figure count mismatch, or horizontal page scroll at 375 px — the three defects a
code review of the SVG text does not see.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed: python3 -m pip install playwright && python3 -m playwright install chromium")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    page_path, out_dir = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]).resolve()
    names = [n.strip() for n in sys.argv[3].split(",") if n.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)

    text = page_path.read_text()
    if "<html" not in text.lower():
        text = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>'
                f"{text}</body></html>")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        doc = pathlib.Path(tmp) / "page.html"
        doc.write_text(text)
        browser = p.chromium.launch()
        for theme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=2)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(doc.as_uri())
            page.evaluate(f"() => document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_timeout(1200)   # the web fonts; a render before they load measures the fallback
            figures = page.locator(".fig-scroll")
            if figures.count() != len(names):
                failures.append(f"{figures.count()} .fig-scroll figures but {len(names)} names given")
                break
            for i, name in enumerate(names):
                target = out_dir / f"{name}.{theme}.png"
                figures.nth(i).screenshot(path=str(target))
                print(f"wrote {target} ({target.stat().st_size} bytes)")
            failures += [f"{theme}: page error: {e}" for e in errors]
            page.close()
        phone = browser.new_page(viewport={"width": 375, "height": 800})
        phone.goto(doc.as_uri())
        phone.wait_for_timeout(600)
        width = phone.evaluate("document.documentElement.scrollWidth")
        print(f"375 px viewport: scrollWidth {width}")
        if width > 375:
            failures.append(f"the page scrolls sideways at 375 px (scrollWidth {width})")
        browser.close()
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
