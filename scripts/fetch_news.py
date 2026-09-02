"""
Digitalisation News Brief
--------------------------
Fetches real RSS feeds, scores every item against an approximation of the
company's 8-criterion scoring rubric (see Digitalisation_Newspaper_Context
doc) for each of 6 topics, maintains a rolling 31-day history so "today /
this week / this month" views are all computable, and writes:
  - docs/index.html : tabbed site (Main + 6 topics) x (Today/Week/Month)
  - docs/feed.xml    : one-item-per-day RSS feed carrying the Main Feed's
                        daily top items, watched by Power Automate for your
                        personal Teams notification and email
  - docs/data/history.json : the rolling raw data window (needed across runs)

IMPORTANT — approximation, not the real rubric: several of the document's
8 criteria (practical actionability, internal-initiative bridge, genuine
novelty vs. a repeated announcement) require actually understanding what an
article says. This script approximates them with keyword/source/recency
heuristics. It will miss nuance a human reader (or an LLM) would catch —
see the README for that tradeoff.
"""

import os
import re
import json
import html
import feedparser
from datetime import datetime, timezone, timedelta
from time import mktime

# ---------------------------------------------------------------------------
# CONFIG — edit this section to tune the app
# ---------------------------------------------------------------------------

# Verify each of these loads real XML in a browser before trusting it —
# I cannot check these live from here.
FEEDS = {
    # Tier 1 — official / regulatory / vendor
    "European Commission Digital Strategy": ("https://digital-strategy.ec.europa.eu/en/rss.xml", 1),
    "Microsoft 365 Blog": ("https://www.microsoft.com/en-us/microsoft-365/blog/feed/", 1),
    "Azure AI Blog": ("https://azure.microsoft.com/en-us/blog/tag/ai/feed/", 1),
    "Power Automate Blog": ("https://www.microsoft.com/en-us/power-platform/blog/power-automate/feed/", 1),
    "Power BI Blog": ("https://powerbi.microsoft.com/en-us/blog/feed/", 1),
    "arXiv cs.AI": ("http://export.arxiv.org/rss/cs.AI", 1),
    # Tier 2 — established industry publications
    "MIT Technology Review": ("https://www.technologyreview.com/feed/", 2),
    "ZDNet AI": ("https://www.zdnet.com/topic/artificial-intelligence/rss.xml", 2),
    "TechCrunch AI": ("https://techcrunch.com/category/artificial-intelligence/feed/", 2),
    "VentureBeat AI": ("https://venturebeat.com/category/ai/feed/", 2),
    "The Verge AI": ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 2),
    "IndustryWeek": ("https://www.industryweek.com/rss", 2),
    # Tier 3 — specialist newsletters
    "The Batch": ("https://www.deeplearning.ai/the-batch/feed/", 3),
    "Import AI": ("https://importai.substack.com/feed", 3),
}

# The 6 topic toggles, each with its keyword list drawn from the company
# framework doc. These are editable — if a topic feels off, add/remove
# keywords here rather than touching the scoring logic below.
TOPIC_KEYWORDS = {
    "Data": [
        "data governance", "data quality", "data lineage", "data catalogue",
        "lakehouse", "semantic model", "knowledge graph", "rag", "vector search",
        "synthetic data", "mlops", "data products", "datalake",
    ],
    "Business Intelligence": [
        "power bi", "dashboard", "business intelligence", "self-service analytics",
        "reporting standard", "scorecard", "customer churn", "b2b analytics",
        "pricing analytics", "sales intelligence",
    ],
    "Artificial Intelligence": [
        "enterprise ai", "applied ai", "generative ai", "agentic ai", "ai agent",
        "autonomous agent", "multimodal ai", "small language model", "foundation model",
        "machine learning", "eu ai act", "responsible ai", "ai governance", "ai risk",
        "model governance", "agent governance", "ai inventory", "human oversight",
        "ai monitoring", "ai incident", "shadow ai", "dpia", "copilot studio",
        "copilot chat", "azure ai", "ai builder", "ai literacy", "ai adoption",
        "human-ai collaboration",
    ],
    "Digital Tools": [
        "microsoft 365", "power platform", "power automate", "power apps",
        "sharepoint", "intelligent automation", "document intelligence", "ocr",
        "email classification", "workflow automation", "process mining",
        "orchestration", "low-code governance", "citizen development",
    ],
    "Industry 4.0": [
        "industrial ai", "manufacturing ai", "process industry", "computer vision",
        "machine vision", "ppe detection", "visual inspection", "anomaly detection",
        "edge ai", "predictive maintenance", "remote inspection", "industrial robotics",
        "cobot", "inspection robot", "quadruped robot", "smart valve", "industrial iot",
        "digital twin", "connected worker", "smart glasses",
    ],
    "Open Innovation": [
        "industrial startup", "open innovation", "corporate innovation",
        "technology scouting", "industrial pilot", "research partnership",
        "climate risk ai",
    ],
}

# Approximate proxies for the rubric criteria that aren't a direct topic match.
INDUSTRIAL_KEYWORDS = [
    "process industr", "manufactur", "engineering", "safety", "logistics",
    "b2b", "industrial", "plant", "chemical",
]
ACTIONABILITY_KEYWORDS = [
    "case study", "deployment", "pilot", "benchmark", "lesson", "tool update",
    "release", "launch", "rollout", "implementation",
]
EUROPEAN_KEYWORDS = [
    "eu ", "european union", "european commission", "gdpr", "eu ai act",
    "brussels", "member state",
]
RISK_KEYWORDS = [
    "lawsuit", "security", "privacy", "safety", "incident", "breach",
    "vulnerability", "risk", "fine", "penalty",
]
# Named internal tools/initiatives, for the "internal bridge" criterion —
# includes both the company's approved-tools list and the platforms named
# in the framework doc.
INTERNAL_TOOLS = [
    "datalake", "power bi", "copilot", "sharepoint", "power automate",
    "power apps", "power platform", "ai builder", "azure ai", "copilot studio",
    "copilot chat", "work iq", "heygen", "google ai studio", "claude",
]

SCORE_THRESHOLD = 10          # out of 16, per the document
MAIN_FEED_TOP_N = 10
TOPIC_TOP_N = 5
NOTIFICATION_TOP_N = 5        # how many items go into the Teams/email brief
HISTORY_RETENTION_DAYS = 31
HISTORY_PATH = "docs/data/history.json"

WINDOWS = {"today": 1, "week": 7, "month": 30}

# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------

def fetch_all_items():
    items = []
    for source_name, (url, tier) in FEEDS.items():
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
            title = html.unescape(entry.get("title", "").strip())
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", entry.get("description", ""))
            summary = re.sub("<[^<]+?>", "", summary_raw).strip()
            summary = html.unescape(summary)

            published_dt = None
            if entry.get("published_parsed"):
                published_dt = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
            elif entry.get("updated_parsed"):
                published_dt = datetime.fromtimestamp(mktime(entry.updated_parsed), tz=timezone.utc)

            if not title or not link:
                continue

            items.append({
                "source": source_name,
                "source_tier": tier,
                "title": title,
                "link": link,
                "summary": summary[:280],
                "published": published_dt.isoformat() if published_dt else None,
            })
    return items


def dedupe(items):
    seen_links, seen_titles, unique = set(), set(), []
    for item in items:
        norm_title = re.sub(r"\W+", "", item["title"].lower())
        if item["link"] in seen_links or norm_title in seen_titles:
            continue
        seen_links.add(item["link"])
        seen_titles.add(norm_title)
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# HISTORY (enables today / week / month views)
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"WARNING: could not read existing history ({e}), starting fresh.")
    return []


def merge_into_history(existing_history, new_items, now):
    by_link = {h["link"]: h for h in existing_history}
    for item in new_items:
        if item["link"] not in by_link:
            by_link[item["link"]] = {**item, "first_seen": now.isoformat()}

    cutoff = now - timedelta(days=HISTORY_RETENTION_DAYS)
    pruned = []
    for h in by_link.values():
        reference_date = (
            datetime.fromisoformat(h["published"]) if h.get("published")
            else datetime.fromisoformat(h["first_seen"])
        )
        if reference_date >= cutoff:
            pruned.append(h)
    return pruned


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ---------------------------------------------------------------------------
# SCORING (approximation of the document's 8-criterion / 16-point rubric)
# ---------------------------------------------------------------------------

def _count_hits(text, words):
    return sum(1 for w in words if w in text)


def score_item_for_topic(item, topic_name, now):
    text = f"{item['title']} {item['summary']}".lower()

    theme_score = min(3, _count_hits(text, TOPIC_KEYWORDS[topic_name]))
    industrial_score = min(3, _count_hits(text, INDUSTRIAL_KEYWORDS))
    action_score = min(2, _count_hits(text, ACTIONABILITY_KEYWORDS))
    euro_score = min(2, _count_hits(text, EUROPEAN_KEYWORDS))
    tier = item.get("source_tier", 3)
    trust_score = {1: 2, 2: 1, 3: 0}.get(tier, 0)

    published = datetime.fromisoformat(item["published"]) if item.get("published") else None
    hours_ago = (now - published).total_seconds() / 3600 if published else None
    novelty_score = 1 if (hours_ago is not None and hours_ago <= 24) else 0

    risk_score = 1 if _count_hits(text, RISK_KEYWORDS) > 0 else 0
    bridge_score = min(2, _count_hits(text, INTERNAL_TOOLS))

    total = (theme_score + industrial_score + action_score + euro_score
             + trust_score + novelty_score + risk_score + bridge_score)

    breakdown = {
        "theme": theme_score, "industrial": industrial_score, "action": action_score,
        "european": euro_score, "trust": trust_score, "novelty": novelty_score,
        "risk": risk_score, "bridge": bridge_score, "tier": tier,
    }
    return total, breakdown


def is_included(total, topic_name, breakdown):
    """Mirrors the document's stated exception: below-threshold items are
    only kept if they represent a major regulatory change, or a material
    Microsoft platform change relevant to current tools."""
    if total >= SCORE_THRESHOLD:
        return True
    if breakdown["european"] >= 2:
        return True  # major regulatory signal
    if (topic_name in ("Digital Tools", "Artificial Intelligence")
            and breakdown["tier"] == 1 and breakdown["theme"] >= 2 and breakdown["bridge"] >= 1):
        return True  # material Microsoft platform change from an official source
    return False


def rank_for_topic(items, topic_name, now, top_n):
    scored = []
    for item in items:
        total, breakdown = score_item_for_topic(item, topic_name, now)
        if is_included(total, topic_name, breakdown):
            scored.append({**item, "score": total, "score_breakdown": breakdown, "topic": topic_name})
    scored.sort(key=lambda i: i["score"], reverse=True)
    return scored[:top_n]


def rank_for_main_feed(items, now, top_n):
    """Each item's best-matching topic determines its badge and score."""
    scored = []
    for item in items:
        best_total, best_breakdown, best_topic = -1, None, None
        for topic_name in TOPIC_KEYWORDS:
            total, breakdown = score_item_for_topic(item, topic_name, now)
            if total > best_total:
                best_total, best_breakdown, best_topic = total, breakdown, topic_name
        if is_included(best_total, best_topic, best_breakdown):
            scored.append({**item, "score": best_total, "score_breakdown": best_breakdown, "topic": best_topic})
    scored.sort(key=lambda i: i["score"], reverse=True)
    return scored[:top_n]


def build_all_views(history, now):
    """Returns { window: { 'Main': [...], topic_name: [...] } } for every
    window/topic combination, computed fresh from the current history."""
    views = {}
    for window_name, days in WINDOWS.items():
        cutoff = now - timedelta(days=days)
        pool = [
            h for h in history
            if h.get("published") and datetime.fromisoformat(h["published"]) >= cutoff
        ]
        window_views = {"Main": rank_for_main_feed(pool, now, MAIN_FEED_TOP_N)}
        for topic_name in TOPIC_KEYWORDS:
            window_views[topic_name] = rank_for_topic(pool, topic_name, now, TOPIC_TOP_N)
        views[window_name] = window_views
    return views


# ---------------------------------------------------------------------------
# BUILD STATIC SITE
# ---------------------------------------------------------------------------

def _item_to_json(item):
    published = datetime.fromisoformat(item["published"]) if item.get("published") else None
    return {
        "title": item["title"],
        "link": item["link"],
        "summary": item["summary"],
        "source": item["source"],
        "published_display": published.strftime("%Y-%m-%d %H:%M UTC") if published else "Date unavailable",
        "topic": item["topic"],
        "score": item["score"],
    }


def build_html(views, generated_at):
    data_for_js = {
        window_name: {topic: [_item_to_json(i) for i in items] for topic, items in topics.items()}
        for window_name, topics in views.items()
    }
    data_json = json.dumps(data_for_js)
    tab_names = ["Main"] + list(TOPIC_KEYWORDS.keys())
    tab_buttons = "".join(
        f'<button class="tab-btn{" active" if i == 0 else ""}" data-topic="{html.escape(t)}">{html.escape(t)}</button>'
        for i, t in enumerate(tab_names)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Digitalisation News Brief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 780px; margin: 40px auto; padding: 0 16px; background:#fafafa; color:#1a1a1a; }}
  h1 {{ font-size: 1.6rem; margin-bottom:4px; }}
  .generated {{ color:#666; font-size:0.85rem; margin-bottom: 20px; }}
  .controls {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom: 8px; }}
  .tab-btn, .window-btn {{ border:1px solid #ddd; background:white; border-radius:999px; padding:6px 14px; font-size:0.85rem; cursor:pointer; color:#333; }}
  .tab-btn.active, .window-btn.active {{ background:#3730a3; color:white; border-color:#3730a3; }}
  .window-row {{ margin-bottom:20px; }}
  .item {{ display:flex; gap:16px; background:white; border-radius:10px; padding:16px 20px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .rank {{ font-size:1.4rem; font-weight:700; color:#888; min-width:36px; }}
  .badge {{ display:inline-block; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.03em; background:#eef2ff; color:#3730a3; padding:2px 8px; border-radius:999px; margin-bottom:6px; }}
  h2 {{ font-size:1.05rem; margin:4px 0; }}
  h2 a {{ color:#1a1a1a; text-decoration:none; }}
  h2 a:hover {{ text-decoration:underline; }}
  p {{ font-size:0.9rem; color:#444; margin:6px 0; }}
  .meta {{ font-size:0.78rem; color:#888; }}
  .empty {{ color:#888; font-size:0.9rem; padding:20px; text-align:center; }}
</style>
</head>
<body>
  <h1>Digitalisation News Brief</h1>
  <div class="generated">Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} — approximate scoring against the company relevance rubric; see README for what this heuristic can and can't judge.</div>

  <div class="controls" id="topic-tabs">{tab_buttons}</div>
  <div class="controls window-row" id="window-tabs">
    <button class="window-btn active" data-window="today">Today</button>
    <button class="window-btn" data-window="week">This Week</button>
    <button class="window-btn" data-window="month">This Month</button>
  </div>

  <div id="news-list"></div>

<script id="news-data" type="application/json">{data_json}</script>
<script>
  const newsData = JSON.parse(document.getElementById('news-data').textContent);
  let currentTopic = 'Main';
  let currentWindow = 'today';

  function escapeHtml(s) {{
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }}

  function render() {{
    const items = (newsData[currentWindow] && newsData[currentWindow][currentTopic]) || [];
    const container = document.getElementById('news-list');
    if (items.length === 0) {{
      container.innerHTML = '<div class="empty">No qualifying stories for this topic/window yet.</div>';
      return;
    }}
    container.innerHTML = items.map((item, idx) => `
      <div class="item">
        <div class="rank">#${{idx + 1}}</div>
        <div class="content">
          <span class="badge">${{escapeHtml(item.topic)}}</span>
          <h2><a href="${{item.link}}" target="_blank" rel="noopener">${{escapeHtml(item.title)}}</a></h2>
          <p>${{escapeHtml(item.summary)}}</p>
          <div class="meta">Source: ${{escapeHtml(item.source)}} — ${{item.published_display}} — score ${{item.score}}/16</div>
        </div>
      </div>
    `).join('');
  }}

  document.getElementById('topic-tabs').addEventListener('click', (e) => {{
    if (!e.target.classList.contains('tab-btn')) return;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    e.target.classList.add('active');
    currentTopic = e.target.dataset.topic;
    render();
  }});

  document.getElementById('window-tabs').addEventListener('click', (e) => {{
    if (!e.target.classList.contains('window-btn')) return;
    document.querySelectorAll('.window-btn').forEach(b => b.classList.remove('active'));
    e.target.classList.add('active');
    currentWindow = e.target.dataset.window;
    render();
  }});

  render();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# BUILD RSS FEED (Main Feed's "today" view — this is what Power Automate watches)
# ---------------------------------------------------------------------------

def build_rss(main_today_items, generated_at, pages_url):
    date_str = generated_at.strftime("%Y-%m-%d")
    guid = f"digitalisation-brief-{generated_at.strftime('%Y%m%dT%H%M%S')}"
    pub_date_rfc822 = generated_at.strftime("%a, %d %b %Y %H:%M:%S +0000")
    link = pages_url or ""

    top_for_notification = main_today_items[:NOTIFICATION_TOP_N]

    if top_for_notification:
        parts = []
        for idx, item in enumerate(top_for_notification, start=1):
            safe_title = html.escape(item["title"])
            safe_link = html.escape(item["link"], quote=True)
            safe_topic = html.escape(item["topic"])
            safe_summary = html.escape(item["summary"])
            parts.append(
                f"{idx}. [{safe_topic}] "
                f'<a href="{safe_link}"><b>{safe_title}</b></a><br/>'
                f"{safe_summary}"
            )
        description_html = "<br/><br/>".join(parts)
        if link:
            description_html += f'<br/><br/><a href="{html.escape(link, quote=True)}">View full site</a>'
    else:
        description_html = "No qualifying news found today."

    title = f"Digitalisation News Brief — {date_str}"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Digitalisation News Brief</title>
  <link>{html.escape(link)}</link>
  <description>Top digitalisation stories, refreshed daily</description>
  <item>
    <title><![CDATA[{title}]]></title>
    <link>{html.escape(link)}</link>
    <guid isPermaLink="false">{guid}</guid>
    <pubDate>{pub_date_rfc822}</pubDate>
    <description><![CDATA[{description_html}]]></description>
  </item>
</channel>
</rss>
"""


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)

    raw_items = dedupe(fetch_all_items())
    print(f"Fetched {len(raw_items)} unique items across {len(FEEDS)} feeds.")

    history = load_history()
    history = merge_into_history(history, raw_items, now)
    save_history(history)
    print(f"History now holds {len(history)} items (after 31-day pruning).")

    views = build_all_views(history, now)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pages_url = ""
    if "/" in repo:
        owner, repo_name = repo.split("/", 1)
        pages_url = f"https://{owner}.github.io/{repo_name}/"

    os.makedirs("docs", exist_ok=True)
    site_html = build_html(views, now)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(site_html)

    feed_xml = build_rss(views["today"]["Main"], now, pages_url)
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(feed_xml)

    print("Wrote docs/index.html, docs/feed.xml, docs/data/history.json.")
    print("\nToday's Main Feed:")
    for item in views["today"]["Main"]:
        print(f" - [{item['topic']}] {item['title']} (score {item['score']}/16, {item['source']})")


if __name__ == "__main__":
    main()
