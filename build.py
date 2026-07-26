#!/usr/bin/env python3
"""New PAC City — static site builder.

Fetches the RSS feeds listed in feeds.json and writes site/index.html.
Standard library only, so any scheduler anywhere can run it:

    python3 build.py

Constraints implemented here are contractual (spec.md): headlines and
short snippets only, every item links out, every item names its source,
the "last updated" stamp is honest.
"""

import email.utils
import gzip
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
URL_DATE = re.compile(r"/(20\d\d)/([a-z]{3}|\d{1,2})/(\d{1,2})/", re.I)

HERE = Path(__file__).parent
USER_AGENT = "NewPACCityBot/0.1 (news aggregator; links and attribution only)"
FETCH_TIMEOUT_S = 20
SPORT_LABELS = {"football": "Football", "mbb": "Men's Basketball", "wbb": "Women's Basketball"}

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # some feed servers gzip regardless of Accept-Encoding
        raw = gzip.decompress(raw)
    return raw


def text_of(el):
    return (el.text or "").strip() if el is not None else ""


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def snippet_of(s, limit):
    s = strip_html(s)
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:—- ") + "…"


def date_from_url(link):
    """Some feeds omit dates; many news URLs carry /2026/jul/22/ or /2026/07/22/."""
    m = URL_DATE.search(link or "")
    if not m:
        return None
    year, month, day = m.groups()
    month = MONTHS.get(month.lower()) if month.isalpha() else int(month)
    try:
        return datetime(int(year), month, int(day), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_date(s):
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(raw):
    """Return a list of {title, link, date, summary} from RSS 2.0 or Atom bytes."""
    root = ET.fromstring(raw)
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = ""
            for l in e.findall(f"{ATOM}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            items.append({
                "title": strip_html(text_of(e.find(f"{ATOM}title"))),
                "link": link,
                "date": parse_date(text_of(e.find(f"{ATOM}published")) or text_of(e.find(f"{ATOM}updated"))),
                "summary": text_of(e.find(f"{ATOM}summary")) or text_of(e.find(f"{ATOM}content")),
            })
    else:  # RSS 2.0
        for e in root.iter("item"):
            items.append({
                "title": strip_html(text_of(e.find("title"))),
                "link": text_of(e.find("link")),
                "date": parse_date(text_of(e.find("pubDate"))),
                "summary": text_of(e.find("description")),
            })
    return [i for i in items if i["title"] and i["link"]]


def rel_time(dt, now):
    if dt is None:
        return "recently"
    delta = now - dt
    if delta < timedelta(hours=1):
        return "just now"
    if delta < timedelta(hours=24):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    if delta < timedelta(days=14):
        return f"{delta.days}d ago"
    return dt.strftime("%b %-d")


def collect(cfg):
    """Fetch all feeds; return (items_by_team, fetch_report)."""
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=cfg["max_item_age_days"])
    by_team, report = {}, []

    def job(feed):
        return feed, parse_feed(fetch(feed["url"]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = []
        for feed, fut in [(f, pool.submit(job, f)) for f in cfg["feeds"]]:
            try:
                results.append(fut.result())
            except Exception as exc:  # a dead source must never kill the page
                report.append((feed["team"], feed["url"], "FAILED", str(exc)[:120]))
    for feed, parsed in results:
        fresh = 0
        for it in parsed:
            if it["date"] is None:
                it["date"] = date_from_url(it["link"])
            if it["date"] is not None and it["date"] < oldest:
                continue
            it["source"] = feed["source"]
            it["sport"] = feed["sport"]
            by_team.setdefault(feed["team"], []).append(it)
            fresh += 1
        report.append((feed["team"], feed["url"], "ok", f"{fresh} recent items"))
    for team, items in by_team.items():
        seen, unique = {}, []
        for it in sorted(items, key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            key = it["link"] or it["title"]
            if key not in seen:
                seen[key] = it
                unique.append(it)
            elif seen[key]["sport"] != it["sport"]:
                # same story in multiple sport feeds = department-wide news;
                # a specific sport label would be wrong, so drop it
                seen[key]["sport"] = "all"
        cap = cfg["max_items_conference"] if team == "conference" else cfg["max_items_per_team"]
        by_team[team] = unique[:cap]
    return by_team, report


def render_item(it, cfg, now):
    sport = SPORT_LABELS.get(it["sport"])
    sport_tag = f'<span class="tag">{html.escape(sport)}</span>' if sport else ""
    snip = snippet_of(it["summary"], cfg["snippet_max_chars"])
    snip_html = f'<p class="snip">{html.escape(snip)}</p>' if snip else ""
    return (
        f'<article class="item">'
        f'<a class="headline" href="{html.escape(it["link"], quote=True)}" rel="noopener">{html.escape(it["title"])}</a>'
        f'{snip_html}'
        f'<p class="attrib">{sport_tag}{html.escape(it["source"])} · {rel_time(it["date"], now)}</p>'
        f"</article>"
    )


def render_section(title, color, items, cfg, now, anchor):
    dot = f'<span class="dot" style="background:{html.escape(color, quote=True)}"></span>' if color else ""
    if items:
        body = "\n".join(render_item(it, cfg, now) for it in items)
    else:
        body = '<p class="empty">No recent news — check back soon.</p>'
    return (
        f'<section id="{anchor}">'
        f"<h2>{dot}{html.escape(title)}</h2>\n{body}\n</section>"
    )


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render_page(cfg, by_team, now):
    teams = sorted(cfg["teams"], key=lambda t: t["name"])
    nav = "".join(
        f'<a href="#{slugify(t["name"])}">{html.escape(t["name"])}</a>' for t in teams
    )
    sections = [render_section("Around the Conference", None, by_team.get("conference", []), cfg, now, "conference")]
    sections += [
        render_section(t["name"], t["color"], by_team.get(t["name"], []), cfg, now, slugify(t["name"]))
        for t in teams
    ]
    stamp = now.strftime("%b %d, %Y · %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(cfg["site_name"])} — {html.escape(cfg["tagline"])}</title>
<meta name="description" content="Latest {html.escape(cfg['site_name'])} headlines: news for every new PAC-12 team, updated automatically, always linking to the original source.">
<style>
:root {{
  --bg: #f5f4f0; --card: #ffffff; --ink: #1a1a1a; --muted: #6b6b6b;
  --accent: #0a4d68; --line: #e3e1da; --tag-bg: #eef2f5;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #14161a; --card: #1e2126; --ink: #eceae6; --muted: #9a9a94;
          --accent: #7fc3dd; --line: #2c3038; --tag-bg: #262b33; }}
}}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: var(--bg); color: var(--ink);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
header {{ padding: 28px 16px 12px; max-width: 720px; margin: 0 auto; }}
h1 {{ font-size: 30px; letter-spacing: -0.5px; }}
h1 .pac {{ color: var(--accent); }}
.tagline {{ color: var(--muted); margin-top: 2px; }}
.updated {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
nav {{ max-width: 720px; margin: 8px auto 0; padding: 0 16px 4px; display: flex;
  gap: 8px; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
nav a {{ flex: 0 0 auto; font-size: 13px; color: var(--ink); text-decoration: none;
  background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px; }}
main {{ max-width: 720px; margin: 0 auto; padding: 8px 16px 40px; }}
section {{ margin-top: 26px; scroll-margin-top: 12px; }}
h2 {{ font-size: 20px; display: flex; align-items: center; gap: 8px;
  border-bottom: 1px solid var(--line); padding-bottom: 6px; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto; }}
.item {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; margin-top: 10px; }}
.headline {{ color: var(--ink); font-weight: 600; text-decoration: none; display: block; }}
.headline:hover {{ color: var(--accent); }}
.snip {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
.attrib {{ color: var(--muted); font-size: 12.5px; margin-top: 8px; }}
.tag {{ background: var(--tag-bg); color: var(--accent); border-radius: 4px;
  padding: 1px 6px; margin-right: 8px; font-size: 11.5px; font-weight: 600; }}
.empty {{ color: var(--muted); font-style: italic; padding: 10px 2px; }}
footer {{ max-width: 720px; margin: 0 auto; padding: 0 16px 48px; color: var(--muted); font-size: 12.5px; }}
footer p {{ margin-top: 6px; }}
a:focus-visible, .headline:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
</style>
</head>
<body>
<header>
  <h1>New <span class="pac">PAC</span> City</h1>
  <p class="tagline">{html.escape(cfg["tagline"])}</p>
  <p class="updated">Updated {stamp} · refreshes about every {cfg["refresh_hours"]} hours</p>
</header>
<nav aria-label="Jump to team">{nav}</nav>
<main>
{chr(10).join(sections)}
</main>
<footer>
  <p>Every headline links to its original publisher; snippets are brief excerpts shown with attribution. Full stories belong to their sources.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12 Conference or any university.</p>
</footer>
</body>
</html>
"""


def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "feeds.json"
    cfg = json.loads(cfg_path.read_text())
    now = datetime.now(timezone.utc)
    by_team, report = collect(cfg)
    ok = [r for r in report if r[2] == "ok"]
    for team, url, status, detail in report:
        print(f"[{status}] {team}: {url} — {detail}")
    total_items = sum(len(v) for v in by_team.values())
    if not ok or total_items == 0:
        print("ERROR: no feed produced any items; keeping the previous page.", file=sys.stderr)
        sys.exit(1)
    out = HERE / cfg.get("output_dir", "site")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(render_page(cfg, by_team, now))
    print(f"Wrote {out.name}/index.html — {total_items} items from {len(ok)}/{len(report)} feeds.")


if __name__ == "__main__":
    main()
