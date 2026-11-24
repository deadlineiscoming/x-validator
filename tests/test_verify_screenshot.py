import unittest
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import URLError

from verify_screenshot import assess_account, find_original, normalize_extraction, post_time_window, render_fact_summary, verify
from x_validator import AUTHOR_FIELDS, XClient


POST = {
    "id": "1460323737035677698",
    "url": "https://x.com/example/status/1460323737035677698",
    "username": "example",
    "text": "The Apollo 11 mission was the first crewed Moon landing in 1969.",
    "published_at": "2026-01-02T00:00:00Z",
    "edit_history_ids": [],
}
CLAIM = "Apollo 11 was the first crewed Moon landing."
QUOTE = "Apollo 11 was the first crewed Moon landing in history."
SOURCE = "https://example.org/history/apollo"


class LocalX:
    def __init__(self, posts=None):
        self.posts = posts or [POST]
        self.scopes = []

    def get_post(self, link):
        return POST

    def search_posts(self, username, phrase, archive=False, around=None, next_token=None):
        self.scopes.append((archive, around, next_token))
        return {"posts": self.posts, "has_more": False}

    def search_author_posts(self, username, start_time, end_time, archive=False):
        self.scopes.append((username, start_time, end_time, archive))
        return {"posts": self.posts, "has_more": True}


class LocalDeepSeek:
    def __init__(self, quote=QUOTE, verdict="supported"):
        self.quote = quote
        self.verdict = verdict
        self.extractions = 0

    def extract(self, path):
        self.extractions += 1
        return {
            "is_x_post": True, "username": "@example", "visible_text": POST["text"],
            "date": "2026-01-02", "search_phrase": "Apollo 11 mission was the first crewed Moon landing",
            "claims": [{"claim": CLAIM, "query": "Apollo 11 landing"}],
        }

    def search(self, query):
        return [
            {"url": "https://127.0.0.1/private", "title": "Discarded"},
            {"url": "https://x.com/some/status/123", "title": "Original forum"},
            {"url": SOURCE, "title": "Flight history"},
        ]

    def fetch(self, url):
        if url != SOURCE:
            raise AssertionError("Fetched a non-independent or non-public source")
        return {"content": f"Mission archives and records. {QUOTE} The crew returned to Earth in July."}

    def grade(self, claim, sources):
        return {
            "verdict": self.verdict, "reason": "The records support the claim.",
            "citations": [{"source_id": "1", "quote": self.quote}],
        }


class VerifyScreenshotTests(unittest.TestCase):
    def test_matching_post_and_verified_evidence_are_separate(self):
        result = verify({"image_path": "fixture.png", "text": POST["url"]}, LocalX(), LocalDeepSeek())
        self.assertEqual(result["original"]["status"], "matched")
        self.assertEqual(result["facts"][0]["verdict"], "supported")
        self.assertIn("【原帖核验】🟢", result["reply"])
        self.assertIn("贴文证据支持度 100%", result["reply"])
        self.assertIn("🟢 有证据支持", result["facts_text"])
        self.assertIn("Flight history", result["reply"])
        self.assertNotIn("https://", result["reply"])

    def test_fabricated_citation_downgrades_verdict(self):
        deepseek = LocalDeepSeek(quote="An invented passage not found in the source.")
        result = verify({"image_path": "fixture.png"}, LocalX(), deepseek)
        self.assertEqual(result["facts"][0]["verdict"], "insufficient")
        self.assertIn("🟡 证据不足", result["facts_text"])
        self.assertIn("贴文证据支持度 暂无法评估", result["facts_text"])
        self.assertNotIn(SOURCE, result["reply"])

    def test_fact_summary_distinguishes_pending_claims_from_refuted_claims(self):
        cases = (
            (("supported", "refuted", "insufficient"), "33%–67%", "67%（2/3）"),
            (("supported", "insufficient", "insufficient"), "33%–100%", "33%（1/3）"),
            (("refuted",), "0%", "100%（1/1）"),
            (("insufficient",), "暂无法评估", "0%（0/1）"),
        )
        for verdicts, support, coverage in cases:
            with self.subTest(verdicts=verdicts):
                headline, detail = render_fact_summary([{"verdict": verdict} for verdict in verdicts])
                self.assertIn(f"贴文证据支持度 {support}", headline)
                self.assertIn(f"证据覆盖率 {coverage}", detail)

        self.assertEqual(render_fact_summary([]), ["贴文证据支持度 暂无法评估"])

    def test_x_timeout_does_not_stop_fact_checking(self):
        class UnreachableX(LocalX):
            def get_post(self, link):
                raise URLError(TimeoutError())

        result = verify({"image_path": "fixture.png", "text": POST["url"]}, UnreachableX(), LocalDeepSeek())
        self.assertEqual(result["original"]["status"], "unconfirmed")
        self.assertEqual(result["facts"][0]["verdict"], "supported")

    def test_account_reuses_author_profile_and_checks_a_reciprocal_link(self):
        website = "https://example.org/organization"
        profile = {
            "username": "example", "verified_type": "business", "url": "https://t.co/short",
            "entities": {"url": {"urls": [{"expanded_url": website}]}},
        }
        x_client = Mock()
        x_client.get_post.return_value = POST | {"author_profile": profile}
        deepseek = Mock()
        deepseek.fetch.return_value = {"content": "Follow us on [X](https://x.com/EXAMPLE)."}

        result = verify({"text": POST["url"]}, x_client, deepseek)

        x_client.get_user.assert_not_called()
        deepseek.fetch.assert_called_once_with(website, timeout=8)
        deepseek.search.assert_not_called()
        self.assertEqual(result["account"]["level"], "linked")
        self.assertIn("【账号可信度】🟩", result["reply"])
        self.assertIn("所填网站有回链", result["reply"])
        self.assertNotIn("https://", result["reply"])
        self.assertNotIn("author_profile", result["original"])

    def test_unmatched_translation_still_checks_the_screenshot_account(self):
        x_client = Mock()
        x_client.get_user.return_value = {
            "username": "example", "verified_type": "blue", "public_metrics": {"followers_count": 2000000},
        }
        deepseek = Mock()
        deepseek.extract.return_value = LocalDeepSeek().extract("fixture.png") | {
            "translated_only": True, "date": "", "claims": [],
        }
        deepseek.search.return_value = [{"url": SOURCE, "content": "https://x.com/example/status/123"}]

        result = verify({"image_path": "fixture.png"}, x_client, deepseek)

        self.assertEqual(result["original"]["status"], "unconfirmed")
        x_client.search_author_posts.assert_not_called()
        x_client.get_user.assert_called_once_with("example")
        deepseek.search.assert_called_once()
        deepseek.fetch.assert_not_called()
        self.assertEqual(result["account"]["level"], "limited")
        self.assertIn("【账号可信度】🟨", result["reply"])
        self.assertFalse(result["account"]["reference"])
        self.assertIn("蓝标", result["reply"])

    def test_account_uses_screenshot_handle_when_supplied_link_has_another_author(self):
        x_client = Mock()
        x_client.get_user.return_value = {"username": "example", "parody": True}
        deepseek = Mock()
        screenshot = {"username": "example"}
        original = {"username": "unrelated", "author_profile": {"username": "unrelated", "verified_type": "government"}}

        result = assess_account(x_client, deepseek, screenshot, original, previous=None)
        self.assertEqual(result["level"], "parody")
        self.assertEqual(result["username"], "example")
        self.assertEqual(assess_account(x_client, deepseek, screenshot, original, {"account": result}), result)
        x_client.get_user.assert_called_once_with("example")
        deepseek.search.assert_not_called()

    def test_ambiguous_archive_matches_are_candidates(self):
        deepseek = LocalDeepSeek()
        screenshot = normalize_extraction(deepseek.extract("fixture.png") | {"date": "2007-01-01"})
        post = POST | {"published_at": "2007-01-01T10:00:00Z"}
        client = LocalX([post, post | {"id": "2"}])
        result = find_original(client, screenshot, "", allow_archive=True)
        self.assertEqual(client.scopes[0], (True, date(2007, 1, 1), None))
        self.assertEqual(len(client.scopes), 2)
        self.assertEqual(result["status"], "candidate")

    def test_ambiguous_posts_capture_only_the_first_candidate(self):
        posts = [POST | {"id": str(index), "url": f"https://x.com/example/status/{index}"} for index in range(4)]
        with TemporaryDirectory() as path, patch("verify_screenshot.capture_candidate", return_value="candidate-1.png") as capture:
            result = verify({"image_path": "fixture.png", "preview_dir": path}, LocalX(posts), LocalDeepSeek())
        capture.assert_called_once_with("0", path, budget=12)
        self.assertEqual(result["candidate_images"], ["candidate-1.png"])
        self.assertIn("【原帖核验】🟡", result["reply"])
        self.assertNotIn("https://", result["reply"])
        self.assertIn("附 1 张候选帖的 X 官方嵌入页截图", result["reply"])
        self.assertNotIn("candidate", result["original"])

    def test_capture_failure_displays_only_one_candidate_excerpt(self):
        second_post = "Another possible match."
        posts = [POST, POST | {"id": "2", "text": f"{POST['text']} {second_post}"}]
        with TemporaryDirectory() as path, patch("verify_screenshot.capture_candidate", return_value=""):
            result = verify({"image_path": "fixture.png", "preview_dir": path}, LocalX(posts), LocalDeepSeek())
        self.assertEqual(result["candidate_images"], [])
        self.assertIn("候选帖暂时无法截图", result["reply"])
        self.assertIn(POST["text"], result["reply"])
        self.assertNotIn(second_post, result["reply"])

    def test_search_checks_one_more_page_when_x_returns_a_cursor(self):
        class PaginatedX(LocalX):
            def search_posts(self, username, phrase, archive=False, around=None, next_token=None):
                self.scopes.append((archive, around, next_token))
                if next_token:
                    return {"posts": [], "has_more": False}
                return {"posts": [POST], "has_more": True, "next_token": "more"}

        client = PaginatedX()
        screenshot = normalize_extraction(LocalDeepSeek().extract("fixture.png"))
        self.assertEqual(find_original(client, screenshot, "", allow_archive=True)["status"], "matched")
        self.assertEqual(client.scopes[-1][-1], "more")
        self.assertEqual(len(client.scopes), 2)

    def test_explicit_time_finds_original_without_searching_ocr_phrase(self):
        moment = datetime(2026, 1, 2, 8, 14).astimezone(timezone.utc)
        post = POST | {"published_at": moment.isoformat()}
        screenshot = normalize_extraction({
            "is_x_post": True, "username": "@example", "date": "2026-01-02", "clock": "08:14",
            "visible_text": f"{POST['text']} 显示更多",
        })
        client = Mock()
        client.search_author_posts.return_value = {
            "posts": [post | {"id": "2", "text": "Unrelated post."}, post], "has_more": False,
        }

        result = find_original(client, screenshot, "", allow_archive=True)

        client.search_author_posts.assert_called_once_with(
            "example", moment - timedelta(minutes=5), moment + timedelta(minutes=5), archive=True,
        )
        client.search_posts.assert_not_called()
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["url"], POST["url"])

    def test_embedded_thread_with_ambiguous_header_rereads_the_whole_target_post(self):
        moment = datetime(2026, 1, 2, 8, 14).astimezone(timezone.utc)
        deepseek = Mock()
        deepseek.extract.side_effect = [
            {"is_x_post": True, "embedded_layout": True, "date": "2026-01-02", "username": "@parent", "visible_text": "The context post"},
            {"is_x_post": True, "embedded_layout": True, "date": "2026-01-02", "clock": "08:14", "username": "@example", "visible_text": POST["text"]},
        ]
        client = Mock()
        client.search_author_posts.return_value = {
            "posts": [POST | {"published_at": moment.isoformat()}], "has_more": False,
        }

        result = verify({"image_path": "fixture.png"}, client, deepseek)

        self.assertEqual(deepseek.extract.call_count, 2)
        deepseek.extract.assert_called_with("fixture.png", focus_embed=True)
        self.assertEqual(client.search_author_posts.call_args.args[0], "example")
        self.assertEqual(result["screenshot"]["visible_text"], POST["text"])
        self.assertEqual(result["original"]["status"], "matched")

    def test_empty_phrase_search_uses_author_and_visible_date(self):
        screenshot = normalize_extraction(LocalDeepSeek().extract("fixture.png"))
        client = Mock()
        client.search_posts.return_value = {"posts": [], "has_more": False}
        client.search_author_posts.return_value = {"posts": [POST], "has_more": False}

        result = find_original(client, screenshot, "", allow_archive=True)

        client.search_posts.assert_called_once()
        client.search_author_posts.assert_called_once()
        self.assertEqual(result["status"], "matched")

    def test_empty_narrow_time_window_uses_original_text(self):
        screenshot = normalize_extraction(LocalDeepSeek().extract("fixture.png") | {"clock": "11:42"})
        client = Mock()
        client.search_author_posts.return_value = {"posts": [], "has_more": False}
        client.search_posts.return_value = {"posts": [POST], "has_more": False}

        result = find_original(client, screenshot, "", allow_archive=True)

        self.assertEqual([call[0] for call in client.method_calls], ["search_author_posts", "search_posts"])
        self.assertEqual(result["status"], "matched")

    def test_later_link_reuses_previous_fact_check(self):
        deepseek = LocalDeepSeek()
        first = verify({"image_path": "fixture.png"}, None, deepseek)
        result = verify({"text": POST["url"], "previous": first}, LocalX(), deepseek)
        self.assertEqual(result["original"]["status"], "matched")
        self.assertEqual(result["facts"], first["facts"])
        self.assertEqual(deepseek.extractions, 1)

    def test_edited_post_difference_is_explained(self):
        deepseek = LocalDeepSeek()
        screenshot = normalize_extraction(deepseek.extract("fixture.png"))
        client = LocalX()
        client.get_post = lambda link: POST | {"text": "The latest edit.", "edit_history_ids": ["1", "2"]}
        result = find_original(client, screenshot, POST["url"], allow_archive=True)
        self.assertEqual(result["status"], "different")
        self.assertIn("编辑记录", result["reason"])

    def test_displayed_link_matches_the_x_post_only_for_known_aliases(self):
        client = XClient("offline-token")
        url = "https://t.co/Origin"
        payload = {
            "data": {
                "id": POST["id"], "author_id": "123", "text": f"{POST['text']} {url} https://t.co/Photo",
                "entities": {"urls": [
                    {"url": url, "display_url": "bit.ly/A1B", "expanded_url": "https://bit.ly/A1B"},
                    {"url": "https://t.co/Photo", "display_url": "pic.x.com/Photo"},
                ]},
            },
            "includes": {"users": [{"id": "123", "username": "example"}]},
        }
        with patch.object(client, "_get", return_value=payload) as request:
            for link, expected in (
                ("bit.ly/\nA1B", "matched"),
                ("bit.ly/A1B", "matched"),
                ("https://bit.ly/A1B", "matched"),
                ("bit.ly/a1b", "different"),
                ("bit.ly/A1B?redirect=other", "different"),
                ("other.example/bit.ly/A1B", "different"),
            ):
                with self.subTest(link=link):
                    screenshot = normalize_extraction({
                        "is_x_post": True, "username": "@example", "visible_text": f"{POST['text']} {link}",
                    })
                    result = find_original(client, screenshot, POST["url"], True)
                    self.assertEqual(result["status"], expected)
        self.assertIn("entities", request.call_args.args[1]["tweet.fields"].split(","))

    def test_translation_searches_one_page_by_author_and_relative_time(self):
        client = LocalX([POST | {
            "id": "2", "url": "https://x.com/example/status/2", "text": "Something unrelated.",
        }, POST])
        screenshot = normalize_extraction({
            "is_x_post": True, "username": "@example", "visible_text": "阿波罗11号是首次载人登月任务。",
            "relative_time": "2h", "translated_only": True, "back_translation": POST["text"],
        })
        received = datetime(2026, 1, 2, 2, 0, tzinfo=timezone.utc)
        result = find_original(client, screenshot, "", allow_archive=True, received_at=received)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["url"], POST["url"])
        self.assertEqual(result["candidate"]["id"], POST["id"])
        self.assertEqual(len(client.scopes), 1)
        self.assertEqual(client.scopes[0], (
            "example", datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc), True,
        ))

    def test_translation_without_time_does_not_issue_broad_search(self):
        client = LocalX()
        screenshot = normalize_extraction({
            "is_x_post": True, "username": "@example", "visible_text": "阿波罗11号是首次载人登月任务。",
            "translated_only": True,
        })
        result = find_original(client, screenshot, "", allow_archive=True)
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(client.scopes, [])

    def test_truncated_handle_can_be_completed_from_a_same_author_quote(self):
        raw = {
            "is_x_post": True,
            "username": "@ExampleAu…",
            "quoted_username": "@ExampleAuthor",
            "quote_same_author": True,
        }
        self.assertEqual(normalize_extraction(raw)["username"], "ExampleAuthor")
        self.assertEqual(normalize_extraction(raw | {"quoted_username": "@ExampleOther"})["username"], "")
        self.assertEqual(normalize_extraction(raw | {"quote_same_author": False})["username"], "")
        self.assertEqual(normalize_extraction(raw | {"username": "@different"})["username"], "different")

    def test_translated_screenshot_retries_an_unreadable_header_before_searching_x(self):
        deepseek = Mock()
        deepseek.extract.return_value = {
            "is_x_post": True, "username": "@Example...", "translated_only": True, "embedded_layout": True,
            "visible_text": "阿波罗11号完成首次载人登月。", "back_translation": POST["text"],
        }
        deepseek.extract_header.return_value = {
            "username": "@Example…", "quoted_username": "@ExampleAuthor", "quote_same_author": True,
            "date": "2026-01-02",
        }
        x_client = LocalX([POST | {"username": "ExampleAuthor"}])

        result = verify({"image_path": "fixture.png"}, x_client, deepseek)

        deepseek.extract.assert_called_once_with("fixture.png")
        deepseek.extract_header.assert_called_once_with("fixture.png")
        self.assertEqual(result["screenshot"]["username"], "ExampleAuthor")
        self.assertEqual(x_client.scopes[0][0], "ExampleAuthor")
        self.assertEqual(result["original"]["status"], "candidate")

    def test_header_retry_does_not_replace_a_visible_handle_prefix_with_another_author(self):
        deepseek = Mock()
        deepseek.extract.return_value = {
            "is_x_post": True, "username": "@Example...", "visible_text": POST["text"],
        }
        deepseek.extract_header.return_value = {"username": "@OtherAuthor"}
        x_client = LocalX()

        result = verify({"image_path": "fixture.png"}, x_client, deepseek)

        self.assertEqual(result["screenshot"]["username"], "")
        self.assertEqual(result["original"]["status"], "unconfirmed")
        self.assertEqual(x_client.scopes, [])

    def test_relative_post_time_uses_earlier_capture_clock_when_visible(self):
        received = datetime(2026, 11, 24, 14, 52).astimezone()
        screenshot = normalize_extraction({
            "is_x_post": True, "capture_clock": "10:25", "relative_time": "2 小时",
        })
        start, end = post_time_window(screenshot, received)
        estimate = datetime(2026, 11, 24, 8, 25).astimezone(timezone.utc)
        self.assertEqual(start, estimate - timedelta(minutes=90))
        self.assertEqual(end, estimate + timedelta(minutes=90))

    def test_link_identifies_original_without_literally_comparing_translation(self):
        screenshot = normalize_extraction({
            "is_x_post": True, "username": "@example", "visible_text": "阿波罗11号是首次载人登月任务。",
            "translated_only": True,
        })
        result = find_original(LocalX(), screenshot, POST["url"], allow_archive=True)
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["url"], POST["url"])

    def test_author_search_sends_time_bounds_and_no_phrase(self):
        client = XClient("offline-token")
        start = datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc)
        with patch.object(client, "_request", return_value={}) as request:
            client.search_author_posts("@Example", start, end, archive=True)
        request.assert_called_once_with(
            "/tweets/search/all", query="from:Example -is:retweet", max_results=10,
            sort_order="recency", start_time="2026-01-01T22:30:00Z", end_time="2026-01-02T01:30:00Z",
        )

    def test_user_lookup_requests_author_fields_without_post_expansions(self):
        client = XClient("offline-token")
        profile = {"username": "example", "verified_type": "business"}
        with patch.object(client, "_get", return_value={"data": profile}) as request:
            self.assertEqual(client.get_user("@example"), profile)
        request.assert_called_once_with("/users/by/username/example", {"user.fields": AUTHOR_FIELDS}, timeout=8)
