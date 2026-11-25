"""Compare an X screenshot and check the claims visible in it."""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from ipaddress import ip_address
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

from candidate_preview import capture_candidate
from deepseek_client import DeepSeekError, clients_from_env
from x_validator import HTTP_ERRORS, POST_HOSTS, USERNAME, XApiError, XClient, parse_post_id


ORIGINAL_LABELS = {
    "matched": "🟢 截图可见内容与原帖一致",
    "candidate": "🟡 找到候选，暂无法确认",
    "different": "🔴 找到原帖，存在差异",
    "located": "🔵 已找到原帖",
    "unconfirmed": "⚪ 暂无法确认",
}
FACT_LABELS = {
    "supported": "🟢 有证据支持",
    "refuted": "🔴 有证据反驳",
    "insufficient": "🟡 证据不足",
}
LINK = re.compile(r"https?://[^\s<>\"“”‘’，。；！）】]+", re.IGNORECASE)
RELATIVE_TIME = re.compile(r"(\d{1,3})\s*(m|min(?:utes?)?|分钟|h|hours?|小时|d|days?|天)(?:\s*(?:ago|前))?", re.I)
TRUNCATED_USERNAME = re.compile(r"@?([A-Za-z0-9_]{1,14})(?:…|\.{2,})")
VISIBLE_CLOCK = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")
TRANSLATION_CAPTIONS = {"译文", "这是译文", "翻译截图", "这是翻译截图", "x翻译"}
EXPAND_LABEL = re.compile(r"(?:\s+显示更多|(?:\n|…|\.{3})\s*Show more)\s*$", re.I)


def clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def original_link(text):
    for match in LINK.finditer(text):
        candidate = match[0].rstrip(".,;!)]")
        try:
            parse_post_id(candidate)
        except ValueError:
            continue
        return candidate
    return ""


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold().translate(str.maketrans("‘’“”", "''\"\""))
    return " ".join(text.split())


def significant_text(text):
    letters = [char for char in text if char.isalnum()]
    return len(letters) >= 22 or sum(ord(char) > 255 for char in letters) >= 10


def read_date(value):
    try:
        parsed = date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    except ValueError:
        return None
    return parsed if parsed and date(2006, 3, 22) <= parsed < date(9999, 1, 1) else None


def extracted_username(raw):
    value = clean_text(raw.get("username"), 64)
    handle = USERNAME.fullmatch(value)
    if handle:
        return handle[1]
    prefix = TRUNCATED_USERNAME.fullmatch(value)
    quoted = USERNAME.fullmatch(clean_text(raw.get("quoted_username"), 64))
    if prefix and quoted and raw.get("quote_same_author") is True:
        if len(quoted[1]) > len(prefix[1]) and quoted[1].casefold().startswith(prefix[1].casefold()):
            return quoted[1]
    return ""


def normalize_extraction(raw):
    if not isinstance(raw.get("is_x_post"), bool):
        raise DeepSeekError("模型未能识别截图类型。")
    visible = EXPAND_LABEL.sub("", clean_text(raw.get("visible_text"), 5000))
    search_phrase = clean_text(raw.get("search_phrase"), 140)
    if not significant_text(search_phrase) or normalize(search_phrase) not in normalize(visible):
        search_phrase = ""
        for line in visible.splitlines():
            candidate = LINK.sub("", line).strip().rstrip("… .")[:120]
            if significant_text(candidate):
                search_phrase = candidate
                break
    date_hint = clean_text(raw.get("date"), 20)
    claims = []
    candidates = raw.get("claims")
    if isinstance(candidates, list):
        for candidate in candidates[:3]:
            if isinstance(candidate, dict):
                claim = clean_text(candidate.get("claim"), 240)
                query = clean_text(candidate.get("query"), 128)
                if claim and all(normalize(item["claim"]) != normalize(claim) for item in claims):
                    claims.append({"claim": claim, "query": query or claim})
    return {
        "is_x_post": raw["is_x_post"],
        "ambiguous_target": raw.get("ambiguous_target") is True,
        "username": extracted_username(raw),
        "visible_text": visible,
        "date": date_hint if read_date(date_hint) else "",
        "clock": clean_text(raw.get("clock"), 12),
        "relative_time": clean_text(raw.get("relative_time"), 32),
        "capture_clock": clean_text(raw.get("capture_clock"), 12),
        "translated_only": raw.get("translated_only") is True,
        "back_translation": clean_text(raw.get("back_translation"), 5000),
        "search_phrase": search_phrase,
        "claims": claims,
    }


def read_missing_header(client, path, raw, screenshot, received_at):
    missing_time = screenshot["translated_only"] and not post_time_window(screenshot, received_at)
    if screenshot["username"] and not missing_time:
        return
    try:
        fields = client.extract_header(path)
        if not isinstance(fields, dict):
            return
        header = normalize_extraction(fields | {"is_x_post": True})
    except (DeepSeekError, ValueError):
        return

    username = header["username"]
    existing = screenshot["username"]
    prefix = TRUNCATED_USERNAME.fullmatch(clean_text(raw.get("username"), 64))
    if username and (
        (existing and existing.casefold() != username.casefold())
        or (prefix and not username.casefold().startswith(prefix[1].casefold()))
    ):
        return
    if username and not existing:
        screenshot["username"] = username
    use_header_time = missing_time and post_time_window(header, received_at)
    for field in ("date", "clock", "relative_time", "capture_clock"):
        if header[field] and (use_header_time or not screenshot[field]):
            screenshot[field] = header[field]


def extract_screenshot(client, path, received_at, *, translation_hint=False, repair=True, vision_reviewer=None):
    try:
        raw = client.extract(path)
        screenshot = normalize_extraction(raw)
    except DeepSeekError:
        if not vision_reviewer:
            raise
        raw = vision_reviewer.extract(path)
        screenshot = normalize_extraction(raw)
        repair = False
    if not screenshot["is_x_post"]:
        return screenshot

    screenshot["translated_only"] |= translation_hint
    if not repair:
        return screenshot
    visual_review = vision_reviewer or client
    has_embedded_footer = raw.get("embedded_layout") is True and not (screenshot["translated_only"] or screenshot["relative_time"])
    has_complete_header = screenshot["username"] and screenshot["date"] and VISIBLE_CLOCK.fullmatch(screenshot["clock"])
    needs_embedded_review = has_embedded_footer and not has_complete_header
    if needs_embedded_review or screenshot["ambiguous_target"]:
        try:
            reviewed = normalize_extraction(visual_review.extract(path, focus_embed=has_embedded_footer))
        except (DeepSeekError, ValueError):
            return screenshot
        if (
            reviewed["is_x_post"] and reviewed["username"] and not reviewed["ambiguous_target"]
            and (not needs_embedded_review or (reviewed["date"] and VISIBLE_CLOCK.fullmatch(reviewed["clock"])))
        ):
            reviewed["translated_only"] |= translation_hint
            return reviewed
    else:
        read_missing_header(visual_review, path, raw, screenshot, received_at)
    return screenshot


def original_result(status, reason, post=None):
    post = post or {}
    result = {"status": status, "reason": reason, "url": post.get("url", ""), "username": post.get("username", "")}
    result["published_at"] = post.get("published_at") or ""
    if post.get("author_profile"):
        result["author_profile"] = post["author_profile"]
    if status == "candidate":
        result["candidate"] = post
    return result


def screenshot_reference_time(screenshot, received_at):
    clock = screenshot.get("capture_clock", "")
    if not VISIBLE_CLOCK.fullmatch(clock):
        return received_at
    local_received = received_at.astimezone()
    capture = datetime.combine(local_received.date(), time.fromisoformat(clock.zfill(5))).astimezone()
    if capture > local_received:
        capture -= timedelta(days=1)
    if local_received - capture <= timedelta(hours=12):
        return capture.astimezone(timezone.utc)
    return received_at


def post_time_window(screenshot, received_at):
    day = read_date(screenshot["date"])
    if day:
        clock = screenshot.get("clock", "")
        if VISIBLE_CLOCK.fullmatch(clock):
            moment = datetime.combine(day, time.fromisoformat(clock.zfill(5))).astimezone(timezone.utc)
            margin = timedelta(minutes=5)
            return moment - margin, moment + margin
        start = datetime.combine(day, time.min).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min).astimezone(timezone.utc)
        return start, end

    reference = screenshot_reference_time(screenshot, received_at)
    relative = normalize(screenshot.get("relative_time", ""))
    if relative in {"昨天", "yesterday"}:
        yesterday = reference.astimezone().date() - timedelta(days=1)
        return post_time_window({"date": yesterday.isoformat()}, received_at)

    match = RELATIVE_TIME.fullmatch(relative)
    if not match:
        return None
    count, unit = int(match[1]), match[2].lower()
    if unit.startswith("d") or unit == "天":
        age, margin = timedelta(days=count), timedelta(hours=18)
    elif unit.startswith("h") or unit == "小时":
        age, margin = timedelta(hours=count), timedelta(minutes=90)
    else:
        age, margin = timedelta(minutes=count), timedelta(minutes=25)
    moment = reference - age
    return moment - margin, moment + margin


def published_timestamp(post):
    value = post.get("published_at")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo:
                return moment.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def search_author_in_window(client, username, bounds, allow_archive, now):
    start, end = bounds
    end = min(end, now - timedelta(seconds=20))
    recent_limit = now - timedelta(days=7) + timedelta(minutes=2)
    archive = start < recent_limit and allow_archive
    if not archive:
        start = max(start, recent_limit)
    if start >= end:
        return None

    result = client.search_author_posts(username, start, end, archive=archive)
    posts = []
    for post in result["posts"]:
        moment = published_timestamp(post)
        if moment and start <= moment < end and (post.get("username") or "").casefold() == username.casefold():
            posts.append(post)
    return result | {"posts": posts}


def text_similarity(seen, post):
    text = normalize(post.get("text") or "")
    if not seen or not text:
        return 0
    if seen in text:
        return 1
    # The screenshot often stops part-way through a long post.
    return SequenceMatcher(None, seen[:1200], text[:min(len(seen), 1200) + 60]).ratio()


def find_translated_original(client, screenshot, bounds, allow_archive, now):
    if not bounds:
        return original_result("unconfirmed", "译文截图里缺少可用的发帖时间，请补原帖链接或包含时间的截图。")
    page = search_author_in_window(client, screenshot["username"], bounds, allow_archive, now)
    if page is None:
        return original_result("unconfirmed", "截图时间超出了可查询范围，请补原帖链接。")
    back_translation = screenshot.get("back_translation")
    compare_text = normalize(back_translation or screenshot["visible_text"])
    candidates = [(text_similarity(compare_text, post), post) for post in page["posts"]]
    if not candidates:
        return original_result("unconfirmed", "在图中账号的估计发帖时间范围内没有找到候选，请补原帖链接。")

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_post = candidates[0]
    reason = "按图中账号和估计的发帖时间，展示最接近的一条候选。"
    if back_translation and best_score >= 0.70 and (len(candidates) == 1 or best_score - candidates[1][0] >= 0.20):
        reason += "回译后的文字线索与首个候选较接近。"
    reason += "仅凭译文还无法确认对应关系。"
    return original_result("candidate", reason, best_post)


def comparable_post_text(seen, actual, url_entities):
    aliases = {}
    for entity in url_entities:
        canonical = clean_text(entity.get("url"), 2048)
        if not canonical.startswith(("https://", "http://")) or canonical not in actual:
            continue
        for field in ("url", "display_url", "expanded_url"):
            alias = clean_text(entity.get(field), 2048)
            if not alias or any(char.isspace() for char in alias):
                continue
            for spelling in {alias, re.sub(r"^https?://", "", alias)}:
                aliases.setdefault(spelling, set()).add(canonical)

    # Compare known URL aliases before normalizing the prose. URL paths can be case-sensitive.
    canonical_links = sorted({url for targets in aliases.values() for url in targets})
    markers = {url: f"\x00{i}\x00" for i, url in enumerate(canonical_links)}
    replacements = {alias: markers[next(iter(targets))] for alias, targets in aliases.items() if len(targets) == 1}
    if replacements:
        pattern = "|".join(
            re.escape(alias).replace("/", r"/(?:\r?\n[ \t]*)?")
            for alias in sorted(replacements, key=len, reverse=True)
        )
        link = re.compile(r"(?<![\w./@+-])(?:" + pattern + r")(?=$|[\s<>\"“”‘’。，；！？、）】]|[.,!?;:)\]](?=\s|$))")

        def replace(match):
            return replacements[re.sub(r"\r?\n[ \t]*", "", match[0])]

        seen, actual = link.sub(replace, seen), link.sub(replace, actual)
    return normalize(seen).rstrip("… ."), normalize(actual)


def compare(post, screenshot):
    if not screenshot:
        return original_result("located", "发截图后可以逐项和原帖对照。", post)
    seen, actual = comparable_post_text(screenshot["visible_text"], post.get("text") or "", post.get("url_entities", ()))
    username = screenshot["username"].casefold()
    actual_user = (post.get("username") or "").casefold()
    date_hint = read_date(screenshot["date"])
    published = read_date((post.get("published_at") or "")[:10])
    # The screenshot time zone is usually unknown. Adjacent UTC dates can be the same local day.
    if date_hint and published and abs((date_hint - published).days) > 1:
        return original_result("different", "截图日期与 API 返回的发帖日期相差超过一天。", post)
    if username and actual_user and username != actual_user:
        return original_result("different", "截图账号与链接所指帖子的当前账号不同。", post)
    if screenshot.get("translated_only"):
        return original_result("located", "已按链接定位原帖。截图显示译文，译文与原文的对应关系待确认。", post)
    if not username or not actual_user or not significant_text(normalize(screenshot["visible_text"])) or not actual:
        return original_result("candidate", "截图的账号或可见文字不足以确认。", post)
    if seen in actual:
        return original_result("matched", "账号和截图可辨认的正文一致。", post)
    if len(post.get("edit_history_ids") or []) > 1:
        return original_result("different", "正文与 API 返回的版本不同，原帖有编辑记录。", post)
    return original_result("different", "可辨认的正文与 API 返回的正文有出入。", post)


def resolve_original_page(page, screenshot):
    matches = [post for post in page["posts"] if compare(post, screenshot)["status"] == "matched"]
    if len(matches) == 1 and not page["has_more"]:
        return compare(matches[0], screenshot)
    if matches:
        return original_result("candidate", "已找到正文一致的候选，当前结果不足以确认唯一原帖。", matches[0])

    seen = normalize(screenshot["visible_text"])
    username = screenshot["username"].casefold()
    date_hint = read_date(screenshot["date"])
    candidates = []
    for post in page["posts"]:
        published = published_timestamp(post)
        if (post.get("username") or "").casefold() != username:
            continue
        if date_hint and published and abs((published.date() - date_hint).days) > 1:
            continue
        score = text_similarity(seen, post)
        if score >= 0.5:
            candidates.append((score, post))
    if candidates:
        best = max(candidates, key=lambda item: item[0])[1]
        return original_result("candidate", "找到同账号的相似帖子，截图可见正文仍有出入或信息不足。", best)
    return original_result("unconfirmed", "用截图里的账号及可用的正文或时间线索，暂未找到可确认的原帖。")


def merge_search_pages(first, second, *, paginated=False):
    return {
        "posts": list({post["id"]: post for post in first["posts"] + second["posts"]}.values()),
        "has_more": second["has_more"] or (first["has_more"] and not paginated),
    }


def find_untranslated_original(client, screenshot, bounds, allow_archive, now):
    phrase = screenshot["search_phrase"]
    date_hint = read_date(screenshot["date"])
    older = bool(date_hint and date_hint < now.date() - timedelta(days=6))
    if older and not allow_archive:
        return original_result("unconfirmed", "截图早于近期搜索范围，请补发原帖链接。")

    username = screenshot["username"]
    exact_clock = date_hint and VISIBLE_CLOCK.fullmatch(screenshot.get("clock", ""))
    time_page = None
    if bounds and (exact_clock or not phrase):
        time_page = search_author_in_window(client, username, bounds, allow_archive, now)
        if time_page is not None:
            resolved = resolve_original_page(time_page, screenshot)
            if resolved["status"] == "matched" or not phrase:
                return resolved

    if not phrase:
        return original_result("unconfirmed", "截图里缺少可以搜索的正文或发帖时间。")
    page = client.search_posts(username, phrase, archive=older, around=date_hint)
    if time_page is not None:
        return resolve_original_page(merge_search_pages(time_page, page), screenshot)
    resolved = resolve_original_page(page, screenshot)
    if resolved["status"] == "matched":
        return resolved
    if page["posts"] and page.get("next_token") and (resolved["status"] == "candidate" or not bounds):
        next_page = client.search_posts(username, phrase, archive=older, around=date_hint, next_token=page["next_token"])
        return resolve_original_page(merge_search_pages(page, next_page, paginated=True), screenshot)
    if bounds:
        time_page = search_author_in_window(client, username, bounds, allow_archive, now)
        if time_page is not None:
            timed_result = resolve_original_page(time_page, screenshot)
            if timed_result["status"] == "matched":
                return timed_result
            return resolve_original_page(merge_search_pages(time_page, page), screenshot)
    if not page["posts"] and not date_hint and allow_archive:
        page = client.search_posts(username, phrase, archive=True)
        return resolve_original_page(page, screenshot)
    return resolved


def find_original(client, screenshot, link, allow_archive, received_at=None):
    if not link and (not screenshot or not screenshot["username"]):
        return original_result("unconfirmed", "请补发原帖链接，或发一张能看清 @账号的截图。")
    if not client:
        return original_result("unconfirmed", "本机尚未配置可用的 X Bearer Token。")
    try:
        if link:
            return compare(client.get_post(link), screenshot)
        now = datetime.now(timezone.utc)
        bounds = post_time_window(screenshot, received_at or now)
        if screenshot.get("translated_only"):
            return find_translated_original(client, screenshot, bounds, allow_archive, now)
        return find_untranslated_original(client, screenshot, bounds, allow_archive, now)
    except HTTPError as exc:
        exc.close()
        reason = HTTP_ERRORS.get(exc.code, "X API 暂时返回异常。")
        return original_result("unconfirmed", f"X 查询失败（HTTP {exc.code}）。{reason}")
    except (URLError, TimeoutError):
        return original_result("unconfirmed", "连接 X 超时或失败，独立事实查证可以继续。")
    except (XApiError, TypeError, AttributeError, KeyError, ValueError):
        return original_result("unconfirmed", "X 没有返回可读取的公开原帖。")


def public_source(value):
    try:
        url = urlsplit(clean_text(value, 700))
        host = url.hostname or ""
        if (
            url.scheme not in {"https", "http"} or "." not in host
            or url.username is not None or url.password is not None or url.port not in {None, 80, 443}
            or host.endswith((".local", ".internal", ".localhost"))
            or any(host == x or host.endswith("." + x) for x in POST_HOSTS)
            or any(char.isspace() for char in url.geturl())
        ):
            return ""
        try:
            if not ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    except ValueError:
        return ""


def profile_website(profile):
    entities = profile.get("entities")
    entry = entities.get("url") if isinstance(entities, dict) else None
    urls = entry.get("urls") if isinstance(entry, dict) else None
    candidates = []
    if isinstance(urls, list):
        for item in urls:
            if isinstance(item, dict):
                candidates.extend((item.get("unwound_url"), item.get("expanded_url")))
    candidates.append(profile.get("url"))
    for candidate in candidates:
        url = public_source(candidate)
        if url and urlsplit(url).hostname not in {"t.co", "www.t.co"}:
            return url
    return ""


def has_profile_link(content, username):
    path = f"/{username.casefold()}"
    for match in LINK.finditer(clean_text(content, 80000)):
        try:
            url = urlsplit(match[0].rstrip(".,;!)]"))
            if (
                url.hostname in POST_HOSTS and url.username is None and url.password is None
                and url.port in {None, 80, 443} and url.path.rstrip("/").casefold() == path
            ):
                return True
        except ValueError:
            continue
    return False


def account_reference(client, username, website):
    if not client:
        return "", False
    # Use at most one web request. A reference is only counted if the page links to this profile.
    try:
        if website:
            page = client.fetch(website, timeout=8)
            if has_profile_link(page.get("content"), username):
                return website, True
        else:
            for page in client.search(f'"{username}" "x.com/{username}"', timeout=8):
                if not isinstance(page, dict):
                    continue
                url = public_source(page.get("url"))
                if url and has_profile_link(page.get("content"), username):
                    return url, False
    except DeepSeekError:
        pass
    return "", False


def assess_account(x_client, deepseek, screenshot, original, previous):
    username = (screenshot or {}).get("username") or original.get("username")
    handle = USERNAME.fullmatch(clean_text(username, 64))
    if not handle:
        return {"level": "unknown", "reason": "暂未识别到完整 @账号。"}
    username = handle[1]
    prior = previous.get("account") if isinstance(previous, dict) else None
    if isinstance(prior, dict) and prior.get("level") != "unknown" and prior.get("username", "").casefold() == username.casefold():
        return prior

    result = {"level": "unknown", "username": username}
    profile = original.get("author_profile") or {}
    try:
        if clean_text(profile.get("username"), 64).casefold() != username.casefold():
            if not x_client:
                return result | {"reason": "本机尚未配置可用的 X Bearer Token。"}
            profile = x_client.get_user(username)
        if clean_text(profile.get("username"), 64).casefold() != username.casefold():
            raise XApiError("账号不一致。")
    except HTTPError as exc:
        exc.close()
        message = HTTP_ERRORS.get(exc.code) if exc.code in {401, 402, 429} else "暂未取得公开账号资料。"
        return result | {"reason": f"X 账号查询失败（HTTP {exc.code}）。{message}"}
    except (URLError, TimeoutError):
        return result | {"reason": "连接 X 超时或失败。"}
    except (XApiError, ValueError, AttributeError, TypeError):
        return result | {"reason": "暂未查到该 @账号的公开资料。"}

    username = profile["username"]
    badge_type = clean_text(profile.get("verified_type"), 32).lower()
    badge = {
        "business": "金标（X 机构认证）",
        "government": "灰标（X 政府或多边机构类认证）",
        "blue": "蓝标（X Premium）",
    }.get(badge_type, "")
    if not badge and profile.get("verified") is True:
        badge = "X 显示已认证，类型未明"
    identity = profile.get("is_identity_verified") is True
    parody = profile.get("parody") is True
    website = profile_website(profile)
    reference, reciprocal = account_reference(deepseek, username, website) if not parody else ("", False)
    metrics = profile.get("public_metrics")
    followers = metrics.get("followers_count") if isinstance(metrics, dict) else None
    created = clean_text(profile.get("created_at"), 32)[:10]
    level = "limited"
    if parody:
        level = "parody"
    elif reciprocal:
        level = "linked"
    elif identity or badge_type in {"business", "government"}:
        level = "platform"
    return {
        "level": level, "username": username, "badge": badge, "identity_verified": identity,
        "created_at": created if read_date(created) else "",
        "followers": followers if type(followers) is int and followers >= 0 else None,
        "description": LINK.sub("", clean_text(profile.get("description"), 120)).strip(),
        "reference": reference, "reciprocal": reciprocal,
    }


def search_query(value):
    value = LINK.sub("", value)
    return " ".join(re.sub(r'[:()"\\]', " ", value).split())[:128]


def fetch_sources(client, query):
    results = [hit for hit in client.search(query) if isinstance(hit, dict)]
    sources = []
    visited = set()
    remaining = 2
    for item in results:
        url = public_source(item.get("url"))
        if not url or url in visited:
            continue
        visited.add(url)
        remaining -= 1
        page = item
        content = clean_text(page.get("content"), 14000)
        if len(content) < 80:
            try:
                page = client.fetch(url)
                content = clean_text(page.get("content"), 14000)
            except DeepSeekError as exc:
                if exc.status not in {403, 404} and not sources:
                    raise
        # Match text samples, so identical syndicated reports do not count twice.
        copy = any(
            SequenceMatcher(None, normalize(source["content"][:1500]), normalize(content[:1500])).ratio() > 0.87
            for source in sources
        )
        if len(content) >= 80 and not copy:
            sources.append({
                "id": str(len(sources) + 1), "url": url, "content": content,
                "title": LINK.sub("", clean_text(page.get("title") or item.get("title"), 110)),
            })
        if remaining == 0:
            break
    return sources


def validate_grade(result, grading, sources):
    result = result | {"sources": []}
    citations = grading.get("citations")
    if not isinstance(citations, list):
        citations = []
    requested = {
        citation["source_id"] for citation in citations[:6]
        if isinstance(citation, dict) and isinstance(citation.get("source_id"), str)
    }
    verified = set()
    for source in sources:
        for citation in citations[:6]:
            if not isinstance(citation, dict) or citation.get("source_id") != source["id"]:
                continue
            quote = clean_text(citation.get("quote"), 350)
            if significant_text(quote) and normalize(quote) in normalize(source["content"]):
                verified.add(source["id"])
                result["sources"].append({
                    "id": source["id"], "url": source["url"], "title": source["title"], "quote": quote,
                })
                break
    verdict = grading.get("verdict")
    if verdict == "insufficient" or (verdict in ("supported", "refuted") and verified and requested == verified):
        reason = LINK.sub("", clean_text(grading.get("reason"), 200)).strip()
        result.update(verdict=verdict, reason=reason or result["reason"])
    elif verdict in ("supported", "refuted"):
        result["reason"] = "未能在网页正文中核对结论对应的引用。"
    return result


def check_claim(client, item, *, grader=None, reviewer=None, review_deadline=None):
    claim = item["claim"]
    base = {"claim": claim, "verdict": "insufficient", "reason": "未取得可核对的独立证据。", "sources": []}
    if not client:
        return base | {"reason": "尚未配置可用的网页取证 API。"}
    query = search_query(item["query"]) or search_query(claim)
    if not query:
        return base
    try:
        sources = fetch_sources(client, query)
    except DeepSeekError as exc:
        return base | {"reason": str(exc)}
    if not sources:
        return base

    evidence = [{key: value for key, value in source.items() if key != "url"} for source in sources]
    try:
        grading = (grader or client).grade(claim, evidence)
        result = validate_grade(base, grading, sources)
        needs_review = (
            grading.get("review_needed") is True or grading.get("verdict") not in ("supported", "refuted", "insufficient")
            or grading.get("verdict") == "refuted" or grading.get("verdict") != result["verdict"]
        )
    except DeepSeekError as exc:
        result = base | {"reason": str(exc)}
        needs_review = True
    if not reviewer or not needs_review:
        return result

    timeout = min(12, review_deadline - monotonic()) if review_deadline is not None else 12
    if timeout < 1:
        return base | {"reason": "初判需要复核，本次处理时间不足。", "sources": result["sources"]}
    try:
        reviewed = validate_grade(base, reviewer.grade(claim, evidence, timeout=timeout, review=True), sources)
    except DeepSeekError:
        return base | {"reason": "初判需要复核，复核接口暂未完成。", "sources": result["sources"]}
    if {result["verdict"], reviewed["verdict"]} == {"supported", "refuted"}:
        return reviewed | {"verdict": "insufficient", "reason": "初判和复核对同一批证据得出相反结论，暂无法确认。"}
    return reviewed


def display_line(text):
    # Keep model and web text from becoming gateway reply or media directives.
    return " ".join(LINK.sub("", text).split()).replace("[[", "［［").replace("MEDIA:", "MEDIA：")


def render_account(account):
    label = {
        "unknown": "⬜ 资料不足",
        "limited": "🟨 可信度证据有限",
        "platform": "🟦 有 X 平台身份线索",
        "linked": "🟩 账号与所填网站互相链接",
        "parody": "🟧 X 标记为戏仿类账号",
    }[account["level"]]
    lines = [f"【账号可信度】{label}"]
    if account.get("username"):
        lines.append(f"@{account['username']}")
    if account["level"] == "unknown":
        return "\n".join(lines + [account["reason"]])
    details = []
    if account["badge"]:
        details.append(account["badge"])
    if account["identity_verified"]:
        details.append("X 显示已验证身份")
    if account["created_at"]:
        details.append(f"注册于 {account['created_at']}")
    if account["followers"] is not None:
        details.append(f"{account['followers']:,} 位关注者")
    if details:
        lines.append("｜".join(details))
    if account["description"]:
        lines.append(f"自填简介 {display_line(account['description'])}")
    if account["reference"]:
        lines.append("所填网站有回链" if account["reciprocal"] else "站外网页有提及该账号的记录")
    lines.append("以上依据反映账号身份和背景。帖内说法另看事实核验。")
    return "\n".join(lines)


def render_fact_summary(facts):
    total = len(facts)
    if not total:
        return ["贴文证据支持度 暂无法评估"]

    counts = Counter(fact["verdict"] for fact in facts)
    supported, refuted = counts["supported"], counts["refuted"]
    resolved = supported + refuted
    pending = total - resolved
    if not resolved:
        headline = "贴文证据支持度 暂无法评估"
    else:
        low = round(100 * supported / total)
        high = round(100 * (supported + pending) / total)
        support = f"{low}%–{high}%" if pending else f"{low}%"
        headline = f"贴文证据支持度 {support}（本次核验 {total} 条说法）"

    coverage = round(100 * resolved / total)
    return [
        headline,
        f"证据覆盖率 {coverage}%（{resolved}/{total}）｜支持 {supported} · 反驳 {refuted} · 待核实 {pending}",
    ]


def render(original, facts, fact_note, candidate, image):
    root = f"【原帖核验】{ORIGINAL_LABELS[original['status']]}\n{original['reason']}"
    if image:
        root += "\n附 1 张候选帖的 X 官方嵌入页截图。"
    elif candidate:
        root += "\n候选帖暂时无法截图。"
        excerpt = display_line(clean_text(candidate.get("text"), 180))
        if excerpt:
            root += f"\n候选正文节选：{excerpt}"
    elif original.get("username"):
        root += f"\n@{original['username']}"
        if original.get("published_at"):
            root += f" · {original['published_at']}"
    facts_lines = ["【事实核验】", *render_fact_summary(facts)]
    for number, fact in enumerate(facts, 1):
        claim = display_line(fact["claim"])
        reason = display_line(fact["reason"])
        facts_lines.append(f"{number}. {FACT_LABELS[fact['verdict']]}｜{claim}\n{reason}")
        for source in fact["sources"]:
            host = (urlsplit(source["url"]).hostname or "").replace(".", "．")
            title = display_line(source["title"]) or host or "网页证据"
            facts_lines.append(f"来源 {source['id']} {title}")
    if fact_note:
        facts_lines.append(fact_note)
    return root, "\n".join(facts_lines)


def verify(payload, x_client, deepseek, allow_archive=True, *, reader=None, reviewer=None, vision_reviewer=None):
    started = monotonic()
    if reader is None and hasattr(deepseek, "extract"):
        reader = deepseek
    received_at = datetime.now(timezone.utc)
    text = clean_text(payload.get("text"), 4000)
    link = original_link(text)
    path = clean_text(payload.get("image_path"), 4096)
    previous = payload.get("previous") if not path and link else None
    screenshot = previous.get("screenshot") if isinstance(previous, dict) else None
    facts = previous.get("facts", []) if isinstance(previous, dict) else []
    fact_note = previous.get("fact_note", "") if isinstance(previous, dict) else ""
    if path:
        try:
            if not reader:
                raise DeepSeekError("本机尚未配置可用的识图 AI API Key。")
            translation_hint = normalize(LINK.sub("", text)).strip("。！：") in TRANSLATION_CAPTIONS
            screenshot = extract_screenshot(
                reader, path, received_at, translation_hint=translation_hint, repair=not link, vision_reviewer=vision_reviewer,
            )
            if not screenshot["is_x_post"]:
                screenshot = None
                fact_note = "未识别为 X 帖子截图，请发送包含帖子正文的截图。"
            elif screenshot["ambiguous_target"]:
                screenshot = None
                fact_note = "图中有多条可能的目标帖，请发一张只含目标帖的截图。"
        except (ValueError, DeepSeekError) as exc:
            fact_note = str(exc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        original_task = executor.submit(find_original, x_client, screenshot, link, allow_archive, received_at)
        tasks = []
        if path and screenshot:
            tasks = [
                executor.submit(check_claim, deepseek, item, grader=reader, reviewer=reviewer, review_deadline=started + 92)
                for item in screenshot["claims"]
            ]
        original = original_task.result()
        account_task = executor.submit(assess_account, x_client, deepseek, screenshot, original, previous)
        candidate = original.get("candidate")
        directory = clean_text(payload.get("preview_dir"), 4096)
        # The WeChat gateway stops the worker after 100 seconds.
        budget = min(12, 92 - (monotonic() - started))
        image = capture_candidate(candidate.get("id"), directory, budget=budget) if candidate and directory and budget > 0 else ""
        if path and screenshot:
            facts = [task.result() for task in tasks]
            if not facts:
                fact_note = "截图里没有识别到清楚、可核验的事实性说法。"
        account = account_task.result()
    original.pop("author_profile", None)
    original.pop("candidate", None)
    if not facts and not fact_note:
        fact_note = "发截图后可以核验帖里的具体说法。"
    original_text, facts_text = render(original, facts, fact_note, candidate, image)
    account_text = render_account(account)
    return {
        "screenshot": screenshot, "original": original, "account": account, "facts": facts, "fact_note": fact_note,
        "original_text": original_text, "facts_text": facts_text, "candidate_images": [image] if image else [],
        "reply": f"{original_text}\n\n{account_text}\n\n{facts_text}",
    }


def client_or_none(client_type, key):
    try:
        return client_type(key) if key else None
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description="接收截图并核验 X 原帖与事实说法。")
    parser.add_argument("--stdin", action="store_true", help="从标准输入接收 UTF-8 JSON")
    parser.add_argument("--image", help="本地截图路径")
    parser.add_argument("--link", default="", help="可选的公开 X 原帖链接")
    parser.add_argument("--check-config", action="store_true", help="检查 AI 配置，无网络请求")
    args = parser.parse_args()
    if args.check_config:
        from diagnostics import check_ai_config
        return check_ai_config(os.environ)
    try:
        payload = json.loads(sys.stdin.buffer.read(128 * 1024 + 1)) if args.stdin else {
            "image_path": args.image, "text": args.link,
        }
        if not isinstance(payload, dict):
            raise ValueError("请输入核验对象。")
        x_client = client_or_none(XClient, os.environ.get("X_BEARER_TOKEN"))
        clients = clients_from_env(os.environ)
        result = verify(payload, x_client, allow_archive=os.environ.get("X_VALIDATOR_ARCHIVE", "1") != "0", **clients)
        result["request_counts"] = {
            "x": x_client.request_count if x_client else 0,
            "model": sum(clients[role].request_count for role in ("reader", "reviewer", "vision_reviewer") if clients[role]),
            "web": clients["deepseek"].request_count if clients["deepseek"] else 0,
        }
        print(json.dumps(result, ensure_ascii=True))
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
        # Internal details can contain user data. Only the exception class is logged.
        print(f"X Validator failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
