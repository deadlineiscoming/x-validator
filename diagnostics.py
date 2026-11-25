"""Local configuration checks and opt-in checks with synthetic input."""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

from deepseek_client import DeepSeekError, FAST_MODEL, MODEL_PROVIDERS, REVIEW_MODEL, clients_from_env
from x_validator import HTTP_ERRORS, XApiError, XClient


ROLES = {"reader": "识图与初判", "reviewer": "文字复核", "vision_reviewer": "视觉复读", "deepseek": "网页取证"}
DEMO_IMAGE = Path(__file__).resolve().parent / "docs" / "demo" / "input.png"


def describe(clients):
    routes = {}
    for role, label in ROLES.items():
        client = clients[role]
        value = client.name if client else "未配置"
        if client and role != "deepseek":
            value = f"{value} / {client.model}"
        routes[label] = value
    return routes


def check_ai_config(env, *, require_x=False, require_review=False, snapshot=False, online=False):
    try:
        clients = clients_from_env(env)
        token = env.get("X_BEARER_TOKEN", "")
        x = XClient(token) if token and require_x else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    routes = describe(clients)
    if snapshot:
        print(json.dumps(routes, ensure_ascii=True))
        return 0
    for label, value in routes.items():
        print(f"{label}  {value}")
    if require_x:
        print(f"X App-only Bearer Token  {'已配置' if x else '未配置'}")
    if not clients["reader"] or not clients["deepseek"] or (require_x and not x):
        print("请配置识图、网页取证和所需的 X 凭证。", file=sys.stderr)
        return 1
    if require_review and not clients["reviewer"]:
        print("请配置文字复核模型的 Key。", file=sys.stderr)
        return 1
    if not online:
        print("本地配置检查通过。在线自检可检查真实接口权限和模型能力。")
        return 0
    return check_online(x, clients, archive=env.get("X_VALIDATOR_ARCHIVE", "1") != "0")


def check_online(x, clients, *, archive):
    query = "xvalidator diagnostic no matching post 17f503"
    checks = [("X 最近搜索", lambda: x.search_posts("XDevelopers", query))]
    if archive:
        checks.append(("X 全历史搜索", lambda: x.search_posts("XDevelopers", query, archive=True, around=date.today() - timedelta(days=10))))

    def check_image(model):
        result = model.extract(DEMO_IMAGE, timeout=20)
        if not isinstance(result.get("visible_text"), str) or "northbank" not in result["visible_text"].casefold():
            raise ValueError("没有读出样例图片里的 Northbank。请检查所用模型的图像输入和 JSON 输出能力。")

    def check_review():
        grade = clients["reviewer"].grade("这是配置检查。", [], timeout=15)
        if grade.get("verdict") not in {"supported", "refuted", "insufficient"}:
            raise ValueError("模型未返回预期的事实判定 JSON。")

    def check_fetch():
        page = web.fetch("https://example.com", timeout=15)
        if not isinstance(page.get("content"), str) or not page["content"].strip():
            raise ValueError("抓取接口未返回网页正文。")

    for role, label in (("reader", "识图"), ("vision_reviewer", "视觉复读")):
        if clients[role]:
            checks.append((label, lambda model=clients[role]: check_image(model)))
    if clients["reviewer"]:
        checks.append(("文字复核", check_review))
    web = clients["deepseek"]
    checks.extend([
        ("网页搜索", lambda: web.search("example.com IANA example domain", timeout=15)),
        ("网页抓取", check_fetch),
    ])

    passed = True
    for label, probe in checks:
        try:
            probe()
        except HTTPError as exc:
            exc.close()
            message = HTTP_ERRORS.get(exc.code, f"X 请求失败（HTTP {exc.code}）。")
        except (URLError, OSError):
            message = "连接超时或失败，请检查本机网络。"
        except DeepSeekError as exc:
            message = str(exc)
            if exc.status in {400, 404} and label in {"识图", "视觉复读", "文字复核"}:
                message += " 请检查模型名称、权限和结构化输出能力。"
        except (ValueError, XApiError) as exc:
            message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "接口返回了无法识别的响应。"
        else:
            print(f"通过  {label}", flush=True)
            continue
        passed = False
        print(f"失败  {label}  {message}", flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description="X Validator 配置检查")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--catalog", action="store_true")
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--online", action="store_true")
    mode.add_argument("--require-review", action="store_true")
    args = parser.parse_args()
    if args.catalog:
        catalog = {key: {"name": name, "url": url} for key, (name, url) in MODEL_PROVIDERS.items()}
        catalog["deepseek"]["models"] = {"FAST": FAST_MODEL, "REVIEW": REVIEW_MODEL}
        print(json.dumps(catalog))
        return 0
    return check_ai_config(os.environ, require_x=not args.snapshot, require_review=args.require_review, snapshot=args.snapshot, online=args.online)


if __name__ == "__main__":
    sys.exit(main())
