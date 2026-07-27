#!/usr/bin/env python3
"""New PAC City — static site builder.

Fetches the RSS feeds listed in feeds.json and writes a homepage plus one
page per team under docs/. Standard library only, so any scheduler
anywhere can run it:

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
SPORT_EMOJI = {"football": "\U0001F3C8", "mbb": "\U0001F3C0", "wbb": "\U0001F3C0", "all": "\U0001F4E3"}

STOPWORDS = frozenset(
    "the a an and or of for to in on at with from as is are was were be been its it's this that "
    "his her their new says said after before over under vs against".split())

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
           "%3Crect width='64' height='64' rx='12' fill='%230a4d68'/%3E"
           "%3Ctext x='32' y='45' font-family='Arial' font-size='28' font-weight='bold'"
           " fill='white' text-anchor='middle'%3ENP%3C/text%3E%3C/svg%3E")

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"

# Shared CSS: color tokens + the element styles used on every page (homepage
# and team pages alike). Page-specific rules (lead card, tiles, masthead,
# team color vars) are appended by each page's own render function.
BASE_STYLE = """
:root {
  --bg: #f5f4f0; --card: #ffffff; --ink: #1a1a1a; --muted: #6b6b6b;
  --accent: #0a4d68; --line: #e3e1da; --tag-bg: #eef2f5;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14161a; --card: #1e2126; --ink: #eceae6; --muted: #9a9a94;
          --accent: #7fc3dd; --line: #2c3038; --tag-bg: #262b33; }
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--bg); color: var(--ink);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 720px; margin: 0 auto; padding: 8px 16px 40px; }
h2 { font-size: 20px; border-bottom: 1px solid var(--line); padding-bottom: 6px; margin-top: 26px; }
.item { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; margin-top: 10px; }
.headline { color: var(--ink); font-weight: 600; text-decoration: none; display: block; }
.headline:hover { color: var(--accent); }
.snip { color: var(--muted); font-size: 14px; margin-top: 4px; }
.attrib { color: var(--muted); font-size: 12.5px; margin-top: 8px; }
.tag { background: var(--tag-bg); color: var(--accent); border-radius: 4px;
  padding: 1px 6px; margin-right: 8px; font-size: 11.5px; font-weight: 600; }
.tag-big { background: #f4e8c8; color: #7a5c00; }
@media (prefers-color-scheme: dark) { .tag-big { background: #3d3420; color: #e8c96a; } }
.follow { font-size: 13px; color: var(--muted); margin: 8px 2px 0; }
.empty { color: var(--muted); font-style: italic; padding: 10px 2px; }
details.more { margin-top: 10px; }
details.more summary { cursor: pointer; color: var(--muted); font-size: 13.5px; padding: 4px 2px; }
details.more summary:hover { color: var(--accent); }
footer { max-width: 720px; margin: 0 auto; padding: 0 16px 48px; color: var(--muted); font-size: 12.5px; }
footer p { margin-top: 6px; }
footer a { color: var(--accent); }
a:focus-visible, .headline:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""


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
            it["weight"] = feed.get("weight", 1.0)
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
        merged = merge_cross_source(unique)
        if team == "conference":
            by_team[team] = merged[:cfg["max_items_conference"]]
        else:
            by_team[team] = select_for_page(
                merged, cfg["max_items_per_team"], cfg["top_stories_per_team"], now)
    return by_team, report


def title_tokens(title):
    return {w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) > 2 and w not in STOPWORDS}


def merge_cross_source(items):
    """Same story from two different sources = a big story: keep the newest,
    credit the other source on it, badge it."""
    kept = []
    for it in items:  # items arrive newest-first
        toks = title_tokens(it["title"])
        merged = False
        for k in kept:
            if k["source"] == it["source"] or not toks:
                continue
            ktoks = title_tokens(k["title"])
            shared = toks & ktoks
            if len(shared) >= 4 and len(shared) / len(toks | ktoks) >= 0.5:
                if it["source"] not in k.get("also", []):
                    k.setdefault("also", []).append(it["source"])
                merged = True
                break
        if not merged:
            kept.append(it)
    return kept


def story_score(it, now):
    """Fan-importance heuristic (roadmap: recency + source weight + coverage).
    Coverage decays on a slower clock than recency so a story several outlets
    covered stays on top for a day or two, not a week."""
    if it["date"] is not None:
        age_h = (now - it["date"]).total_seconds() / 3600
    else:
        age_h = 1080  # undated: rank as the oldest the 45-day window allows
    extra_sources = min(len(it.get("also", [])), 2)
    return (it.get("weight", 1.0) * 2 ** (-age_h / 36)
            + 0.6 * extra_sources * 2 ** (-age_h / 72))


def split_top(items, now, top_n):
    """Return (top stories by score, the rest newest-first).
    The leading top_n are always score-ordered — even when nothing folds —
    so the visible order doesn't reshuffle the day an extra item arrives.
    A one-item tail stays inline rather than folding into a stub."""
    if top_n <= 0 or len(items) <= 1:
        return items, []
    ranked = sorted(items, key=lambda i: story_score(i, now), reverse=True)
    top_ids = {id(i) for i in ranked[:top_n]}
    rest = [i for i in items if id(i) not in top_ids]
    if len(rest) < 2:
        return ranked[:top_n] + rest, []
    return ranked[:top_n], rest


def select_for_page(items, cap, top_n, now):
    """Cap a team's page items without letting the cap drop a top-ranked
    story: the pinned top_n survive, the rest of the budget goes to the
    newest. Input and output are newest-first."""
    if len(items) <= cap:
        return items
    if top_n <= 0:
        return items[:cap]
    ranked = sorted(items, key=lambda i: story_score(i, now), reverse=True)
    pinned = {id(i) for i in ranked[:top_n]}
    rest = [i for i in items if id(i) not in pinned][:max(cap - len(pinned), 0)]
    keep = pinned | {id(i) for i in rest}
    return [i for i in items if id(i) in keep]


def pick_lead(by_team, now):
    """Choose the homepage lead story. Prefer a big (cross-source-merged)
    story out of the conference feed; otherwise the top-scored/newest
    conference item; if the conference feed produced nothing, fall back to
    the highest-scored story anywhere. Returns (item, source) where source
    is "conference" or a team name, or (None, None) if there's nothing."""
    conf = by_team.get("conference", [])
    if conf:
        big = [it for it in conf if it.get("also")]
        pool = big if big else conf
        return max(pool, key=lambda it: story_score(it, now)), "conference"
    candidates = [(team, it) for team, items in by_team.items() for it in items]
    if not candidates:
        return None, None
    team, it = max(candidates, key=lambda pair: story_score(pair[1], now))
    return it, team


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render_item(it, cfg, now, in_team_section):
    sport = it["sport"]
    if sport in SPORT_LABELS:
        label = f"{SPORT_EMOJI[sport]} {SPORT_LABELS[sport]}"
        sport_tag = f'<span class="tag tag-{sport}">{html.escape(label)}</span>'
    elif in_team_section:
        sport_tag = f'<span class="tag">{SPORT_EMOJI["all"]} All sports</span>'
    else:
        sport_tag = ""
    big_tag, source = "", html.escape(it["source"])
    if it.get("also"):
        big_tag = '<span class="tag tag-big">⭐ Big story</span>'
        source += " · also " + html.escape(", ".join(it["also"]))
    snip = snippet_of(it["summary"], cfg["snippet_max_chars"])
    snip_html = f'<p class="snip">{html.escape(snip)}</p>' if snip else ""
    return (
        f'<article class="item">'
        f'<a class="headline" href="{html.escape(it["link"], quote=True)}" rel="noopener">{html.escape(it["title"])}</a>'
        f'{snip_html}'
        f'<p class="attrib">{big_tag}{sport_tag}{source} · {rel_time(it["date"], now)}</p>'
        f"</article>"
    )


def render_team_sport_item(it, cfg, now, prefix=None):
    """An item inside a team page's per-sport section: no repeated sport tag
    (the section heading already says it), but a Men's/Women's prefix for
    the combined basketball section, and the big-story badge preserved."""
    big_tag, source = "", html.escape(it["source"])
    if it.get("also"):
        big_tag = '<span class="tag tag-big">⭐ Big story</span> '
        source += " · also " + html.escape(", ".join(it["also"]))
    prefix_html = f"{html.escape(prefix)} · " if prefix else ""
    snip = snippet_of(it["summary"], cfg["snippet_max_chars"])
    snip_html = f'<p class="snip">{html.escape(snip)}</p>' if snip else ""
    return (
        f'<article class="item">'
        f'<a class="headline" href="{html.escape(it["link"], quote=True)}" rel="noopener">{html.escape(it["title"])}</a>'
        f'{snip_html}'
        f'<p class="attrib">{big_tag}{prefix_html}{source} · {rel_time(it["date"], now)}</p>'
        f"</article>"
    )


def render_lead_card(item, source, teams_by_name, cfg, now):
    """The homepage's single lead-story object (mockup A's `.item.lead`)."""
    big_tag = '<span class="tag tag-big">⭐ Big story</span> ' if item.get("also") else ""
    if source == "conference":
        teamline = f"{big_tag}Around the Conference"
    else:
        team = teams_by_name[source]
        sport = item["sport"]
        if sport in SPORT_LABELS:
            sport_txt = f"{SPORT_EMOJI[sport]} {SPORT_LABELS[sport]}"
        else:
            sport_txt = f'{SPORT_EMOJI["all"]} All sports'
        dot = f'<span class="dot" style="background:{html.escape(team["primary"], quote=True)}"></span>'
        teamline = (f'{big_tag}{dot}<a href="{html.escape(team["slug"], quote=True)}/">'
                    f'{html.escape(team["name"])}</a> · {sport_txt}')
    snip = snippet_of(item["summary"], cfg["snippet_max_chars"])
    snip_html = f'<p class="snip">{html.escape(snip)}</p>' if snip else ""
    also = f' · also covered by {html.escape(", ".join(item["also"]))}' if item.get("also") else ""
    return (
        '<article class="item lead">'
        f'<p class="teamline">{teamline}</p>'
        f'<a class="headline" href="{html.escape(item["link"], quote=True)}" rel="noopener">{html.escape(item["title"])}</a>'
        f"{snip_html}"
        f'<p class="attrib">{html.escape(item["source"])} · {rel_time(item["date"], now)}{also}</p>'
        "</article>"
    )


def render_tile(team, items, now):
    """One of the nine homepage team tiles — color-wash treatment (Idea 2)."""
    slug = html.escape(team["slug"], quote=True)
    legacy_anchor = slugify(team["name"])
    if not items:
        body = ('<p class="empty-tile">No recent news — check back soon.</p>'
                f'<a class="more" href="{slug}/">Team page →</a>')
    else:
        top = max(items, key=lambda it: story_score(it, now))
        remaining = len(items) - 1
        if remaining > 0:
            more_text = f"{remaining} more {html.escape(team['nickname'])} stories →"
        else:
            more_text = "Team page →"
        body = (
            f'<p class="hl"><a href="{html.escape(top["link"], quote=True)}" rel="noopener">{html.escape(top["title"])}</a></p>'
            f'<p class="src">{html.escape(top["source"])} · {rel_time(top["date"], now)}</p>'
            f'<a class="more" href="{slug}/">{more_text}</a>'
        )
    return (
        f'<div class="tile wash team-{slug}" id="{legacy_anchor}">'
        '<div class="bar"><b></b><i></i></div>'
        '<div class="tbody">'
        f'<div class="tname"><a href="{slug}/">{html.escape(team["name"])}</a>'
        f'<span class="slug">/{slug}</span></div>'
        f"{body}"
        "</div></div>"
    )


def team_css_vars(teams):
    """Per-team CSS custom properties, scoped by .team-<slug>, with a
    dark-mode override — the homepage's one source of truth for team colors
    (used to paint all nine tiles). --p/--s are the official colors, used
    only for decoration (stripe, wash tint); --pt is the text-safe variant
    for anything rendering primary as foreground text — WCAG AA 4.5:1.
    In dark mode --pt collapses onto --p since primary_dark was chosen to
    pass contrast on the wash background."""
    light = "\n".join(
        f'.team-{t["slug"]} {{ --p: {t["primary"]}; --s: {t["secondary"]}; '
        f'--pt: {t.get("primary_text", t["primary"])}; }}' for t in teams)
    dark = "\n  ".join(
        f'.team-{t["slug"]} {{ --p: {t["primary_dark"]}; --s: {t["secondary_dark"]}; '
        f'--pt: {t["primary_dark"]}; }}' for t in teams)
    return f"{light}\n@media (prefers-color-scheme: dark) {{\n  {dark}\n}}"


def team_root_vars(team):
    """Single-team :root color vars for a team page's cap bar / links / nickname.
    See team_css_vars for the --p vs --pt (decoration vs text) split."""
    pt = team.get("primary_text", team["primary"])
    return (
        f':root {{ --p: {team["primary"]}; --s: {team["secondary"]}; --pt: {pt}; }}\n'
        "@media (prefers-color-scheme: dark) {\n"
        f'  :root {{ --p: {team["primary_dark"]}; --s: {team["secondary_dark"]}; '
        f'--pt: {team["primary_dark"]}; }}\n'
        "}"
    )


def follow_links(team):
    """Short-label follow links for a team page's masthead."""
    links = []
    if team.get("x"):
        links.append(f'<a href="https://x.com/{html.escape(team["x"], quote=True)}" rel="noopener">X</a>')
    if team.get("ig"):
        links.append(
            f'<a href="https://www.instagram.com/{html.escape(team["ig"], quote=True)}/" rel="noopener">Instagram</a>')
    if team.get("site"):
        links.append(f'<a href="{html.escape(team["site"], quote=True)}" rel="noopener">Official site</a>')
    return " · ".join(links)


def render_homepage(cfg, by_team, now):
    teams = sorted(cfg["teams"], key=lambda t: t["name"])
    teams_by_name = {t["name"]: t for t in cfg["teams"]}
    lead, source = pick_lead(by_team, now)

    conf_rest = by_team.get("conference", [])
    if source == "conference" and lead is not None:
        conf_rest = [it for it in conf_rest if it is not lead]

    lead_html = render_lead_card(lead, source, teams_by_name, cfg, now) if lead else ""
    tiles_html = "\n".join(render_tile(t, by_team.get(t["name"], []), now) for t in teams)

    conf_section = ""
    if conf_rest:
        items_html = "\n".join(render_item(it, cfg, now, False) for it in conf_rest)
        conf_section = f'<section id="conference"><h2>Around the Conference</h2>\n{items_html}\n</section>'

    stamp = now.strftime("%b %d, %Y · %H:%M UTC")
    style = BASE_STYLE + f"""
header {{ padding: 28px 16px 12px; max-width: 720px; margin: 0 auto; }}
h1 {{ font-size: 30px; letter-spacing: -0.5px; }}
h1 .pac {{ color: var(--accent); }}
.tagline {{ color: var(--muted); margin-top: 2px; }}
.updated {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
.lead {{ border-width: 2px; padding: 18px 18px; margin-top: 18px; }}
.lead .headline {{ font-size: 22px; line-height: 1.3; }}
.teamline {{ font-size: 12.5px; color: var(--muted); margin-bottom: 4px; }}
.teamline a {{ color: var(--ink); font-weight: 600; text-decoration: none; }}
.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 26px; }}
@media (max-width: 540px) {{ .grid {{ grid-template-columns: 1fr; }} }}
.tile {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
.bar {{ display: flex; height: 5px; }}
.bar b {{ flex: 7; background: var(--p); }}
.bar i {{ flex: 3; background: var(--s); }}
.tbody {{ padding: 11px 13px; }}
.tname {{ display: flex; justify-content: space-between; align-items: baseline; }}
.tname a {{ font-size: 15.5px; font-weight: 700; text-decoration: none; }}
.tile .slug {{ color: var(--accent); font-size: 12px; font-weight: 600; }}
.hl {{ margin-top: 7px; font-size: 13.5px; font-weight: 600; line-height: 1.35; }}
.hl a {{ color: var(--ink); text-decoration: none; }}
.hl a:hover {{ text-decoration: underline; }}
.src {{ color: var(--muted); font-size: 11.5px; margin-top: 3px; }}
.tile .more {{ display: inline-block; font-size: 12px; margin-top: 7px; text-decoration: none; }}
.wash .tbody {{ background: color-mix(in srgb, var(--p) 7%, var(--card)); }}
.wash .tname a {{ color: var(--pt); }}
.wash .more {{ color: var(--pt); }}
.empty-tile {{ color: var(--muted); font-style: italic; font-size: 13px; margin-top: 7px; }}
{team_css_vars(teams)}
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(cfg["site_name"])} — {html.escape(cfg["tagline"])}</title>
<meta name="description" content="Latest {html.escape(cfg['site_name'])} headlines: news for every new PAC-12 team, updated automatically, always linking to the original source.">
<link rel="icon" href="{FAVICON}">
<meta property="og:title" content="{html.escape(cfg["site_name"])} — {html.escape(cfg["tagline"])}">
<meta property="og:description" content="The latest football, men's basketball, and women's basketball news for all nine new Pac-12 schools — refreshed automatically, always linking to the original source.">
<meta property="og:type" content="website">
<meta property="og:url" content="{html.escape(cfg.get("site_url", ""), quote=True)}">
<meta name="twitter:card" content="summary">
<style>{style}</style>
</head>
<body>
<header>
  <h1>New <span class="pac">PAC</span> City</h1>
  <p class="tagline">{html.escape(cfg["tagline"])}</p>
  <p class="updated">Updated {stamp} · refreshes about every {cfg["refresh_hours"]} hours</p>
</header>
<main>
{lead_html}
<div class="grid">
{tiles_html}
</div>
{conf_section}
</main>
<footer>
  <p>Every headline links to its original publisher; snippets are brief excerpts shown with attribution. Full stories belong to their sources.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12 Conference or any university.</p>
</footer>
</body>
</html>
"""


def render_team_page(team, items, cfg, now):
    top, rest = split_top(items, now, cfg["top_stories_per_team"])
    football = [it for it in rest if it["sport"] == "football"]
    hoops = [it for it in rest if it["sport"] in ("mbb", "wbb")]
    other = [it for it in rest if it["sport"] not in ("football", "mbb", "wbb")]

    sections = []
    if top:
        top_html = "\n".join(render_item(it, cfg, now, True) for it in top)
        sections.append(f'<h2>Top {html.escape(team["nickname"])} stories</h2>\n{top_html}')
    if football:
        football_html = "\n".join(render_team_sport_item(it, cfg, now) for it in football)
        sections.append(f"<h2>\U0001F3C8 Football</h2>\n{football_html}")
    if hoops:
        hoops_html = "\n".join(
            render_team_sport_item(it, cfg, now, prefix=("Men's" if it["sport"] == "mbb" else "Women's"))
            for it in hoops)
        sections.append(f"<h2>\U0001F3C0 Basketball</h2>\n{hoops_html}")
    if other:
        other_html = "\n".join(render_team_sport_item(it, cfg, now) for it in other)
        sections.append(f'<h2>\U0001F4E3 More {html.escape(team["name"])} news</h2>\n{other_html}')
    if not items:
        sections.append('<p class="empty">No recent news — check back soon.</p>')

    body = "\n".join(sections)
    reserved = ('<article class="item future">Reserved for later slices: schedule &amp; next game · '
                "scores · social feed</article>")
    follow = follow_links(team)
    follow_html = f'<p class="follow">Follow the team: {follow}</p>' if follow else ""
    stamp = now.strftime("%b %d, %Y · %H:%M UTC")

    nickname = html.escape(team["nickname"])
    school = html.escape(team["name"])
    slug = team["slug"]
    title = f"{school} {nickname} — {html.escape(cfg['site_name'])}"
    description = f"Latest {school} {nickname} news — football and basketball headlines, updated automatically."
    og_url = html.escape(cfg.get("site_url", "") + slug + "/", quote=True)

    style = BASE_STYLE + f"""
{team_root_vars(team)}
.teambar {{ height: 6px; background: var(--p); }}
.crumb {{ max-width: 720px; margin: 0 auto; padding: 14px 16px 0; font-size: 13px; color: var(--muted); }}
.crumb a {{ color: var(--accent); text-decoration: none; }}
header {{ padding: 8px 16px 12px; max-width: 720px; margin: 0 auto; }}
h1 {{ font-size: 32px; letter-spacing: -0.5px; margin-top: 6px; }}
h1 .nick {{ color: var(--s); }}
.slugpill {{ display: inline-block; margin-top: 8px; font-size: 13px; font-weight: 600;
  color: var(--pt); background: var(--card); border: 1px solid var(--line);
  border-radius: 999px; padding: 3px 12px; }}
.updated {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
.follow a {{ color: var(--pt); text-decoration: none; font-weight: 600; }}
.future {{ border-style: dashed; color: var(--muted); font-size: 14px; text-align: center; padding: 16px 14px; }}
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{FAVICON}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<meta property="og:url" content="{og_url}">
<meta name="twitter:card" content="summary">
<style>{style}</style>
</head>
<body>
<div class="teambar"></div>
<p class="crumb"><a href="../">← New PAC City</a> · all nine teams</p>
<header>
  <h1>{school} <span class="nick">{nickname}</span></h1>
  <span class="slugpill">/{slug}</span>
  <p class="updated">Updated {stamp} · refreshes about every {cfg["refresh_hours"]} hours</p>
  {follow_html}
</header>
<main>
{body}
{reserved}
</main>
<footer>
  <p>Every headline links to its original publisher; snippets are brief excerpts shown with attribution. Full stories belong to their sources.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12 Conference or any university.</p>
  <p><a href="../">← Back to the New PAC City homepage</a></p>
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
    (out / "index.html").write_text(render_homepage(cfg, by_team, now))
    for team in cfg["teams"]:
        team_dir = out / team["slug"]
        team_dir.mkdir(exist_ok=True)
        (team_dir / "index.html").write_text(render_team_page(team, by_team.get(team["name"], []), cfg, now))
    print(f"Wrote {out.name}/index.html + {len(cfg['teams'])} team pages — "
          f"{total_items} items from {len(ok)}/{len(report)} feeds.")


if __name__ == "__main__":
    main()
