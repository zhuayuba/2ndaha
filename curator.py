#!/usr/bin/env python3
"""
二手顿悟 - 策展推送系统
从 RSS 策展源抓取内容 → LLM 筛选 → Lark 推送 → GitHub Pages 归档
"""

import json
import os
import sys
import hashlib
import re
import html as html_mod
import random
from datetime import datetime
from pathlib import Path
from collections import Counter

import feedparser
import requests
from openai import OpenAI

# ── 路径 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen.json"
ISSUES_PATH = BASE_DIR / "issues.json"
HTML_PATH = BASE_DIR / "docs" / "index.html"

# ── 配置 ──────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        print("❌ 缺少 config.json")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def init_llm_client(cfg):
    llm = cfg["llm"]
    kwargs = {"api_key": llm["api_key"]}
    if llm.get("base_url"):
        kwargs["base_url"] = llm["base_url"]
    return OpenAI(**kwargs), llm["model"]


# ── RSS 抓取 ──────────────────────────────────────

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_all_feeds(sources: dict) -> list[dict]:
    all_posts = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ErShouDunWu/1.0)"}

    for name, url in sources.items():
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            for entry in feed.entries:
                title = clean_html(entry.get("title", ""))
                link = entry.get("link", "")
                summary = clean_html(entry.get("summary", entry.get("description", "")))
                published = entry.get("published", entry.get("updated", ""))

                if not title or not link:
                    continue

                # 摘要取 600 字给 LLM 判断
                content = summary[:600] if summary else title

                all_posts.append({
                    "id": hashlib.md5(link.encode()).hexdigest(),
                    "title": title,
                    "link": link,
                    "content": content,
                    "source": name,
                    "published": published,
                })

            print(f"  ✅ {name}: {len(feed.entries)} 篇")

        except Exception as e:
            print(f"  ⚠️ {name}: 抓取失败 ({e})")

    return all_posts


# ── 去重 ──────────────────────────────────────────

def load_seen() -> set:
    if SEEN_PATH.exists():
        with open(SEEN_PATH, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(ids: set):
    with open(SEEN_PATH, "w") as f:
        json.dump(list(ids), f)


def filter_new_posts(posts: list[dict], seen: set) -> list[dict]:
    new = [p for p in posts if p["id"] not in seen]
    print(f"\n📊 共抓取 {len(posts)} 篇，其中 {len(new)} 篇为新内容")
    return new


# ── LLM 筛选 ──────────────────────────────────────

SYSTEM_PROMPT = """你是一位「二手顿悟」的编辑。你的任务是从英文内容中筛选出能让人「读完停顿两秒」的东西，并用中文重述。

# 什么值得收录

收录的内容有一个共同点：它重新框定了你熟悉的东西，让你从今以后看它的方式变了。

可以是：
- 一个反直觉的事实（但不仅仅是"冷知识"，它必须让人联想到更大的东西）
- 一个你默认知道但从未见人写出来的规律
- 一个把你一直在模糊感觉到的东西说清楚了的洞察
- 科技如何反过来重塑人的行为、关系、认知方式——工具不仅解决问题，也在改变使用工具的人

不收录：
- 抽象的人生哲理（"孤独是力量"、"温柔是爱"——太虚了）
- 纯信息增量而没有重新框定（"某年某月发生了某事"）
- 励志/鸡汤/正确废话
- 需要读原文才能理解的碎片
- 只有作者个人情境才有意义的流水账

# 重述要求

关键原则：**不要概括成道理。概括会杀死洞察。**

✅ 正确示范（保留了原文的具体性）：
标题：「社交媒体不是廉价版社交——它是社交的反面」
正文：「我们一直以为社交媒体是社交生活的低配替代品——方便但质量差。但实际它不是在模拟社交，而是在消解社交。真正的社交让你和他人建立连接；社交媒体训练你把他人变成观众，把互动变成表演。你用完社交媒体后感觉更孤独，不是因为你'没真的在社交'，而是因为它反向运行了你的社交本能。」
评语：从此你刷完手机后的空虚有了一个准确的名字

❌ 错误示范（概括成了抽象道理）：
标题：「社交的本质是连接」
正文：「真正的社交是与他人建立深层联系，而非表面的互动。」
评语：珍惜真实关系

——上面这个错误示范就是典型的"把洞察熬成了鸡汤"。不要这样做。

# 输出格式

对每篇候选内容，返回 JSON：
{"收录": true/false, "标题": "中文标题，保留原文的具体性和反直觉感（20字以内）", "正文": "用2-4句话重述核心洞察。保留原文的具体细节、例子、数据。不要概括成道理。让读者读完能联想到自己的生活场景。（80-150字）", "评语": "一句话，帮助读者理解为什么这值得停顿（15字以内）"}

注意：
- 标题一定要有具体信息，不能是"关于XX的思考"这种万能标题
- 如果原文只是有趣但没到「重新框定认知」的程度，不收录
- 质量绝对优先。不合格就全部返回 false
- 最多收录 2-3 条/批"""


def filter_with_llm(posts: list[dict], client: OpenAI, model: str, max_picks: int) -> list[dict]:
    if not posts:
        return []

    batch_size = 8
    all_picks = []

    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]

        candidates = ""
        for j, p in enumerate(batch):
            candidates += f"[{j + 1}] 来源: {p['source']}\n标题: {p['title']}\n内容: {p['content'][:400]}\n\n"

        user_msg = f"以下是候选内容，请逐一判断是否收录：\n\n{candidates}\n请以 JSON 数组格式返回结果。只收录真正让你'停顿两秒'的内容。质量优先。"

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
            if json_match:
                results = json.loads(json_match.group(0))
            else:
                results = json.loads(raw)

            for j, result in enumerate(results):
                if isinstance(result, dict) and result.get("收录"):
                    if j < len(batch):
                        batch[j]["zh_title"] = result.get("标题", batch[j]["title"])
                        batch[j]["zh_body"] = result.get("正文", "")
                        batch[j]["zh_comment"] = result.get("评语", "")
                        all_picks.append(batch[j])
                        print(f"  ✨ 收录: {batch[j]['zh_title']}")

        except Exception as e:
            print(f"  ⚠️ LLM 筛选出错 (batch {i // batch_size + 1}): {e}")
            continue

    # 每源最多 2 条，保证多样性
    source_count = Counter()
    capped_picks = []
    for p in all_picks:
        src = p["source"]
        if source_count[src] < 2:
            capped_picks.append(p)
            source_count[src] += 1

    if len(all_picks) != len(capped_picks):
        dropped = len(all_picks) - len(capped_picks)
        print(f"  🔻 因每源上限去重 {dropped} 条")

    return capped_picks[:max_picks]


# ── 格式化（Lark 推送用） ─────────────────────────

def format_lark_message(picks: list[dict], issue_num: int, site_url: str = "") -> str:
    today = datetime.now().strftime("%Y.%m.%d")
    lines = [
        f"🪵 **二手顿悟 · Vol.{issue_num}**",
        f"_{today}_",
        "",
    ]

    for i, p in enumerate(picks, 1):
        lines.append(f"---")
        lines.append(f"**{p['zh_title']}**")
        lines.append(f"{p['zh_body']}")
        lines.append(f"_{p['zh_comment']}_")
        lines.append(f"🔗 [来源：{p['source']}]({p['link']})")

    if site_url:
        lines.append("")
        lines.append(f"📖 [在网页中查看全部往期]({site_url})")

    return "\n".join(lines)


# ── Lark 推送 ─────────────────────────────────────

def push_to_lark(markdown: str, webhook_url: str):
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🪵 二手顿悟"},
                "template": "wathet",
            },
            "elements": [
                {"tag": "markdown", "content": markdown}
            ],
        },
    }

    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code == 200:
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ Lark 推送成功")
        else:
            print(f"⚠️ Lark 返回异常: {result}")
    else:
        print(f"❌ Lark 推送失败: HTTP {resp.status_code} {resp.text[:200]}")


# ── HTML 归档 ─────────────────────────────────────

def load_issues() -> list[dict]:
    if ISSUES_PATH.exists():
        with open(ISSUES_PATH, "r") as f:
            return json.load(f)
    return []


def save_issues(issues: list[dict]):
    with open(ISSUES_PATH, "w") as f:
        json.dump(issues, f, ensure_ascii=False, indent=2)


def generate_html(issues: list[dict]):
    os.makedirs(HTML_PATH.parent, exist_ok=True)

    issue_cards = ""
    for issue in reversed(issues):
        entries_html = ""
        for entry in issue["entries"]:
            entries_html += f"""
            <article class="entry">
                <h3>{html_mod.escape(entry['zh_title'])}</h3>
                <p class="body">{html_mod.escape(entry['zh_body'])}</p>
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

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>二手顿悟</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
                         "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif;
            line-height: 1.8;
            color: #2c3e50;
            background: #fafaf8;
            max-width: 700px;
            margin: 0 auto;
            padding: 48px 24px 80px;
        }}
        .site-title {{
            font-size: 2em;
            font-weight: 800;
            letter-spacing: -0.02em;
            margin-bottom: 6px;
            color: #1a1a1a;
        }}
        .site-desc {{
            font-size: 1em;
            color: #999;
            margin-bottom: 56px;
        }}
        .issue {{
            margin-bottom: 64px;
        }}
        .issue-header {{
            display: flex;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 24px;
            padding-bottom: 10px;
            border-bottom: 1px solid #e8e8e4;
        }}
        .issue-number {{
            font-weight: 700;
            font-size: 1em;
            color: #1a1a1a;
        }}
        .issue-header time {{
            font-size: 0.85em;
            color: #bbb;
        }}
        .entry {{
            margin-bottom: 36px;
            padding-left: 18px;
            border-left: 2px solid #e8e8e4;
        }}
        .entry h3 {{
            font-size: 1.15em;
            font-weight: 700;
            margin-bottom: 8px;
            color: #1a1a1a;
            line-height: 1.5;
        }}
        .entry .body {{
            font-size: 0.95em;
            color: #444;
            margin-bottom: 8px;
            line-height: 1.8;
        }}
        .entry .comment {{
            font-size: 0.85em;
            color: #aaa;
            margin-bottom: 8px;
            font-style: italic;
        }}
        .entry .source {{
            font-size: 0.8em;
            color: #bbb;
            text-decoration: none;
            border-bottom: 1px dotted #ddd;
        }}
        .entry .source:hover {{
            color: #555;
            border-bottom-color: #999;
        }}
        footer {{
            margin-top: 80px;
            padding-top: 24px;
            border-top: 1px solid #e8e8e4;
            font-size: 0.8em;
            color: #ccc;
            text-align: center;
        }}
        @media (max-width: 480px) {{
            body {{ padding: 24px 16px 60px; }}
            .site-title {{ font-size: 1.6em; }}
            .entry {{ padding-left: 12px; }}
            .entry h3 {{ font-size: 1.05em; }}
            .entry .body {{ font-size: 0.9em; }}
        }}
    </style>
</head>
<body>
    <h1 class="site-title">🪵 二手顿悟</h1>
    <p class="site-desc">那些值得你停下来想两秒的东西。</p>

    {issue_cards}

    <footer>
        由机器策展，人工筛选。每天 2 分钟读完，可能花一整天消化。
    </footer>
</body>
</html>"""

    with open(HTML_PATH, "w") as f:
        f.write(html)
    print(f"📄 网页已更新: {HTML_PATH}")


# ── Git ───────────────────────────────────────────

def git_commit_and_push():
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, cwd=BASE_DIR
        )
        if result.returncode != 0:
            print("ℹ️ 尚未设置 Git 远程仓库，跳过推送")
            return

        subprocess.run(["git", "add", "docs/index.html", "issues.json"], capture_output=True, cwd=BASE_DIR)
        today = datetime.now().strftime("%Y-%m-%d")
        subprocess.run(["git", "commit", "-m", f"📝 二手顿悟更新 {today}"], capture_output=True, cwd=BASE_DIR)
        subprocess.run(["git", "push"], capture_output=True, cwd=BASE_DIR)
        print("🚀 已推送到 GitHub")
    except Exception as e:
        print(f"⚠️ Git 推送失败: {e}")


# ── 主流程 ────────────────────────────────────────

def main():
    print("🪵 二手顿悟 · 策展推送系统")
    print("=" * 50)

    cfg = load_config()
    client, model = init_llm_client(cfg)
    print(f"🤖 LLM: {model}")

    print("\n📡 抓取策展源...")
    posts = fetch_all_feeds(cfg["feeds"]["sources"])

    seen = load_seen()
    new_posts = filter_new_posts(posts, seen)

    if not new_posts:
        print("📭 没有新内容，结束。")
        return

    max_picks = cfg["push"]["posts_per_issue"]
    print(f"\n🧠 LLM 筛选中（目标 ≤{max_picks} 条）...")
    random.shuffle(new_posts)  # 打乱顺序，防止同源内容扎堆
    picks = filter_with_llm(new_posts, client, model, max_picks)

    for p in new_posts:
        seen.add(p["id"])
    save_seen(seen)

    if not picks:
        print("📭 本期没有内容通过筛选，不推送。")
        return

    issues = load_issues()
    issue_num = len(issues) + 1

    markdown = format_lark_message(picks, issue_num, cfg.get("site_url", ""))
    print(f"\n📤 推送到 Lark...")
    print(markdown)
    print()

    push_to_lark(markdown, cfg["lark_webhook_url"])

    today = datetime.now().strftime("%Y-%m-%d")
    issues.append({
        "number": issue_num,
        "date": today,
        "entries": [
            {
                "zh_title": p["zh_title"],
                "zh_body": p["zh_body"],
                "zh_comment": p["zh_comment"],
                "source": p["source"],
                "link": p["link"],
            }
            for p in picks
        ],
    })
    save_issues(issues)
    generate_html(issues)

    git_commit_and_push()

    print(f"\n✨ 完成！本期推送 {len(picks)} 条")


if __name__ == "__main__":
    main()
