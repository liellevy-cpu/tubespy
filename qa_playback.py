import json
from playwright.sync_api import sync_playwright

result = {"console_errors": [], "page_errors": []}
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.set_default_timeout(10000)
    page.on("console", lambda msg: result["console_errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)))
    page.goto("http://127.0.0.1:8767/", wait_until="domcontentloaded", timeout=15000)
    card = page.locator(".video-card").first
    card.locator(".video-launch").click()
    iframe = card.locator("iframe")
    result["iframe_src"] = iframe.get_attribute("src")
    page.wait_for_timeout(8000)
    handle = iframe.element_handle()
    frame = handle.content_frame() if handle else None
    result["frame_url"] = frame.url if frame else None
    result["video_present"] = frame.locator("video").count() if frame else 0
    if frame and frame.locator("video").count():
        result["playback"] = frame.locator("video").evaluate(
            "v => ({currentTime:v.currentTime, paused:v.paused, readyState:v.readyState, error:v.error && v.error.code})"
        )
    browser.close()
print(json.dumps(result, indent=2))
