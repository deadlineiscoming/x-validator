"""Read public posts for X Validator."""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import timedelta
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


API_URL = "https://api.x.com/2"
POST_ID = re.compile(r"[0-9]{1,19}")
POST_PATH = re.compile(r"/(?:[A-Za-z0-9_]+/status|i/web/status)/([0-9]{1,19})(?:/(?:photo|video)/[0-9]+)?/?")
POST_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
USERNAME = re.compile(r"@?([A-Za-z0-9_]{1,15})")
AUTHOR_FIELDS = (
    "username,created_at,description,url,entities,public_metrics,verified,verified_type,"
    "is_identity_verified,parody"
)
HTTP_ERRORS = {
    400: "请求参数无效。请检查帖子 ID 或搜索词。",
    401: "请检查 X App-only Bearer Token。",
    402: "请检查 X Developer Console 的 API 额度。",
    403: "请检查开发者应用对该接口或帖子的访问权限。",
    404: "该帖子无法从 X 读取，可能已删除或受访问限制。",
    429: "请求频率已达上限。请稍后再试。",
}


class XApiError(Exception):
    pass


def parse_post_id(value):
    value = value.strip()
    if POST_ID.fullmatch(value):
        return value

    url = urlsplit(value)
    match = POST_PATH.fullmatch(url.path)
    if url.scheme in {"https", "http"} and url.hostname in POST_HOSTS and match:
        return match[1]
    raise ValueError("请输入 X 原帖链接或帖子 ID。")


def post_details(post, users):
    post_id = post["id"]
    author = users.get(post.get("author_id"), {})
    username = author.get("username")
    author_path = f"{username}/status" if username else "i/web/status"
    note = post.get("note_post") or post.get("note_tweet") or {}
    return {
        "id": post_id,
        "url": f"https://x.com/{author_path}/{post_id}",
        "username": username,
        "author_profile": author,
        "published_at": post.get("created_at"),
        "text": note.get("text") or post.get("text"),
        "url_entities": [
            url for record in (post, note) for url in ((record.get("entities") or {}).get("urls") or [])
            if isinstance(url, dict)
        ],
        "edit_history_ids": post.get("edit_history_post_ids", post.get("edit_history_tweet_ids", [])),
    }


class XClient:
    def __init__(self, bearer_token):
        self.bearer_token = bearer_token.strip()
        if not self.bearer_token or not re.fullmatch(r"[!-~]+", self.bearer_token):
            raise ValueError("请输入完整的 App-only Bearer Token，不含 Bearer 前缀。")
        self.request_count = 0
        self._request_lock = Lock()

    def get_post(self, link_or_id):
        post_id = parse_post_id(link_or_id)
        payload = self._request(f"/tweets/{post_id}")
        post = payload.get("data")
        if not post:
            raise XApiError("X 未返回可读取的公开原帖。")
        return post_details(post, self._authors(payload))

    def get_user(self, username):
        username = self._username(username)
        payload = self._get(f"/users/by/username/{username}", {"user.fields": AUTHOR_FIELDS}, timeout=8)
        user = payload.get("data")
        if not isinstance(user, dict) or not user.get("username"):
            raise XApiError("X 未返回可读取的公开账号资料。")
        return user

    def search_posts(self, username, phrase, archive=False, around=None, next_token=None):
        username = self._username(username)
        phrase = " ".join(phrase.replace('"', " ").replace("\\", " ").split())
        query = f'from:{username} "{phrase}" -is:retweet'
        if not phrase or len(query) > 512:
            raise ValueError("请输入一小段原文作为搜索词。")

        scope = "all" if archive else "recent"
        options = {}
        if archive and around:
            # A screenshot's displayed date can differ from the post's UTC date.
            options = {
                "start_time": f"{around - timedelta(days=1)}T00:00:00Z",
                "end_time": f"{around + timedelta(days=2)}T00:00:00Z",
            }
        if next_token:
            options["next_token"] = next_token
        return self._search_page(scope, query, "relevancy", **options)

    def search_author_posts(self, username, start_time, end_time, archive=False):
        query = f"from:{self._username(username)} -is:retweet"
        scope = "all" if archive else "recent"
        return self._search_page(
            scope, query, "recency",
            start_time=start_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
            end_time=end_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    @staticmethod
    def _username(value):
        match = USERNAME.fullmatch(value.strip())
        if not match:
            raise ValueError("请输入有效的 X 用户名。")
        return match[1]

    def _search_page(self, scope, query, order, **options):
        payload = self._request(f"/tweets/search/{scope}", query=query, max_results=10, sort_order=order, **options)
        users = self._authors(payload)
        next_token = payload.get("meta", {}).get("next_token")
        return {
            "posts": [post_details(post, users) for post in payload.get("data", [])],
            "has_more": bool(next_token),
            "next_token": next_token,
        }

    @staticmethod
    def _authors(payload):
        return {user["id"]: user for user in payload.get("includes", {}).get("users", [])}

    def _request(self, path, **params):
        # X's lookup guide and endpoint reference use different names for these fields.
        params = {"expansions": "author_id", "user.fields": AUTHOR_FIELDS} | params
        fields = {"tweet.fields": "created_at,author_id,note_tweet,entities"}
        try:
            return self._get(path, fields | params)
        except HTTPError as exc:
            if exc.code == 400:
                with exc:
                    error_body = exc.read().decode("utf-8", errors="replace")
                if "tweet.fields" in error_body or "note_tweet" in error_body:
                    fields = {"post.fields": "created_at,note_post,entities"}
                    return self._get(path, fields | params)
            else:
                exc.close()
            raise

    def _get(self, path, params, *, timeout=15):
        query = urlencode(params)
        request = Request(f"{API_URL}{path}?{query}", headers={"Accept": "application/json"})
        request.add_unredirected_header("Authorization", f"Bearer {self.bearer_token}")
        with self._request_lock:
            self.request_count += 1
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)

        if not isinstance(payload, dict):
            raise XApiError("X 返回了无法识别的响应。")
        if not payload.get("data") and payload.get("errors"):
            raise XApiError("X 未返回所查询的公开数据。")
        return payload


def main():
    parser = argparse.ArgumentParser(description="查询公开 X 帖子。")
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("lookup", help="按链接或 ID 读取原帖")
    lookup.add_argument("post", help="原帖链接或帖子 ID")
    search = commands.add_parser("search", help="按账号和原文片段搜索")
    search.add_argument("username", help="X 用户名")
    search.add_argument("phrase", help="截图里的一小段原文")
    search.add_argument("--archive", action="store_true", help="搜索全量存档，默认搜索最近 7 天")
    args = parser.parse_args()

    try:
        token = os.environ.get("X_BEARER_TOKEN")
        if not token:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise XApiError("请在交互终端输入 Token，或设置 X_BEARER_TOKEN 环境变量。")
            token = getpass.getpass("请输入 X App-only Bearer Token ")
        client = XClient(token)
        if args.command == "lookup":
            result = client.get_post(args.post)
            result.pop("author_profile")
            result.pop("url_entities")
        else:
            result = client.search_posts(args.username, args.phrase, args.archive)
            result.pop("next_token")
            for post in result["posts"]:
                post.pop("author_profile")
                post.pop("url_entities")
    except HTTPError as exc:
        exc.close()
        detail = HTTP_ERRORS.get(exc.code, "请检查 X API 服务状态后重试。")
        print(f"X API 返回 HTTP {exc.code}。{detail}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError):
        print("无法连接 api.x.com。请检查运行机器的网络并重试。", file=sys.stderr)
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        print("X 返回了无法识别的响应。", file=sys.stderr)
        return 1
    except (ValueError, XApiError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130

    # JSON stays readable on UTF-8 terminals and valid under legacy Windows encodings.
    encoding = sys.stdout.encoding or "utf-8"
    print(json.dumps(result, ensure_ascii=encoding.lower() != "utf-8", indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
