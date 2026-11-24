"""Capture a public post from X's official embedded page."""

from pathlib import Path
from time import monotonic

from x_validator import POST_ID


def capture_candidate(post_id, directory, *, budget=12):
    if not isinstance(post_id, str) or not POST_ID.fullmatch(post_id) or not Path(directory).is_dir():
        return ""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        return ""

    deadline = monotonic() + budget

    def timeout(limit):
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError
        return min(limit, remaining_ms)

    filename = "candidate-1.png"
    try:
        with sync_playwright() as playwright:
            for channel in ("chrome", "msedge"):
                try:
                    browser = playwright.chromium.launch(channel=channel, chromium_sandbox=True, timeout=timeout(3500))
                except Error:
                    continue
                break
            else:
                return ""

            try:
                page = browser.new_page(
                    viewport={"width": 680, "height": 900}, device_scale_factor=1.5,
                    locale="zh-CN", accept_downloads=False,
                )
                url = f"https://platform.x.com/embed/Tweet.html?id={post_id}&hideConversation=true&lang=zh-cn"
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout(5500))
                if response is None or not response.ok:
                    return ""
                permalink = page.locator(f'a[href$="/status/{post_id}"], a[href*="/status/{post_id}?"]')
                article = page.locator("article", has=permalink).first
                article.wait_for(state="visible", timeout=timeout(4500))
                article.evaluate("""(post) => Promise.race([
                    Promise.allSettled(Array.from(post.querySelectorAll('img'), (img) => img.decode())),
                    new Promise((done) => setTimeout(done, 700))
                ])""", timeout=timeout(1000))
                image = article.screenshot(animations="disabled", timeout=timeout(3000))
            finally:
                browser.close()
        with (Path(directory) / filename).open("xb") as output:
            output.write(image)
        return filename
    except (Error, OSError, TimeoutError):
        return ""
