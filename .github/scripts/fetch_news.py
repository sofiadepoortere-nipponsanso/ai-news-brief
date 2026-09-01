"""
Daily AI News Brief
--------------------
Fetches real RSS feeds, scores items by relevance to approved company tools
and AI policy topics, picks the top 5, writes docs/index.html (served by
GitHub Pages), and posts a summary to a Microsoft Teams channel webhook.

No AI model is used here on purpose: every headline, link, and summary is
taken verbatim from the source feed, so there is nothing to hallucinate.
"""

import os
import re
import html
import feedparser
import requests
from datetime import datetime, timezone
from time import mktime

# ---------------------------------------------------------------------------
# CONFIG — edit this section to tune the app
# ---------------------------------------------------------------------------

# Verify each of these loads real XML in a browser before trusting it.
# I cannot check these live from here — some may need replacing.
FEEDS = {
    "Anthropic": "https://www.anthropic.com/news/rss.xml",
    "Microsoft AI Blog": "https://blogs.microsoft.com/ai/feed/",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
}

# Keywords for your approved tools — matches are case-insensitive.
TOOL_KEYWORDS = [
    "copilot", "work iq", "heygen", "claude", "anthropic", "google ai studio",
]

# Keywords for general AI policy/regulation news relevant to a company using AI tools.
POLICY_KEYWORDS = [
    "regulation", "policy", "ai act", "executive order", "antitrust",
    "lawsuit", "legislation", "compliance", "export control", "ban",
]

MAX_ITEM_AGE_HOURS = 60   # how far back to look
TOP_N = 5

# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------

def fetch_all_items():
    items = []
    for source_name, url in FEEDS.items():
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"WARNING: failed to fetch {source_name} ({url}): {e}")
            continue

        if parsed.bozo and not parsed.entries:
            print(f"WARNING: {source_name} feed did not parse cleanly, skipping. "
                  f"Check the URL: {url}")
            continue

        for entry in parsed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", entry.get("description", ""))
            summary = re.sub("<[^<]+?>", "", summary_raw).strip()  # strip HTML tags

            published_dt = None
            if entry.get("published_parsed"):
                published_dt = datetime.fromtimestamp(
                    mktime(entry.published_parsed), tz=timezone.utc
                )
            elif entry.get("updated_parsed"):
                published_dt = datetime.fromtimestamp(
                    mktime(entry.updated_parsed), tz=timezone.utc
                )

            if not title or not link:
                continue

            items.append({
                "source": source_name,
                "title": title,
                "link": link,
                "summary": summary[:280],
                "published": published_dt,
            })
    return items


# ---------------------------------------------------------------------------
# SCORE + RANK
# ---------------------------------------------------------------------------

def score_item(item, now):
    text = f"{item['title']} {item['summary']}".lower()

    matched_tools = [k for k in TOOL_KEYWORDS if k in text]
    matched_policy = [k for k in POLICY_KEYWORDS if k in text]

    score = 0
    category = "General AI News"

    if matched_tools:
        score += 100
        category = "Approved Tool News"
    elif matched_policy:
        score += 50
        category = "AI Policy/Regulation"

    if item["published"]:
        hours_ago = (now - item["published"]).total_seconds() / 3600
        if hours_ago > MAX_ITEM_AGE_HOURS:
            return None  # too old, drop entirely
        score += max(0, MAX_ITEM_AGE_HOURS - hours_ago) * 0.5
    else:
        hours_ago = None
        score += 5  # small neutral bonus for undated items so they aren't auto-excluded

    item["score"] = score
    item["category"] = category
    item["hours_ago"] = hours_ago
    return item


def dedupe(items):
    seen_links = set()
    seen_titles = set()
    unique = []
    for item in items:
        norm_title = re.sub(r"\W+", "", item["title"].lower())
        if item["link"] in seen_links or norm_title in seen_titles:
            continue
        seen_links.add(item["link"])
        seen_titles.add(norm_title)
        unique.append(item)
    return unique


def get_top_items():
    now = datetime.now(timezone.utc)
    raw_items = fetch_all_items()
    scored = [score_item(i, now) for i in raw_items]
    scored = [i for i in scored if i is not None]
    scored = dedupe(scored)
    scored.sort(key=lambda i: i["score"], reverse=True)
    return scored[:TOP_N]


# ---------------------------------------------------------------------------
# BUILD STATIC SITE
# ---------------------------------------------------------------------------

def build_html(top_items, generated_at):
    rows = ""
    for idx, item in enumerate(top_items, start=1):
        published_str = (
            item["published"].strftime("%Y-%m-%d %H:%M UTC")
            if item["published"] else "Date unavailable"
        )
        rows += f"""
        <div class="item">
          <div class="rank">#{idx}</div>
          <div class="content">
            <span class="badge">{html.escape(item['category'])}</span>
            <h2><a href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['title'])}</a></h2>
            <p>{html.escape(item['summary'])}</p>
            <div class="meta">Source: {html.escape(item['source'])} — {published_str}</div>
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Daily AI News Brief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; background:#fafafa; color:#1a1a1a; }}
  h1 {{ font-size: 1.6rem; }}
  .generated {{ color:#666; font-size:0.85rem; margin-bottom: 24px; }}
  .item {{ display:flex; gap:16px; background:white; border-radius:10px; padding:16px 20px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .rank {{ font-size:1.4rem; font-weight:700; color:#888; min-width:36px; }}
  .badge {{ display:inline-block; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.03em; background:#eef2ff; color:#3730a3; padding:2px 8px; border-radius:999px; margin-bottom:6px; }}
  h2 {{ font-size:1.05rem; margin:4px 0; }}
  h2 a {{ color:#1a1a1a; text-decoration:none; }}
  h2 a:hover {{ text-decoration:underline; }}
  p {{ font-size:0.9rem; color:#444; margin:6px 0; }}
  .meta {{ font-size:0.78rem; color:#888; }}
</style>
</head>
<body>
  <h1>Daily AI News Brief</h1>
  <div class="generated">Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} — top {len(top_items)} stories, ranked by relevance and recency. Every item links to its original source.</div>
  {rows if top_items else '<p>No qualifying news found in this run.</p>'}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# TEAMS NOTIFICATION
# ---------------------------------------------------------------------------

def post_to_teams(top_items, pages_url):
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        print("No TEAMS_WEBHOOK_URL set — skipping Teams notification.")
        return

    lines = [f"**{i+1}. [{item['title']}]({item['link']})** — {item['category']}"
              for i, item in enumerate(top_items)]
    text_body = "\n\n".join(lines) if lines else "No qualifying news found today."

    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": "Daily AI News Brief",
        "themeColor": "0076D7",
        "title": "📰 Daily AI News Brief — Top Stories",
        "text": text_body,
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "View full site",
                "targets": [{"os": "default", "uri": pages_url}],
            }
        ] if pages_url else [],
    }

    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code >= 300:
        print(f"WARNING: Teams webhook returned {resp.status_code}: {resp.text}")
    else:
        print("Posted to Teams successfully.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    top_items = get_top_items()

    repo = os.environ.get("GITHUB_REPOSITORY", "")  # format: owner/repo
    pages_url = ""
    if "/" in repo:
        owner, repo_name = repo.split("/", 1)
        pages_url = f"https://{owner}.github.io/{repo_name}/"

    site_html = build_html(top_items, now)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(site_html)

    print(f"Wrote docs/index.html with {len(top_items)} items.")
    for item in top_items:
        print(f" - [{item['category']}] {item['title']} ({item['source']})")

    post_to_teams(top_items, pages_url)


if __name__ == "__main__":
    main()
