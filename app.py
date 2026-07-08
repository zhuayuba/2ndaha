#!/usr/bin/env python3
"""
二手顿悟 · 策展推送系统
RSS 抓取 + 主动搜索 → LLM 筛选 → Lark 推送 → GitHub Pages 归档
部署在 Railway，由 cron-job.org 每天触发 /push
"""

import json
import os
import sys
import hashlib
import re
import html as html_mod
import random
import time
from datetime import datetime
from pathlib import Path
from collections import Counter

import feedparser
import requests
from openai import OpenAI
from flask import Flask

# ── 路径 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen.json"
ISSUES_PATH = BASE_DIR / "issues.json"
HTML_PATH = BASE_DIR / "docs" / "index.html"

app = Flask(__name__)

# ── 配置 ──────────────────────────────────────────

def load_config():
    """加载配置。环境变量优先（Railway），config.json 作为本地兜底。"""
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)

    # 环境变量覆盖（Railway 部署用）
    env = os.environ
    if env.get("LARK_WEBHOOK_URL"):
        cfg["lark_webhook_url"] = env["LARK_WEBHOOK_URL"]
    if env.get("LLM_API_KEY"):
        cfg.setdefault("llm", {})
        cfg["llm"]["api_key"] = env["LLM_API_KEY"]
        cfg["llm"]["model"] = env.get("LLM_MODEL", cfg["llm"].get("model", "deepseek-chat"))
        cfg["llm"]["base_url"] = env.get("LLM_BASE_URL", cfg["llm"].get("base_url", "https://api.deepseek.com/v1"))
    if env.get("SITE_URL"):
        cfg["site_url"] = env["SITE_URL"]

    if not cfg.get("lark_webhook_url") or "请替换" in cfg.get("lark_webhook_url", ""):
        print("❌ 缺少 Lark Webhook URL，请在 config.json 或环境变量中配置")
        sys.exit(1)
    if not cfg.get("llm", {}).get("api_key") or "请替换" in cfg["llm"]["api_key"]:
        print("❌ 缺少 LLM API Key，请在 config.json 或环境变量中配置")
        sys.exit(1)

    return cfg

cfg = load_config()

# ── 工具函数 ──────────────────────────────────────

def clean_html(text: str) -> str:
    if not text: return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_seen() -> set:
    if SEEN_PATH.exists():
        with open(SEEN_PATH, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(ids: set):
    with open(SEEN_PATH, "w") as f:
        json.dump(list(ids), f)


def load_issues() -> list[dict]:
    if ISSUES_PATH.exists():
        with open(ISSUES_PATH, "r") as f:
            return json.load(f)
    return []


def save_issues(issues: list[dict]):
    with open(ISSUES_PATH, "w") as f:
        json.dump(issues, f, ensure_ascii=False, indent=2)


# ── 水管 1：RSS ───────────────────────────────────

def fetch_all_feeds(sources: dict) -> list[dict]:
    all_posts = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ErShouDunWu/1.0)"}

    for name, url in sources.items():
        try:
            resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                title = clean_html(entry.get("title", ""))
                link = entry.get("link", "")
                summary = clean_html(entry.get("summary", entry.get("description", "")))
                if not title or not link: continue
                content = summary[:600] if summary else title
                all_posts.append({
                    "id": hashlib.md5(link.encode()).hexdigest(),
                    "title": title,
                    "link": link,
                    "content": content,
                    "source": name,
                    "channel": "rss",
                })
            print(f"  ✅ RSS {name}: {len(feed.entries)} 篇")
        except Exception as e:
            print(f"  ⚠️ RSS {name}: {e}")
    return all_posts


# ── 水管 2：主动搜索 ──────────────────────────────

SEARCH_QUERIES = [
    "scientists discovered consciousness brain surprising study",
    "counterintuitive psychology study human behavior new findings",
    "alternative theory human evolution civilization new evidence",
    "fascinating discovery brain neuroscience changes everything",
    "we were wrong about psychology cognition research",
    "paradox human nature consciousness surprising truth",
    "hidden truth about reality perception mind",
    "science rewrites what we thought we knew",
]


def search_articles(num_results: int = 3) -> list[dict]:
    """用 DuckDuckGo 免费搜索不限时间的视角独特文章（不需要 API Key）"""
    try:
        from ddgs import DDGS
    except ImportError:
        print("  ⚠️ 未安装 ddgs 库，跳过搜索通道")
        return []

    all_posts = []

    # 随机选 5 个查询（每次不一样，增加多样性）
    queries = random.sample(SEARCH_QUERIES, min(5, len(SEARCH_QUERIES)))

    with DDGS() as ddgs:
        for q in queries:
            try:
                results = list(ddgs.text(q, max_results=num_results))
                for result in results:
                    title = clean_html(result.get("title", ""))
                    link = result.get("href", result.get("url", ""))
                    desc = clean_html(result.get("body", result.get("description", "")))
                    if not title or not link: continue
                    post_id = hashlib.md5(link.encode()).hexdigest()
                    all_posts.append({
                        "id": post_id,
                        "title": title,
                        "link": link,
                        "content": desc[:600] if desc else title,
                        "source": "Web Search",
                        "channel": "search",
                    })
                print(f"  ✅ 搜索: {q[:50]}... → {len(results)} 条")
            except Exception as e:
                print(f"  ⚠️ 搜索出错 ({q[:40]}...): {e}")

    return all_posts


# ── LLM 筛选 ──────────────────────────────────────

SYSTEM_PROMPT = """你是一位「二手顿悟」的编辑。你的任务是从英文内容中筛选出能以**新视角重新框定旧认知**的东西，并用中文重述。

# 什么值得收录

你筛选的，是那些让人读完「原来还能这样看」的瞬间。它不一定是刚发布的——一篇 2018 年的文章，只要能提供一个你从没想过的视角，就值得收录。

值得收录的类型：
- 一个反直觉的科学发现，让你重新理解大脑/意识/人类行为
- 一个把你一直在模糊感觉到、但从未见人清晰写出来的规律
- 一个推翻常识的视角——「你以为的其实不对」
- 科技如何反过来重塑了人的行为、关系、认知方式
- 一个简单到被忽视、但细想之后停不下来的观察
- 另类的历史/文明解读（alternative history/theory），只要它有证据支撑、能让你重新思考「我们以为的人类史可能不是唯一版本」

不收录：
- 抽象的人生哲理（"孤独是力量"、"伤你最深的往往是你最爱的人"——太虚了）
- 只有信息增量、没有视角重新框定的纯冷知识
- 励志鸡汤 / 正确废话
- 需要大量专业知识才能理解的纯学术内容
- 只有作者个人情境才有意义的流水账

# 重述要求

每条入选内容产出一段「精要概括」和一段「大白话介绍」。

精要概括（50-80字）：用2-3句话把最核心的洞察说清楚。读者5秒内读完、判断要不要继续。保留原文的反直觉感和具体性。

大白话介绍（200-400字）：用3-5段话，像朋友在饭桌上给你讲「我最近读了一篇特别有意思的文章」那样，把原文的内容讲清楚。包括：原文提出了什么问题/现象？用了什么证据或例子？推翻了什么常识？得出了什么有趣结论？不要概括成道理，不要学术腔。保留原文中让人「哇」的细节。

❌ 错误示范（太抽象）：
标题：「社交的本质是连接」
精要概括：「真正的社交是与他人建立深层联系。」
大白话介绍：「在当今快节奏的社会中，人与人之间的连接变得越来越重要……」

✅ 正确示范：
标题：「社交媒体不是廉价版社交——它是社交的反面」
精要概括：「我们一直以为社交媒体是社交的低配替代品，但实际它不是在模拟社交，而是在反向消解社交。你刷完手机后觉得更孤独，不是因为你没社交——而是社交本能被反向运行了。」
大白话介绍：「原文作者讲了一个自己的故事：二十年前，他在保龄球馆和朋友聚会，有人告诉他'Facebook上有很多你的照片'——他还没注册账号，就已经成了这个网站的内容。一年后所有人都用了Facebook。一开始感觉很好，能跟更多人保持联系。但几年后他发现不对劲：社交媒体不是社交的'便宜版'，它根本就是社交的'负数版'。真正的社交是你们肉体坐在同一个空间里，用脸和声音和心交流；社交媒体是你一个人对着屏幕，把朋友变成观众。他不是在反对技术——他只是给'刷完手机后的空虚'取了一个精确的名字：那不是孤独，那是社交能力被反向消耗后的疲惫。」

# 输出格式

对每篇候选内容返回 JSON：
{"收录": true/false, "标题": "中文标题，保留原文的具体性和反直觉感（20字以内）", "精要概括": "2-3句话，核心洞察。读者5秒判断要不要继续读。（50-80字）", "大白话介绍": "3-5段大白话。像朋友在饭桌上讲给你听那样。包括原文的核心问题、证据例子、有趣细节、反常识结论。（200-400字）", "评语": "一句话（15字以内）"}

注意：
- 标题必须有具体信息，不能是「关于XX的思考」
- 大白话介绍不是翻译，是用你的话重述，保留让人「哇」的细节
- 质量绝对优先。不合格就全部返回 false
- 最多收录 2-3 条/批"""


def filter_with_llm(posts: list[dict], max_per_source: int = 2) -> list[dict]:
    if not posts:
        return []

    llm = cfg["llm"]
    client = OpenAI(api_key=llm["api_key"], base_url=llm.get("base_url"))
    model = llm["model"]

    batch_size = 8
    all_picks = []

    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]
        candidates = ""
        for j, p in enumerate(batch):
            channel_tag = "🔍搜索" if p.get("channel") == "search" else "📡RSS"
            candidates += f"[{j + 1}] {channel_tag} 来源: {p['source']}\n标题: {p['title']}\n内容: {p['content'][:400]}\n\n"

        user_msg = f"以下是候选内容，请逐一判断是否收录：\n\n{candidates}\n请以 JSON 数组格式返回结果。只收录真正提供了新视角的内容。质量优先。"

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content.strip()
            json_match = re.search(r"\[.*\]", raw, re.DOTALL)
            results = json.loads(json_match.group(0)) if json_match else json.loads(raw)

            for j, result in enumerate(results):
                if isinstance(result, dict) and result.get("收录"):
                    if j < len(batch):
                        batch[j]["zh_title"] = result.get("标题", batch[j]["title"])
                        batch[j]["zh_summary"] = result.get("精要概括", "")
                        batch[j]["zh_body"] = result.get("大白话介绍", "")
                        batch[j]["zh_comment"] = result.get("评语", "")
                        all_picks.append(batch[j])
                        print(f"  ✨ 收录: {batch[j]['zh_title']}")
        except Exception as e:
            print(f"  ⚠️ LLM 出错 (batch {i // batch_size + 1}): {e}")
            continue

    # 每源最多 N 条
    source_count = Counter()
    capped = []
    for p in all_picks:
        src = p["source"]
        if source_count[src] < max_per_source:
            capped.append(p)
            source_count[src] += 1
    if len(all_picks) != len(capped):
        print(f"  🔻 每源上限去重 {len(all_picks) - len(capped)} 条")

    return capped


# ── 生成内容 ──────────────────────────────────────

def generate_issues(num_issues: int = 3, posts_per_issue: int = 2):
    """从 RSS + 搜索抓取内容，LLM 筛选，生成 N 期"""
    print(f"\n{'='*50}")
    print(f"🏭 工厂模式：生成 {num_issues} 期（每期 {posts_per_issue} 条）")
    print(f"{'='*50}")

    # 水管 1：RSS
    print("\n📡 水管 1：RSS 抓取...")
    rss_posts = fetch_all_feeds(cfg["feeds"]["sources"])

    # 水管 2：主动搜索（DuckDuckGo，免费，无需 API Key）
    print("\n🔍 水管 2：主动搜索...")
    search_posts = search_articles()

    # 合并
    all_posts = rss_posts + search_posts
    print(f"\n📊 合计: {len(all_posts)} 篇（RSS {len(rss_posts)} + 搜索 {len(search_posts)}）")

    # 去重
    seen = load_seen()
    new_posts = [p for p in all_posts if p["id"] not in seen]
    print(f"📊 去重后: {len(new_posts)} 篇新内容")

    if not new_posts:
        print("📭 无新内容")
        return []

    # 打乱（防同源扎堆）
    random.shuffle(new_posts)

    # LLM 筛选
    total_needed = num_issues * posts_per_issue
    print(f"\n🧠 LLM 筛选（目标 ≥{total_needed} 条）...")
    picks = filter_with_llm(new_posts, max_per_source=3)

    # 标记已见
    for p in new_posts:
        seen.add(p["id"])
    save_seen(seen)

    if not picks:
        print("📭 无内容通过筛选")
        return []

    # 分成 N 期
    issues = load_issues()
    start_num = len(issues) + 1
    new_issues = []

    for n in range(num_issues):
        start_idx = n * posts_per_issue
        end_idx = start_idx + posts_per_issue
        batch = picks[start_idx:end_idx]

        if not batch:
            break

        today = datetime.now().strftime("%Y-%m-%d")
        issue = {
            "number": start_num + n,
            "date": today,  # 实际发送时会被更新
            "sent": False,
            "entries": [
                {
                    "zh_title": p["zh_title"],
                    "zh_summary": p["zh_summary"],
                    "zh_body": p["zh_body"],
                    "zh_comment": p["zh_comment"],
                    "source": p["source"],
                    "link": p["link"],
                }
                for p in batch
            ],
        }
        issues.append(issue)
        new_issues.append(issue)
        print(f"\n📦 Vol.{issue['number']} ({len(batch)} 条) 已入库")

    save_issues(issues)
    print(f"\n✅ 工厂完成：生成 {len(new_issues)} 期，库存 {sum(1 for i in issues if not i['sent'])} 期")

    return new_issues


# ── 推送一期 ──────────────────────────────────────

def push_next_issue() -> bool:
    """推送下一期未发送的内容。返回是否成功推送。"""
    issues = load_issues()
    unsent = [i for i in issues if not i["sent"]]

    if not unsent:
        return False

    issue = unsent[0]
    issue["date"] = datetime.now().strftime("%Y-%m-%d")

    # 格式化 Markdown
    today = datetime.now().strftime("%Y.%m.%d")
    lines = [
        f"🪵 **二手顿悟 · Vol.{issue['number']}**",
        f"_{today}_",
        "",
    ]

    for entry in issue["entries"]:
        lines.append("---")
        lines.append(f"**{entry['zh_title']}**")
        lines.append("")
        lines.append(f"📌 {entry['zh_summary']}")
        lines.append("")
        lines.append(entry["zh_body"])
        lines.append("")
        lines.append(f"_{entry['zh_comment']}_  |  🔗 [来源：{entry['source']}]({entry['link']})")

    site_url = cfg.get("site_url", "")
    if site_url:
        lines.append("")
        lines.append(f"📖 [在网页中查看全部往期]({site_url})")

    markdown = "\n".join(lines)

    # 推 Lark
    print(f"\n📤 推送 Vol.{issue['number']}...")
    print(markdown)
    print()

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🪵 二手顿悟"},
                "template": "wathet",
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        },
    }

    try:
        resp = requests.post(cfg["lark_webhook_url"], json=payload, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                print("✅ Lark 推送成功")
            else:
                print(f"⚠️ Lark: {result}")
                return False
        else:
            print(f"❌ Lark: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Lark: {e}")
        return False

    # 标记已发送
    issue["sent"] = True
    issue["date"] = datetime.now().strftime("%Y-%m-%d")
    save_issues(issues)

    # 更新网页 & 推送 GitHub
    generate_html(issues)
    git_commit_and_push()

    return True


# ── HTML 归档 ─────────────────────────────────────

def generate_html(issues: list[dict]):
    os.makedirs(HTML_PATH.parent, exist_ok=True)

    sent_issues = [i for i in issues if i["sent"]]

    # 按月份分组
    months = {}  # {"2026-07": [issue, ...]}
    for issue in sent_issues:
        month_key = issue["date"][:7]  # "2026-07"
        if month_key not in months:
            months[month_key] = []
        months[month_key].append(issue)

    sorted_months = sorted(months.keys(), reverse=True)
    current_month = sorted_months[0] if sorted_months else ""

    # 生成侧边栏
    sidebar_html = '<ul class="month-list">'
    for mk in sorted_months:
        label = mk.replace("-", "年") + "月"
        active = ' class="active"' if mk == current_month else ""
        sidebar_html += f'<li{active}><a href="#month-{mk}">{label} <span>({len(months[mk])}期)</span></a></li>'
    sidebar_html += "</ul>"

    # 生成各月内容
    all_months_html = ""
    for mk in sorted_months:
        issues_in_month = sorted(months[mk], key=lambda i: i["number"], reverse=True)
        month_label = mk.replace("-", "年") + "月"
        issue_cards = ""
        for issue in issues_in_month:
            entries_html = ""
            for entry in issue["entries"]:
                body_id = f"body-{issue['number']}-{hashlib.md5(entry['zh_title'].encode()).hexdigest()[:6]}"
                entries_html += f"""
            <article class="entry">
                <h3>{html_mod.escape(entry['zh_title'])}</h3>
                <p class="summary">📌 {html_mod.escape(entry.get('zh_summary', ''))}</p>
                <details class="body-details">
                    <summary class="body-toggle">展开阅读</summary>
                    <div class="body">{html_mod.escape(entry['zh_body'])}</div>
                </details>
                <p class="comment">{html_mod.escape(entry['zh_comment'])}</p>
                <a href="{html_mod.escape(entry['link'])}" class="source" target="_blank" rel="noopener">
                    {html_mod.escape(entry['source'])}
                </a>
            </article>"""

            issue_cards += f"""
        <section class="issue">
            <header class="issue-header">
                <span class="issue-number">Vol.{issue['number']}</span>
                <time datetime="{issue['date']}">{issue['date']}</time>
            </header>
            {entries_html}
        </section>"""

        all_months_html += f"""
    <div class="month-section" id="month-{mk}">
        <h2 class="month-title">{month_label}</h2>
        {issue_cards}
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>二手顿悟</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif;
            line-height: 1.8; color: #2c3e50; background: #fafaf8;
        }}
        .layout {{
            display: flex; max-width: 1100px; margin: 0 auto; min-height: 100vh;
        }}
        .sidebar {{
            width: 200px; flex-shrink: 0; padding: 48px 24px 40px;
            border-right: 1px solid #e8e8e4; position: sticky; top: 0; height: 100vh;
            overflow-y: auto; background: #fafaf8;
        }}
        .sidebar .site-title {{
            font-size: 1.15em; font-weight: 800; letter-spacing: -0.02em;
            margin-bottom: 6px; color: #1a1a1a;
        }}
        .sidebar .site-desc {{
            font-size: 0.78em; color: #bbb; margin-bottom: 28px; line-height: 1.5;
        }}
        .month-list {{ list-style: none; }}
        .month-list li {{ margin-bottom: 4px; }}
        .month-list li a {{
            display: block; padding: 5px 8px; border-radius: 4px;
            font-size: 0.85em; color: #777; text-decoration: none;
            transition: background 0.15s;
        }}
        .month-list li a:hover {{ background: #eee; color: #333; }}
        .month-list li.active a {{ background: #e8e8e4; color: #1a1a1a; font-weight: 600; }}
        .month-list li a span {{ font-size: 0.8em; color: #bbb; }}
        .main {{
            flex: 1; padding: 48px 32px 80px; max-width: 720px;
        }}
        .main .site-title-mobile {{
            display: none; font-size: 1.6em; font-weight: 800;
            margin-bottom: 6px; color: #1a1a1a;
        }}
        .month-title {{
            font-size: 1.2em; font-weight: 700; color: #1a1a1a;
            margin-bottom: 32px; padding-bottom: 8px; border-bottom: 2px solid #e0e0dc;
        }}
        .issue {{ margin-bottom: 48px; }}
        .issue-header {{
            display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px;
            padding-bottom: 8px; border-bottom: 1px solid #e8e8e4;
        }}
        .issue-number {{ font-weight: 700; font-size: 0.95em; color: #1a1a1a; }}
        .issue-header time {{ font-size: 0.8em; color: #bbb; }}
        .entry {{
            margin-bottom: 32px; padding-left: 18px; border-left: 2px solid #e8e8e4;
        }}
        .entry h3 {{
            font-size: 1.1em; font-weight: 700; margin-bottom: 8px; color: #1a1a1a; line-height: 1.5;
        }}
        .entry .summary {{
            font-size: 0.9em; color: #2c3e50; margin-bottom: 8px; line-height: 1.7;
            background: #f2f2ee; padding: 10px 14px; border-radius: 4px;
        }}
        .body-details {{ margin-bottom: 8px; }}
        .body-toggle {{
            font-size: 0.85em; color: #888; cursor: pointer;
            padding: 6px 0; user-select: none;
        }}
        .body-toggle:hover {{ color: #555; }}
        .body-details[open] .body-toggle {{ color: #aaa; }}
        .entry .body {{
            font-size: 0.9em; color: #444; line-height: 1.9; margin-top: 8px;
            padding: 12px 0;
        }}
        .entry .comment {{
            font-size: 0.8em; color: #aaa; margin-bottom: 6px; font-style: italic;
        }}
        .entry .source {{
            font-size: 0.75em; color: #bbb; text-decoration: none; border-bottom: 1px dotted #ddd;
        }}
        .entry .source:hover {{ color: #555; border-bottom-color: #999; }}
        footer {{
            margin-top: 60px; padding-top: 20px; border-top: 1px solid #e8e8e4;
            font-size: 0.78em; color: #ccc; text-align: center;
        }}
        @media (max-width: 768px) {{
            .layout {{ flex-direction: column; }}
            .sidebar {{
                width: 100%; height: auto; position: static;
                border-right: none; border-bottom: 1px solid #e8e8e4;
                padding: 24px 20px 16px;
            }}
            .sidebar .month-list {{ display: flex; flex-wrap: wrap; gap: 4px; }}
            .sidebar .month-list li a {{ font-size: 0.8em; padding: 4px 8px; }}
            .main {{ padding: 32px 20px 60px; }}
            .main .site-title-mobile {{ display: none; }}
            .entry {{ padding-left: 12px; }}
            .entry h3 {{ font-size: 1em; }}
            .entry .body {{ font-size: 0.85em; }}
        }}
    </style>
</head>
<body>
    <div class="layout">
        <aside class="sidebar">
            <div class="site-title">🪵 二手顿悟</div>
            <p class="site-desc">那些值得你停下来<br>想两秒的东西。</p>
            {sidebar_html}
        </aside>
        <main class="main">
            {all_months_html}
            <footer>由机器策展。每天 2 分钟读完，可能花一整天消化。</footer>
        </main>
    </div>
</body>
</html>"""

    with open(HTML_PATH, "w") as f:
        f.write(html)
    print(f"📄 网页已更新")


# ── Git ───────────────────────────────────────────

def git_commit_and_push():
    import subprocess
    try:
        result = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, cwd=BASE_DIR)
        if result.returncode != 0:
            return
        subprocess.run(["git", "add", "docs/index.html", "issues.json"], capture_output=True, cwd=BASE_DIR)
        subprocess.run(["git", "commit", "-m", f"📝 二手顿悟更新 {datetime.now().strftime('%Y-%m-%d')}"], capture_output=True, cwd=BASE_DIR)
        subprocess.run(["git", "push"], capture_output=True, cwd=BASE_DIR)
        print("🚀 已推送到 GitHub")
    except Exception as e:
        print(f"⚠️ Git: {e}")


# ── Flask 接口 ────────────────────────────────────

@app.route("/push", methods=["POST"])
def handle_push():
    """cron-job.org 每天触发这个接口"""
    print(f"\n{'='*50}")
    print(f"⏰ 推送触发: {datetime.now().isoformat()}")
    print(f"{'='*50}")

    issues = load_issues()
    unsent_count = sum(1 for i in issues if not i["sent"])
    print(f"📦 当前库存: {unsent_count} 期")

    # 库存不足 → 触发工厂
    if unsent_count < 1:
        print("🔧 库存不足，触发生成...")
        try:
            generate_issues(num_issues=3, posts_per_issue=2)
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return {"status": "error", "message": str(e)}, 500

    # 推送下一期
    success = push_next_issue()
    if success:
        unsent = sum(1 for i in load_issues() if not i["sent"])
        return {"status": "ok", "remaining": unsent}
    else:
        return {"status": "error", "message": "推送失败"}, 500


@app.route("/generate", methods=["POST"])
def handle_generate():
    """手动触发工厂模式（调试用）"""
    try:
        new_issues = generate_issues(num_issues=3, posts_per_issue=2)
        return {"status": "ok", "generated": len(new_issues)}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route("/", methods=["GET"])
def handle_root():
    return {"status": "alive", "project": "二手顿悟"}


# ── 本地命令行模式 ────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="二手顿悟")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "push", "generate"])
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.command == "generate":
        generate_issues(num_issues=3, posts_per_issue=2)
    elif args.command == "push":
        push_next_issue()
    else:
        port = int(os.environ.get("PORT", args.port))
        print(f"🚂 二手顿悟 HTTP 服务启动: http://0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port)
