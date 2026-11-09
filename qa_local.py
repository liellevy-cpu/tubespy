import json
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8767/"
SHOT = Path("stage/private-youtube-pilot-qa.png").resolve()
result = {"url": URL, "console_errors": [], "page_errors": [], "advertisers": []}

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.on("console", lambda msg: result["console_errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)))
    response = page.goto(URL, wait_until="domcontentloaded")
    result["http_status"] = response.status if response else None
    page.locator(".video-card").first.wait_for(state="visible", timeout=30000)
    selector = page.locator("#advertiser-select")
    options = selector.locator("option").evaluate_all("nodes => nodes.map(node => ({value: node.value, label: node.textContent}))")
    for option in options:
        selector.select_option(option["value"])
        cards = page.locator(".video-card").count()
        expected = 70 if option["value"] == "all" else 5
        if cards != expected:
            raise RuntimeError(f"view {option['value']} rendered {cards} cards; expected {expected}")
        views = [int(value.replace(",", "")) for value in page.locator(".views").all_inner_texts()]
        if views != sorted(views, reverse=True):
            raise RuntimeError(f"view {option['value']} is not ordered by views")
        result["advertisers"].append({"name": option["label"], "cards": cards})
    selector.select_option("all")
    if page.locator("#rank-heading").inner_text() != "All brands":
        raise RuntimeError("global view heading is missing")
    if len(set(page.locator(".advertiser").all_inner_texts())) < 2:
        raise RuntimeError("global view does not contain multiple brands")
    first = page.locator(".video-card").first
    result["first"] = {
        "title": first.locator(".title").inner_text(),
        "views": first.locator(".views").inner_text(),
        "uploaded": first.locator(".upload-date").inner_text(),
        "age": first.locator(".age").inner_text(),
        "pace": first.locator(".views-day").inner_text(),
        "iframe": first.locator("iframe").get_attribute("src"),
        "thumbnail": first.locator(".thumbnail").get_attribute("src"),
    }
    page.screenshot(path=str(SHOT), full_page=True)
    first.locator(".video-launch").click()
    result["clicked_iframe"] = first.locator("iframe").get_attribute("src")
    browser.close()

if result["http_status"] != 200 or result["console_errors"] or result["page_errors"]:
    raise RuntimeError(json.dumps(result))
result["screenshot"] = str(SHOT)
print(json.dumps(result, indent=2))
