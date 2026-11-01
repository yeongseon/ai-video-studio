"""Decoded Timeline acceptance: 30 fps fixture, seek tolerance ±2 source frames.

Local cached-browser override: PLAYWRIGHT_CHROMIUM_EXECUTABLE. CI uses the
standard installed Chromium. TIMELINE_BROWSER_SCREENSHOT_PREFIX saves QA PNGs.
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

from studio_proxy_runtime_support import open_studio_proxy, prerequisites_or_skip
from timeline_browser_support import seed_mixed_timeline


def seek(page: Page, time: float) -> None:
    page.get_by_test_id("preview-seek").fill(str(time))


def frame(page: Page, source_time: float) -> list[int]:
    # Wait for the actual seek/decode and compositor, not an arbitrary delay.
    page.wait_for_function("""target => {
        const video = document.querySelector('[data-testid="preview-media"]');
        return video instanceof HTMLVideoElement && !video.seeking && video.readyState >= 2
            && Math.abs(video.currentTime - target) <= 2 / 30;
    }""", arg=source_time, timeout=3000)
    return page.get_by_test_id("preview-media").evaluate("""async video => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 1;
        const context = canvas.getContext('2d');
        context.drawImage(video, 0, 0, 1, 1);
        return Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3);
    }""")


def capture(page: Page, name: str) -> None:
    prefix = os.getenv("TIMELINE_BROWSER_SCREENSHOT_PREFIX")
    if prefix:
        page.screenshot(path=f"{prefix}-{name}.png", full_page=True)


def test_trimmed_video_play_pause_seek_and_mixed_boundaries(tmp_path: Path) -> None:
    needs = prerequisites_or_skip(required_in_ci=True)
    with open_studio_proxy(tmp_path, needs) as runtime:
        project_id, video_path = seed_mixed_timeline(runtime, tmp_path)
        with sync_playwright() as automation:
            executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            browser = automation.chromium.launch(executable_path=executable, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.set_default_timeout(3000)
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{runtime.origin}/projects/{project_id}")
                media = page.get_by_test_id("preview-media")
                expect(media).to_have_js_property("tagName", "IMG")
                approval = page.get_by_role("button", name="Approve timeline & render")
                expect(approval).to_be_enabled()
                seek(page, 0.7)
                if os.getenv("TIMELINE_BROWSER_BASELINE") == "true":
                    page.wait_for_function("() => document.querySelector('video')?.readyState >= 2")
                    capture(page, "green-before")
                green = frame(page, 1.2)
                assert green[1] > 90 and green[0] < 30 and green[2] < 30, green
                capture(page, "green-seek")
                seek(page, 1.9)
                blue = frame(page, 2.4)
                assert blue[2] > 200 and blue[0] < 30 and blue[1] < 30, blue
                capture(page, "blue-seek")

                seek(page, 0.7)
                frame(page, 1.2)
                page.get_by_test_id("preview-play-toggle").click()
                page.wait_for_function("() => document.querySelector('video').currentTime >= 1.5")
                position = page.evaluate("""() => {
                    const video = document.querySelector('video');
                    const time = Number(document.querySelector('[data-testid="preview-seek"]').value);
                    return { drift: Math.abs(video.currentTime - (1 + time - 0.5)), paused: video.paused };
                }""")
                assert not position["paused"]
                assert position["drift"] <= 2 / 30, position
                page.get_by_test_id("preview-play-toggle").click()
                expect(media).to_have_js_property("paused", True)
                stopped = media.evaluate("video => video.currentTime")
                logical = page.get_by_test_id("preview-seek").input_value()
                page.evaluate("""async () => {
                    await new Promise(resolve => {
                        let frames = 0;
                        const step = () => ++frames === 10 ? resolve() : requestAnimationFrame(step);
                        requestAnimationFrame(step);
                    });
                }""")
                assert media.evaluate("video => video.currentTime") == stopped
                assert page.get_by_test_id("preview-seek").input_value() == logical

                seek(page, 0.4)
                expect(media).to_have_js_property("tagName", "IMG")
                page.get_by_test_id("preview-play-toggle").click()
                expect(media).to_have_js_property("tagName", "VIDEO")
                page.wait_for_function("() => !document.querySelector('video').paused")
                expect(media).to_have_js_property("tagName", "IMG")
                page.get_by_test_id("preview-play-toggle").click()

                seek(page, 2.5)
                red = frame(page, 0.2)
                assert red[0] > 200 and red[1] < 30 and red[2] < 30, red
                seek(page, 0.5)
                assert frame(page, 1)[1] > 90
                seek(page, 2)
                expect(media).to_have_js_property("tagName", "IMG")
                video_path.write_bytes(b"not a decodable video")
                seek(page, 2.5)
                expect(approval).to_be_disabled()
                expect(page.get_by_test_id("timeline-preview")).to_have_attribute("data-status", "error")
                assert errors == [], errors
            finally:
                browser.close()
