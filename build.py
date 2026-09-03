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
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

import watch_data
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
# Only non-text media get a label; tagging every article "text" would be noise.
MEDIUM_LABELS = {"audio": "\U0001F3A7 Podcast", "video": "\U0001F4FA Video"}

STOPWORDS = frozenset(
    "the a an and or of for to in on at with from as is are was were be been its it's this that "
    "his her their new says said after before over under vs against".split())

# Keyword topic filter (single recency stream job, 2026-08-06): admission gate
# for feeds whose `topic_scope` is "athletics-wide" (a general campus-sports
# section, not a single-team athletics-department feed) — everything else is
# admitted unfiltered, per H-A1/H-A2 in classification.md. Deliberately
# name/alias matching only, no model in the loop (D-040).
SPORT_KEYWORDS = {
    "football": ["football"],
    "wbb": ["women's basketball", "womens basketball"],
    "mbb": ["men's basketball", "mens basketball"],
}

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
           "%3Crect width='64' height='64' rx='12' fill='%230a4d68'/%3E"
           "%3Ctext x='32' y='45' font-family='Arial' font-size='28' font-weight='bold'"
           " fill='white' text-anchor='middle'%3ENP%3C/text%3E%3C/svg%3E")

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
DC = "{http://purl.org/dc/elements/1.1/}"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Three values, and an unknown renders NOTHING — never "free", never
# "subscribers" (CEO, 2026-09-02; spec 4.3). Both defaults are failures.
LOCK_LABELS = {"free": "Free", "metered": "Metered", "paywall": "\U0001F512 Subscribers"}

# Filled by main() from the config: the school name a row prints -> its route.
TEAM_SLUGS = {}

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
.tag-medium { background: #e8e3f2; color: #4b3f6b; }
@media (prefers-color-scheme: dark) { .tag-big { background: #3d3420; color: #e8c96a; }
                                      .tag-medium { background: #2f2a3d; color: #bfb0e0; } }
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


def duration_seconds(raw):
    """<itunes:duration> as whole seconds. Accepts 'SS', 'MM:SS' and 'HH:MM:SS'."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs or None


def format_duration(seconds):
    """Whole seconds as a compact label — '37 min', '1h 12m' — for the `extent`
    field (nature_of()). Free on every item that carries <itunes:duration>."""
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def parse_feed(raw):
    """Return a list of {title, link, link_kind, date, summary, author, duration, guid}.

    `link` is the item's own web page where the feed gives one, and otherwise the
    audio enclosure. Podcast hosting platforms with no public front end (Megaphone,
    Amperwave, art19) routinely omit <link> entirely because there is no per-episode
    page to point at, and requiring it discarded ~990 items across feeds we are
    permitted to fetch — including every independent voice covering Oregon State.
    The enclosure is unique per episode, so de-duplication (which keys on the link)
    stays correct. Falling back further, to a shared channel URL, is NOT done here:
    collect() does that, from the feed's own `channel_url`, since it would give many
    items one href and needs the stable id scheme (keyed on guid) to stay safe.

    `guid` is the feed's own per-item identifier (Atom's mandatory <id>, RSS's
    optional <guid>) — kept separate from `link` because it is what the stable
    story id gets built from (collect()), never the link itself.
    """
    root = ET.fromstring(raw)
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link, enclosure = "", ""
            for l in e.findall(f"{ATOM}link"):
                rel = l.get("rel")
                if rel in (None, "alternate") and not link:
                    link = l.get("href", "")
                elif rel == "enclosure" and not enclosure:
                    enclosure = l.get("href", "")
            author = e.find(f"{ATOM}author")
            items.append({
                "title": strip_html(text_of(e.find(f"{ATOM}title"))),
                "link": link or enclosure,
                "link_kind": "item" if link else ("enclosure" if enclosure else ""),
                "date": parse_date(text_of(e.find(f"{ATOM}published")) or text_of(e.find(f"{ATOM}updated"))),
                "summary": text_of(e.find(f"{ATOM}summary")) or text_of(e.find(f"{ATOM}content")),
                "author": clean_author(text_of(author.find(f"{ATOM}name")) if author is not None else ""),
                "duration": duration_seconds(text_of(e.find(f"{ITUNES}duration"))),
                "guid": text_of(e.find(f"{ATOM}id")),
            })
    else:  # RSS 2.0
        for e in root.iter("item"):
            link = text_of(e.find("link"))
            enc = e.find("enclosure")
            enclosure = (enc.get("url") or "").strip() if enc is not None else ""
            items.append({
                "title": strip_html(text_of(e.find("title"))),
                "link": link or enclosure,
                "link_kind": "item" if link else ("enclosure" if enclosure else ""),
                "date": parse_date(text_of(e.find("pubDate"))),
                "summary": text_of(e.find("description")),
                "author": clean_author(text_of(e.find(f"{DC}creator")) or text_of(e.find("author"))),
                "duration": duration_seconds(text_of(e.find(f"{ITUNES}duration"))),
                "guid": text_of(e.find("guid")),
            })
    return [i for i in items if i["title"]]


def clean_author(raw):
    """Parsed only, never populated: an invented byline is a fabrication with a
    person's name on it. Absence is itself the institutional/independent signal
    (byline sweep, 2026-09-01) and must never be filled in."""
    name = strip_html(raw or "").strip()
    if not name:
        return ""
    name = re.sub(r"^\s*(by|By|BY)[\s:]+", "", name).strip()
    if "@" in name and " " not in name:          # a bare email is not a byline
        return ""
    return name[:80]


def item_id(source, guid, title):
    """A story's identity, independent of whatever href ends up on its row.

    Keyed on the feed's own guid/atom id when it has one — the field RSS and
    Atom define for exactly this purpose — scoped by source so two feeds'
    unscoped sequential guids can't collide. Falls back to the title when a
    feed omits one. Never the link: a channel-page fallback (D 2026-09-02)
    can put the same href on many rows, and hashing that would collapse their
    identities the way the old link-keyed de-dup collapsed Locked On Zags."""
    basis = guid or title
    return hashlib.sha1(f"{source}\x1f{basis}".encode("utf-8")).hexdigest()[:16]


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
    return "%s %d" % (dt.strftime("%b"), dt.day)   # %-d is glibc-only


def on_topic(it, aliases):
    """Admission check for an athletics-wide feed: does this item actually
    mention the team it's filed under? Deterministic name/alias matching —
    the failure mode is over-inclusion (D-040), which is the safe side to
    fail on for a fan site."""
    text = f'{it["title"]} {it["summary"]}'.lower()
    return any(alias in text for alias in aliases)


def detect_sport(title):
    """Per-item sport, for items whose feed didn't declare one (a general
    team feed covering all sports). Unambiguous keyword phrases only — 'if
    and when applicable' means no tag beats a wrong tag (item-types item,
    2026-08-06 CEO note)."""
    low = title.lower()
    for sport, phrases in SPORT_KEYWORDS.items():
        if any(p in low for p in phrases):
            return sport
    return None


def collect(cfg):
    """Fetch all feeds; return (items_by_team, fetch_report)."""
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=cfg["max_item_age_days"])
    by_team, report = {}, []
    team_aliases = {t["name"]: t.get("aliases", []) for t in cfg["teams"]}

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
        fresh, rejected = 0, 0
        athletics_wide = feed.get("topic_scope") == "athletics-wide"
        aliases = team_aliases.get(feed["team"], [])
        for it in parsed:
            if it["date"] is None:
                it["date"] = date_from_url(it["link"])
            if it["date"] is not None and it["date"] < oldest:
                continue
            channel_url = feed.get("channel_url", "")
            if not it["link"]:
                # No item link, no enclosure either (parse_feed already tried
                # both). The channel is a source attribute, never an item one
                # (D 2026-09-02): falling back to it here, rather than baking
                # it into "link" at parse time, is what keeps it out of the
                # id scheme and off every item that doesn't need it.
                if not channel_url:
                    continue  # nothing to send a reader to
                it["link"] = channel_url
                it["link_kind"] = "channel"
            it["channel_url"] = channel_url
            it["source"] = feed["source"]
            it["id"] = item_id(it["source"], it.pop("guid", ""), it["title"])
            if it.get("duration"):
                it["extent"] = format_duration(it["duration"])
            declared = feed["sport"]
            it["sport"] = (detect_sport(it["title"]) or "all") if declared == "all" else declared
            it["medium"] = feed.get("medium", "text")
            it["weight"] = feed.get("weight", 1.0)
            if athletics_wide and not on_topic(it, aliases):
                rejected += 1
                continue
            by_team.setdefault(feed["team"], []).append(it)
            fresh += 1
        note = f"{fresh} recent items"
        if rejected:
            note += f" ({rejected} rejected by topic filter)"
        report.append((feed["team"], feed["url"], "ok", note))
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
        # Generation 1 (2026-09-02): no acquisition-time cap anywhere. The
        # conference slice at max_items_conference was the site's one real
        # discard and it is gone; a page is bounded by items_per_page and a
        # pager instead, which throws nothing away. Ranking stays on ice.
        by_team[team] = merged
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


def medium_tag(it):
    """A headline that opens a 45-minute audio episode makes a different promise
    than one that opens a 300-word article. Label the ones that aren't text, so
    the reader knows what the click costs before they spend it."""
    label = MEDIUM_LABELS.get(it.get("medium", "text"))
    return f'<span class="tag tag-medium">{html.escape(label)}</span>' if label else ""


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
        f'<a class="headline" href="{html.escape(it["link"], quote=True)}" target="_blank" rel="noopener">{html.escape(it["title"])}</a>'
        f'{snip_html}'
        f'<p class="attrib">{big_tag}{medium_tag(it)}{sport_tag}{source} · {rel_time(it["date"], now)}</p>'
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
        f'<a class="headline" href="{html.escape(it["link"], quote=True)}" target="_blank" rel="noopener">{html.escape(it["title"])}</a>'
        f'{snip_html}'
        f'<p class="attrib">{big_tag}{medium_tag(it)}{prefix_html}{source} · {rel_time(it["date"], now)}</p>'
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
        f'<a class="headline" href="{html.escape(item["link"], quote=True)}" target="_blank" rel="noopener">{html.escape(item["title"])}</a>'
        f"{snip_html}"
        f'<p class="attrib">{medium_tag(item)}{html.escape(item["source"])} · {rel_time(item["date"], now)}{also}</p>'
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
        top = items[0]  # newest-first; no ranking (single-recency-stream, 2026-08-06)
        remaining = len(items) - 1
        if remaining > 0:
            more_text = f"{remaining} more {html.escape(team['nickname'])} stories →"
        else:
            more_text = "Team page →"
        body = (
            f'<p class="hl"><a href="{html.escape(top["link"], quote=True)}" target="_blank" rel="noopener">{html.escape(top["title"])}</a></p>'
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
        links.append(f'<a href="https://x.com/{html.escape(team["x"], quote=True)}" target="_blank" rel="noopener">X</a>')
    if team.get("ig"):
        links.append(
            f'<a href="https://www.instagram.com/{html.escape(team["ig"], quote=True)}/" target="_blank" rel="noopener">Instagram</a>')
    if team.get("site"):
        links.append(f'<a href="{html.escape(team["site"], quote=True)}" target="_blank" rel="noopener">Official site</a>')
    return " · ".join(links)


def render_homepage(cfg, by_team, now):
    # No ranked lead card (single-recency-stream, 2026-08-06): nothing here
    # claims to be "the" story. pick_lead/render_lead_card stay defined,
    # unused, so restoring one is a call-site change, not a rebuild.
    teams = sorted(cfg["teams"], key=lambda t: t["name"])
    conf_rest = by_team.get("conference", [])
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
  <p class="updated">Updated {stamp} · refreshes {cadence_phrase(cfg)}</p>
</header>
<main>
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


def render_team_page(team, items, cfg, now, watch=None):
    """One stream, newest first (single-recency-stream, 2026-08-06): no
    ranked pin, no per-sport sections — sport is a tag on each item
    (render_item's in_team_section branch) instead of a heading. The first
    `team_page_visible` items render open; the rest sit behind a native
    <details> toggle, so the whole 45-day window ships in the page (cheap —
    it's text) without dumping it all on the reader at once. No JS.
    split_top/render_team_sport_item stay defined, unused, for restoring
    the ranked/sectioned layout later."""
    if not items:
        body = '<p class="empty">No recent news — check back soon.</p>'
    else:
        visible_n = cfg.get("team_page_visible", 5)
        visible, rest = items[:visible_n], items[visible_n:]
        body = "\n".join(render_item(it, cfg, now, True) for it in visible)
        if rest:
            noun = "story" if len(rest) == 1 else "stories"
            rest_html = "\n".join(render_item(it, cfg, now, True) for it in rest)
            body += (f'\n<details class="more"><summary>Show {len(rest)} more '
                     f'{html.escape(team["nickname"])} {noun}</summary>\n{rest_html}\n</details>')
    # The watch card sits above the news feed. The "reserved for later slices"
    # placeholder does not get promoted with it — a team without a watch page
    # would otherwise lead with a note about work that doesn't exist yet.
    top_block, bottom_block = "", ""
    if watch:
        cheapest = min((c for c in watch["lead"] if c["price"]), key=lambda c: c["price"], default=None)
        price_line = (f' Plans from ${cheapest["price"]:.2f}'.replace(".00", "") + "/mo,"
                      if cheapest else "")
        top_block = (
            '<article class="item watchcard">'
            f'<a class="headline" href="watch/">📺 How to watch the {html.escape(team["nickname"])} '
            f'this season</a>'
            f'<p class="snip">All {watch["games_total"]} games, the channel each one is on, and '
            f'what each way of watching costs.{price_line} plus what you can get free '
            f'over the air.</p></article>')
    else:
        bottom_block = ('<article class="item future">Reserved for later slices: schedule &amp; '
                        "next game · scores · social feed</article>")
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
.watchcard {{ border-left: 4px solid var(--p); border-radius: 0 10px 10px 0; margin-top: 16px; }}
.watchcard .headline {{ color: var(--pt); }}
.watchcard + h2 {{ margin-top: 30px; }}
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
  <p class="updated">Updated {stamp} · refreshes {cadence_phrase(cfg)}</p>
  {follow_html}
</header>
<main>
{top_block}
{body}
{bottom_block}
</main>
<footer>
  <p>Every headline links to its original publisher; snippets are brief excerpts shown with attribution. Full stories belong to their sources.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12 Conference or any university.</p>
  <p><a href="../">← Back to the New PAC City homepage</a></p>
</footer>
</body>
</html>
"""


WATCH_STYLE = """
.wrap { overflow-x: auto; margin-top: 14px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--card); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); }
thead th { vertical-align: bottom; }
tbody tr:last-child td { border-bottom: none; }
.game { min-width: 210px; }
.gd { font-weight: 600; }
.gm { color: var(--muted); font-size: 12.5px; }
.ch { color: var(--muted); font-size: 12.5px; }
.chan { display: inline-block; background: var(--tag-bg); color: var(--accent);
  border-radius: 4px; padding: 1px 6px; font-size: 11.5px; font-weight: 600; }
.colh { min-width: 108px; text-align: center; }
.colh .pv { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
.colh .pl { font-weight: 600; font-size: 13.5px; line-height: 1.25; margin-top: 2px; }
.colh .pr { font-size: 17px; font-weight: 700; margin-top: 6px; }
.colh .mo { font-size: 11.5px; color: var(--muted); }
.cell { text-align: center; font-weight: 700; }
.yes { background: var(--yes-bg); color: var(--yes-ink); }
.no  { background: var(--no-bg);  color: var(--no-ink); }
.un  { background: var(--un-bg);  color: var(--un-ink); }
.na  { background: var(--no-bg);  color: var(--no-ink); font-weight: 400; font-size: 12px; }
tfoot td { font-size: 12.5px; color: var(--muted); border-top: 2px solid var(--line); }
tfoot .cell { font-weight: 600; color: var(--ink); font-size: 13px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12.5px; color: var(--muted); margin-top: 10px; }
.legend b { display: inline-block; width: 18px; height: 18px; border-radius: 4px; text-align: center;
  line-height: 18px; font-size: 12px; margin-right: 5px; vertical-align: -4px; }
.callout { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--p);
  border-radius: 0 10px 10px 0; padding: 11px 14px; margin-top: 14px; font-size: 14px; }
.fn { color: var(--muted); font-size: 12.5px; margin-top: 8px; }
.fn b { color: var(--ink); font-weight: 600; }
tr.cond .gd { font-weight: 500; }
.ifq { color: var(--un-ink); font-weight: 600; }
@media (max-width: 520px) {
  .game { min-width: 0; }
  .colh { min-width: 74px; }
  th, td { padding: 8px 6px; }
  .colh .pl { font-size: 12px; } .colh .pr { font-size: 15px; } .colh .mo { font-size: 10.5px; }
  .gd { font-size: 13px; } .gm, .ch { font-size: 11.5px; }
}
"""

WATCH_COLORS = """
:root { --yes-bg: #e4f0e2; --yes-ink: #2f6b2a; --no-bg: #f2f0ec; --no-ink: #9a9891;
        --un-bg: #fbf0d8; --un-ink: #8a6410; }
@media (prefers-color-scheme: dark) {
  :root { --yes-bg: #1f3a1d; --yes-ink: #8ed086; --no-bg: #22252b; --no-ink: #6e6d68;
          --un-bg: #3a3115; --un-ink: #e0b95a; }
}
"""

CELL_MARK = {"yes": "✓", "no": "✗", "unsure": "?", "n/a": "—"}
CELL_CLASS = {"yes": "yes", "no": "no", "unsure": "un", "n/a": "na"}


def watch_plan_name(col):
    """Sling's plans are already called "Sling Blue"; printing the provider
    above them yields "Sling / Sling Blue"."""
    plan, provider = col["plan"], col["provider_label"]
    return plan[len(provider):].strip() if plan.startswith(provider) else plan


def watch_price(col):
    if col["price"] == 0:
        return "Free"
    if col["price"] is None:
        return "—"
    return f'${col["price"]:.2f}'.replace(".00", "")


def watch_subprice(col):
    """The unit price and the months needed are shown as two facts and never
    multiplied — a season total would assume the fan cancels on time."""
    if col["price"] == 0:
        return "antenna, one-time"
    if col["price"] is None:
        return "price not found"
    months = col["coverage"]["months"]
    return f'per month · {months} month{"s" if months != 1 else ""}' if months else "per month"


def render_watch_table(data, carriage):
    columns = data["lead"]
    head = "".join(
        f'<th class="colh"><div class="pv">{html.escape(c["provider_label"])}</div>'
        f'<div class="pl">{html.escape(watch_plan_name(c))}</div>'
        f'<div class="pr">{watch_price(c)}</div>'
        f'<div class="mo">{watch_subprice(c)}'
        f'{" · reception varies" if c["synthetic"] else ""}</div></th>' for c in columns)
    body = []
    for r in data["rows"]:
        vs = "at" if r["home_away"] == "away" else "vs"
        if r["channel"]:
            chan = f'<span class="chan">{html.escape(r["channel_label"])}</span>'
        else:
            chan = '<span class="ch">no network announced yet</span>'
        cells = []
        for c in columns:
            if not r["channel"]:
                cells.append('<td class="cell na">—</td>')
                continue
            state, _ = watch_data.cell_state(carriage, c, r["channel"])
            cells.append(f'<td class="cell {CELL_CLASS[state]}">{CELL_MARK[state]}</td>')
        cond = ' <span class="ifq">only if they qualify</span>' if r["conditional"] else ""
        body.append(
            f'<tr{" class=cond" if r["conditional"] else ""}>'
            f'<td class="game"><div class="gd">{r["date"][5:].replace("-", "/")} · '
            f'{html.escape(r["opponent"])}</div>'
            f'<div class="gm">{vs} · {html.escape(r["time"])}{cond}</div>'
            f'<div class="ch">{chan}</div></td>{"".join(cells)}</tr>')
    tbd = data["games_tbd"]
    tail = f', plus {tbd} to be determined' if tbd else ""
    foot = "".join(
        f'<td class="cell">{c["coverage"]["yes"]} of {data["games_known"]}{tail}</td>'
        for c in columns)
    return (f'<div class="wrap"><table><thead><tr><th class="game">Game</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody>'
            f'<tfoot><tr><td>Games this gets you</td>{foot}</tr></tfoot></table></div>')


def render_omitted(data):
    """Plans kept out of the table, named in a line of text instead. Two
    reasons, deliberately not merged: 'carries none of your games' is a
    finding about the provider, 'we could not establish it' is a gap in our
    own work, and calling the second one the first would be a false negative
    against a named company."""
    none_ = [c for c in data["columns"] if c["omit"] == "no-games"]
    unk = [c for c in data["columns"] if c["omit"] == "unestablished"]
    out = []
    if none_:
        names = ", ".join(f'{c["provider_label"]} {watch_plan_name(c)}'.strip() for c in none_)
        out.append(f'<p class="fn"><b>Carries none of your games.</b> {html.escape(names)}. '
                   f'Left out of the table because every cell would be empty — not because '
                   f'they are bad services, but because they carry none of the channels your '
                   f'season is on.</p>')
    if unk:
        names = ", ".join(f'{c["provider_label"]} {watch_plan_name(c)}'.strip() for c in unk)
        out.append(f'<p class="fn"><b>We could not establish these.</b> {html.escape(names)}. '
                   f'These publish partial channel lists or put the full line-up behind a '
                   f'sign-in, so we could not confirm a single one of your games on them. '
                   f'That records what our checking found, not a claim about the provider.</p>')
    return "\n".join(out)


def render_watch_page(team, data, carriage, cfg, now):
    school, nickname, slug = html.escape(team["name"]), html.escape(team["nickname"]), team["slug"]
    total, known, tbd = data["games_total"], data["games_known"], data["games_tbd"]
    lead = data["lead"]
    best = max(c["coverage"]["yes"] for c in lead)
    conditional_note = (", plus the conference championship if they get there"
                        if data["conditional"] else "")

    gaps = []
    if tbd:
        gaps.append(f'{tbd} game{"s" if tbd != 1 else ""} ha{"ve" if tbd != 1 else "s"} '
                    f'no announced network yet')
    if best < known:
        gaps.append(f'the best of these three still misses {known - best} '
                    f'of the {known} that do')
    callout = ""
    if gaps:
        callout = (f'<div class="callout"><b>No single option covers all {total} games.</b> '
                   f'{html.escape(" — and ".join(gaps))}.</div>')

    market, verdict, note = watch_data.ANTENNA_MARKETS[data["team"]]
    fn_antenna = (
        f'<p class="fn"><b>&ldquo;Free over the air&rdquo; means the game is broadcast free '
        f'— not that you can necessarily receive it.</b> A ✓ in the antenna column means the '
        f'network is carrying that game over the air nationally. Whether it reaches you depends '
        f'on your address, your antenna and what is between you and the transmitter, and we '
        f'cannot check that from here. <b>Most fans do not live in their team\'s college town</b>, '
        f'so we do not grade these cells by what one town receives.</p>'
        f'<p class="fn"><b>The one signal measurement we have is {html.escape(market)}.</b> '
        f'{html.escape(note)} That is a modelled prediction for a single ZIP code, so real '
        f'reception can be worse even there. Treat it as an example, not as your answer — every '
        f'network publishes a coverage checker for your own address.</p>')
    if verdict in ("weak", "fails"):
        fn_antenna += (
            '<p class="fn">If you are near campus, note that this is one of the weaker markets '
            'we measured — the free column will be more optimistic for you than for most.</p>')

    legend = ('<div class="legend">'
              '<span><b class="yes">✓</b>carries it</span>'
              '<span><b class="no">✗</b>does not carry it</span>'
              '<span><b class="un">?</b>we could not establish it</span>'
              '<span><b class="na">—</b>no network announced yet</span></div>')

    stamp = now.strftime("%b %d, %Y")
    style = BASE_STYLE + WATCH_COLORS + team_root_vars(team) + WATCH_STYLE + """
.teambar { height: 6px; background: var(--p); }
.crumb { max-width: 720px; margin: 0 auto; padding: 14px 16px 0; font-size: 13px; color: var(--muted); }
.crumb a { color: var(--accent); text-decoration: none; }
header { padding: 8px 16px 12px; max-width: 720px; margin: 0 auto; }
h1 { font-size: 30px; letter-spacing: -0.5px; margin-top: 6px; }
h1 .nick { color: var(--s); }
.lede { color: var(--muted); margin-top: 8px; font-size: 15px; }
.updated { color: var(--muted); font-size: 13px; margin-top: 10px; }
"""
    title = f"How to watch {school} {nickname} football — {html.escape(cfg['site_name'])}"
    description = (f"Every {school} {nickname} football game in 2026, the channel it is on, "
                   f"and what each way of watching costs. No recommendations, no commission.")
    og_url = html.escape(cfg.get("site_url", "") + slug + "/watch/", quote=True)

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
<p class="crumb"><a href="../">← {school} {nickname}</a> · New PAC City</p>
<header>
  <h1>How to watch the <span class="nick">{nickname}</span></h1>
  <p class="lede">All {total} games this season{conditional_note}, and what each way of watching
     actually gets you. We don't recommend one — we show what each costs and what it misses.</p>
  <p class="updated">Channel line-ups checked {html.escape(data["carriage_checked"])} ·
     schedule checked {html.escape(data["schedule_checked"])}</p>
</header>
<main>
<h2>The season, three ways</h2>
{render_watch_table(data, carriage)}
{legend}
{callout}
<h2>The rest of the field</h2>
{render_omitted(data)}
<h2>Footnotes</h2>
{fn_antenna}
<p class="fn"><b>Months.</b> We show the monthly price and how many calendar months your games
   span. We don't multiply them together — that would assume you cancel on time.</p>
<p class="fn"><b>Kickoff times</b> are as published by each school, in that school's own
   local time zone.</p>
</main>
<footer>
  <p>Prices and channel line-ups change without notice — check the provider before you buy.
     A carriage dispute can also remove a channel mid-season: in November 2025 a Disney/YouTube TV
     dispute pulled ESPN and ABC for two weeks across peak college-football Saturdays.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12
     Conference or any university. We take no commission on anything listed here.</p>
  <p><a href="../">← Back to {school} {nickname}</a></p>
</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# GENERATION 1 (2026-09-02) — one template, twelve pages, minted from a roster.
#
#   Spec:   projects/new-pac-city/work-generation-1-spec.md
#   Halt 2: decisions/20260902-1154-generation-1s-arrangement-and-f838.md
#
# This replaces the nine-tile homepage and the <details> fold. render_homepage,
# render_team_page and render_tile stay defined and unused — the same way
# pick_lead and story_score do — so reverting is a call-site change.
#
# Two rules govern everything here (spec 1), and both come from the CEO:
#   R1  no layout may depend on a quantity     — legible at 3 items and 30,000
#   R2  no layout may depend on a field's VALUE — only on whether it is there
# ---------------------------------------------------------------------------

HOUSE_LIGHT = "#075E66"   # chosen against the nine schools AND their rivals;
HOUSE_DARK = "#5FD3DC"    # 7.24:1 on the paper ground, which clears AAA

ARRANGEMENT_COMPACT = "compact"
ARRANGEMENT_DETAILED = "detailed"

# No webfont: the site makes zero third-party requests today and generation 1
# does not spend that. The narrow face is furniture only (spec 3.5) because
# condensed letterforms are the first thing to go for a reader with tired eyes.
STACK = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
STACK_NARROW = ('"Helvetica Neue Condensed", "Arial Narrow", "Roboto Condensed", ' + STACK)
STACK_MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'
STACK_SERIF = 'Georgia, "Times New Roman", serif'

GEN1_STYLE = """
:root {
  --paper: #FBFBFA; --ink: #101112; --mid: #3F4245; --soft: #5F6265;
  --rule: #D6D6D4; --hair: #E6E6E4; --house: %(house_light)s;
}
@media (prefers-color-scheme: dark) {
  :root { --paper: #121416; --ink: #ECEDEE; --mid: #C2C6C8; --soft: #9BA1A3;
          --rule: #2E3235; --hair: #212528; --house: %(house_dark)s; }
}
* { box-sizing: border-box; margin: 0; }
html { -webkit-text-size-adjust: 100%%; }
body { background: var(--paper); color: var(--ink); font-family: %(stack)s;
       font-size: 1rem; line-height: 1.5; }
a { color: var(--house); }
a:focus-visible, :focus-visible { outline: 2px solid var(--house); outline-offset: 2px; }

.mast { max-width: 46rem; margin: 0 auto; padding: 1.1rem 1rem 0.7rem;
        border-bottom: 3px double var(--ink); }
.word { font-family: %(narrow)s; font-weight: 700; font-size: 1.5rem;
        letter-spacing: 0.02em; text-transform: uppercase; line-height: 1.1; }
.word a { color: var(--ink); text-decoration: none; }
.sub { font-size: 0.95rem; line-height: 1.45; color: var(--mid); margin-top: 0.3rem; }
.ed { font-family: %(mono)s; font-size: 0.75rem; color: var(--soft);
      margin-top: 0.45rem; letter-spacing: 0.02em; }

.scope { max-width: 46rem; margin: 0 auto; padding: 0.6rem 1rem;
         border-bottom: 1px solid var(--rule);
         display: flex; flex-wrap: wrap; gap: 0.3rem; }
.scope a, .scope span { font-family: %(narrow)s; font-size: 0.82rem;
  text-transform: uppercase; letter-spacing: 0.04em; border: 1px solid var(--rule);
  color: var(--mid); text-decoration: none; padding: 0.55rem 0.6rem; line-height: 1;
  min-height: 2.75rem; display: inline-flex; align-items: center; }
.scope a:hover { border-color: var(--house); color: var(--house); }
.scope [aria-current="page"] { background: var(--ink); color: var(--paper);
  border-color: var(--ink); font-weight: 700; }

main { max-width: 46rem; margin: 0 auto; padding: 0 1rem 2.5rem; }
.phead { font-family: %(mono)s; font-size: 0.78rem; letter-spacing: 0.04em;
  color: var(--soft); padding: 0.75rem 0 0.6rem;
  border-bottom: 1px solid var(--rule); }
.watchlink { display: block; font-family: %(mono)s; font-size: 0.75rem;
  text-transform: uppercase; letter-spacing: 0.06em; padding: 0.7rem 0;
  border-bottom: 1px solid var(--rule); text-decoration: none; }
.watchlink:hover { text-decoration: underline; }

.mark { font-family: %(narrow)s; font-size: 0.82rem; letter-spacing: 0.06em;
  color: var(--house); font-weight: 700;
  padding: 1rem 0 0.45rem; border-bottom: 1px solid var(--ink);
  display: flex; justify-content: space-between; gap: 1rem; }

.row { padding: 0.7rem 0; border-bottom: 1px solid var(--hair); }
.row h3 { font-size: 1.02rem; font-weight: 600; line-height: 1.35;
  letter-spacing: -0.004em; }
.hl { color: var(--ink); text-decoration: none; }
.hl:hover { color: var(--house); text-decoration: underline; }
.maker { font-size: 0.82rem; color: var(--mid); margin-top: 0.3rem; line-height: 1.5; }
.maker .sch { font-family: %(narrow)s; font-weight: 700; text-transform: uppercase;
  font-size: 0.78rem; letter-spacing: 0.05em; color: var(--ink); text-decoration: none;
  border-bottom: 1px solid var(--rule); }
.maker .sch:hover { color: var(--house); border-bottom-color: var(--house); }
.maker .d { color: var(--rule); padding: 0 0.3rem; }
.maker .t { font-family: %(mono)s; font-size: 0.72rem; color: var(--soft); }
.nature { margin-top: 0.4rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }
.nature i { font-style: normal; font-family: %(narrow)s; font-size: 0.78rem;
  border: 1px solid var(--rule); color: var(--mid); padding: 0.1rem 0.4rem;
  letter-spacing: 0.02em; }
.snip { font-family: %(serif)s; font-size: 0.94rem; line-height: 1.55;
  color: var(--mid); margin-top: 0.4rem; }
.when { font-family: %(mono)s; font-size: 0.72rem; color: var(--soft); margin-top: 0.35rem; }
.empty { color: var(--mid); padding: 1.6rem 0; font-size: 0.95rem; }

.pager { border-top: 3px double var(--ink); margin-top: 0.4rem; padding: 0.8rem 0;
  font-family: %(mono)s; font-size: 0.78rem; color: var(--soft);
  display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.pager a { text-decoration: none; padding: 0.5rem 0; }
.pager a:hover { text-decoration: underline; }

footer { max-width: 46rem; margin: 0 auto; padding: 0 1rem 3rem;
  color: var(--soft); font-size: 0.8rem; line-height: 1.6; }
footer p { margin-top: 0.5rem; }
""" % {"house_light": HOUSE_LIGHT, "house_dark": HOUSE_DARK, "stack": STACK,
       "narrow": STACK_NARROW, "mono": STACK_MONO, "serif": STACK_SERIF}


def build_roster(cfg):
    """The twelve keys. A key is a filter, a display name, an arrangement and a
    page size — nothing else. A thirteenth page type is an entry in this list.

    Arrangement and per_page are set explicitly on every entry, with no default
    anywhere: a new page type must answer rather than inherit a choice nobody
    made (CEO, 2026-09-02)."""
    per_page = cfg["items_per_page"]
    roster = [{
        "key": "all", "route": "", "name": "All nine schools",
        "arrangement": ARRANGEMENT_COMPACT, "per_page": per_page,
        "sub": cfg.get("subhead", ""),
    }]
    for t in cfg["teams"]:
        roster.append({
            "key": t["name"], "route": t["slug"], "name": t["name"], "team": t,
            "arrangement": ARRANGEMENT_DETAILED, "per_page": per_page,
        })
    roster.append({
        "key": "conference", "route": "pac12", "name": "The Conference",
        "arrangement": ARRANGEMENT_COMPACT, "per_page": per_page,
    })
    return roster


def items_for(entry, by_team):
    """A key names a filter over the item pool. `all` is every school plus the
    conference, mixed and newest first — the surface no team-siloed page can
    produce. Every other key is one bucket."""
    if entry["key"] != "all":
        return [dict(it, _team=entry["key"]) for it in by_team.get(entry["key"], [])]
    pool = []
    for team, items in by_team.items():
        pool.extend(dict(it, _team=team) for it in items)
    pool.sort(key=lambda i: i["date"] or EPOCH, reverse=True)
    return pool


def apply_floor(pool, by_team, n):
    """Brand attribute 3 — partisan for the nine, neutral among them.

    Every key with anything in the window is guaranteed a row on page 1. A pure
    recency sort lets the best-supplied school crowd out the thinnest one, and
    supply arrives school by school rather than evenly, so neutrality has to be
    an algorithm rather than a promise.

    Obeys R1 and R2: depends on no quantity (nine reserved rows out of whatever
    per_page is) and on no field's value — only on whether a key has anything.
    A key with nothing reserves nothing."""
    if len(pool) <= n:
        return pool
    head, tail = pool[:n], pool[n:]
    for team in [t for t, items in by_team.items() if items]:
        if any(it.get("_team") == team for it in head):
            continue
        newest = next((it for it in tail if it.get("_team") == team), None)
        if newest is None:
            continue
        counts = {}
        for it in head:
            counts[it["_team"]] = counts.get(it["_team"], 0) + 1
        worst = max(counts, key=lambda k: counts[k])
        for i in range(len(head) - 1, -1, -1):      # displace its oldest row
            if head[i]["_team"] == worst:
                tail.insert(0, head.pop(i))
                break
        tail.remove(newest)
        head.append(newest)
    head.sort(key=lambda i: i["date"] or EPOCH, reverse=True)
    tail.sort(key=lambda i: i["date"] or EPOCH, reverse=True)
    return head + tail


# --- editions ---------------------------------------------------------------
# The transport job will hand us a story list carrying its own date. Until it
# does, edition boundaries are derived from each item's own timestamp at 6 a.m.
# and 6 p.m. Pacific. Approximate by exactly the amount that job will make
# exact — and never, in either case, derived from the render time.

def _pacific(dt):
    """US Pacific, without a tzdata dependency: second Sunday in March to first
    Sunday in November is UTC-7, otherwise UTC-8. A fixed -7 would have gone an
    hour wrong on 1 November 2026 and nothing would have said so."""
    y = dt.year
    mar = datetime(y, 3, 8, 10, tzinfo=timezone.utc)          # 2 a.m. local
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 9, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    offset = 7 if dst_start <= dt < dst_end else 8
    return dt - timedelta(hours=offset)


def _day_label(d):
    return "%s %d %s" % (d.strftime("%A"), d.day, d.strftime("%B"))


def _short_day(d):
    return "%s %d %s" % (d.strftime("%a"), d.day, d.strftime("%b"))


def _edition_key(local):
    """(date, hour) of the edition that CARRIES something published at `local`.

    Editions go out at 6 a.m. and 6 p.m. Pacific and each one carries
    everything published since the one before it. So a story's edition is the
    next boundary at or after its timestamp, never the one before it: a
    5:50 a.m. story rides the 6 a.m. edition, a 5 p.m. story rides 6 p.m.
    """
    if local.hour < 6:
        return local.date(), 6
    if local.hour < 18:
        return local.date(), 18
    return local.date() + timedelta(days=1), 6


def _published_edition(ref):
    """(date, hour) of the most recent edition that has actually gone out."""
    if ref.hour < 6:
        return ref.date() - timedelta(days=1), 18
    if ref.hour < 18:
        return ref.date(), 6
    return ref.date(), 18


def _half_of(hour):
    return "morning" if hour == 6 else "evening"


def _at(hour):
    return "6 a.m." if hour == 6 else "6 p.m."


def edition_of(dt, now):
    """(sort key, 'title\x00when') for the edition that carried an item.

    Groups by the edition a story went out IN, not by the window it was
    published in. Those differ by one window, and grouping by the latter is
    what made a freshly-built 6 a.m. site head its newest section "Last
    night": the 6 a.m. run can only see stories published before 6 a.m., and
    every one of those falls in the previous evening's window, so the morning
    bucket had a five-minute window to fill and always filled it with nothing.
    """
    if dt is None:
        return ((datetime.min.date(), 0), "Undated\x00no timestamp")
    local, ref = _pacific(dt), _pacific(now)
    key = _edition_key(local)
    day, hour = key
    live = _published_edition(ref)
    if key > live:
        # Its moment has not arrived. These are on the page only because a run
        # was triggered between editions -- the attended-publish path -- and
        # dating them "this evening" would date them into the future.
        return (key, "Just in\x00since %s · %s" % (_short_day(live[0]), _at(live[1])))
    half, today = _half_of(hour), ref.date()
    if day == today:
        title = "This morning" if half == "morning" else "This evening"
    elif day == today - timedelta(days=1):
        title = "Yesterday morning" if half == "morning" else "Last night"
    else:
        title = "%s, %s" % (_day_label(day), half)
    when = "%s · %s" % (_short_day(day), _at(hour))
    return (key, title + "\x00" + when)


def group_editions(items, now):
    groups, order = {}, []
    for it in items:
        gid, label = edition_of(it["date"], now)
        if gid not in groups:
            groups[gid] = (label, [])
            order.append(gid)
        groups[gid][1].append(it)
    return [groups[g] for g in order]


def fmt_stamp(dt, time_only=False):
    """Absolute, never relative. Twice-daily publishing makes a render-time
    '3h ago' wrong by up to twelve hours, which is the site lying about the one
    field that is always present (3 of 4 site-map lanes, independently)."""
    if dt is None:
        return ""
    local = _pacific(dt)
    hour = local.hour % 12 or 12
    clock = "%d:%02d %s" % (hour, local.minute, "p.m." if local.hour >= 12 else "a.m.")
    return clock if time_only else "%d %s, %s" % (local.day, local.strftime("%b"), clock)


# --- the row ----------------------------------------------------------------

def nature_of(it):
    """What kind of thing is behind this link, and what will happen when I click
    it — brand attribute 4. Every element is present-or-absent.

    The medium renders only when it is KNOWN. An absent medium is unknown, not
    text: defaulting it to "Article" printed a zero-information tag on nearly
    every row and — worse — made a stripped row render as one line in the
    compact arrangement and two in the detailed one, breaking the spec's own
    invariant that a bare row is identical in both. Found by the independent
    check of the build, 2026-09-02."""
    parts = []
    medium = it.get("medium")
    if medium in MEDIUM_LABELS:
        parts.append(MEDIUM_LABELS[medium])
    elif medium == "text":
        parts.append("Article")          # explicit, never by omission
    if it.get("extent"):
        parts.append(it["extent"])
    if it.get("lock") in LOCK_LABELS:
        parts.append(LOCK_LABELS[it["lock"]])
    if it.get("also"):
        parts.append("%d outlets" % (len(it["also"]) + 1))
    return parts


def render_row(it, entry, cfg, now):
    """One row, three lines and an optional snippet, in a fixed order.

    A line renders only if at least one of its elements has content; a line with
    no content is not rendered at all. The extreme case — a headline, a link and
    nothing else — is a valid row and must look deliberate, because under the
    coming curation mechanism it may well be common."""
    esc = html.escape
    detailed = entry["arrangement"] == ARRANGEMENT_DETAILED

    maker = []
    # The school is a link on the mixed feed and suppressed on a team page,
    # where the masthead, the scope bar and the page head have each said it.
    if entry["key"] == "all" and it.get("_team") in TEAM_SLUGS:
        maker.append('<a class="sch" href="/%s/">%s</a>'
                     % (esc(TEAM_SLUGS[it["_team"]], quote=True), esc(it["_team"])))
    if it.get("source"):
        # Two links, not one (D 2026-09-02), detailed rows only: the outlet
        # name goes to its channel page when the source has one — suppressed
        # when the headline already fell back to that same URL, since two
        # links to one destination in a row is noise.
        channel_url = it.get("channel_url")
        if detailed and channel_url and channel_url != it["link"]:
            maker.append('<a href="%s" target="_blank" rel="noopener">%s</a>'
                         % (esc(channel_url, quote=True), esc(it["source"])))
        else:
            maker.append("<span>%s</span>" % esc(it["source"]))
    if it.get("author"):
        maker.append('<span class="by">%s</span>' % esc(it["author"]))

    out = ['<article class="row"><h3>'
           '<a class="hl" href="%s" target="_blank" rel="noopener">%s</a></h3>'
           % (esc(it["link"], quote=True), esc(it["title"]))]

    if detailed:
        if maker:
            out.append('<p class="maker">%s</p>' % '<span class="d">·</span>'.join(maker))
        nature = nature_of(it)
        if nature:
            out.append('<p class="nature">%s</p>'
                       % "".join("<i>%s</i>" % esc(n) for n in nature))
        snip = snippet_of(it.get("summary", ""), cfg["snippet_max_chars"])
        if snip:
            out.append('<p class="snip">%s</p>' % esc(snip))
        stamp = fmt_stamp(it["date"])
        if stamp:
            out.append('<p class="when">%s</p>' % esc(stamp))
    else:
        line = list(maker)
        # Compact drops the "Article" marker (a text item on a text-heavy page
        # needs no label) and keeps everything that changes what a click costs.
        for n in nature_of(it):
            if n != "Article":
                line.append("<span>%s</span>" % esc(n))
        stamp = fmt_stamp(it["date"], time_only=True)
        if stamp:
            line.append('<span class="t">%s</span>' % esc(stamp))
        if line:
            out.append('<p class="maker">%s</p>' % '<span class="d">·</span>'.join(line))

    out.append("</article>")
    return "".join(out)


# --- the page ---------------------------------------------------------------

def render_scope_bar(roster, current):
    """The site's entire navigation and its entire filtering mechanism: the
    twelve pages ARE the filters.

    It wraps and never scrolls sideways (amended 2026-09-02 at the CEO's report
    that the links did not fit) — a nav you have to discover by swiping is worse
    than one costing a story's height. `All` is anchored first and `Pac-12`
    last, so the two ends do not move as the rows reflow."""
    out = []
    for e in roster:
        if e["key"] == "all":
            label = "All"
        elif e["key"] == "conference":
            label = "Pac-12"
        else:
            label = e["team"]["nickname"]
        if e["key"] == current:
            out.append('<span aria-current="page">%s</span>' % html.escape(label))
        else:
            href = "/" if e["route"] == "" else "/%s/" % e["route"]
            out.append('<a href="%s">%s</a>'
                       % (html.escape(href, quote=True), html.escape(label)))
    return '<nav class="scope" aria-label="Sections">%s</nav>' % "".join(out)


def render_pager(entry, page, total):
    """Bounds a page without discarding an item. The caps this replaces did the
    opposite on the conference key and nothing at all on the nine."""
    if total <= 1:
        return ""
    base = "/" if entry["route"] == "" else "/%s/" % entry["route"]
    url = lambda n: base if n == 1 else "%spage/%d/" % (base, n)
    newer = '<a href="%s">← Newer</a>' % url(page - 1) if page > 1 else "<span></span>"
    older = '<a href="%s">Older →</a>' % url(page + 1) if page < total else "<span></span>"
    return '<nav class="pager">%s<span>Page %d of %d</span>%s</nav>' % (newer, page, total, older)


REFRESH_SCHEDULE_DEFAULT = ["06:00", "18:00"]


def _schedule(cfg):
    """The fetch schedule in Pacific wall-clock, as sorted (hour, minute) pairs.

    One easy-to-change setting, as spec 5 asks. It is the single source for
    every cadence claim the site makes, so no page can print a schedule the
    site does not run on — the honesty failure the 2026-09-02 check caught."""
    raw = cfg.get("refresh_schedule_pacific") or REFRESH_SCHEDULE_DEFAULT
    return sorted(tuple(int(p) for p in t.split(":")) for t in raw)


def _clock(hour, minute):
    return "%d:%02d %s" % (hour % 12 or 12, minute, "p.m." if hour >= 12 else "a.m.")


def cadence_phrase(cfg):
    n = len(_schedule(cfg))
    return {1: "once a day", 2: "twice a day"}.get(n, "%d times a day" % n)


def next_update(local, cfg):
    """The next scheduled fetch strictly after `local`, a Pacific datetime."""
    for h, m in _schedule(cfg):
        if (local.hour, local.minute) < (h, m):
            return _clock(h, m)
    h, m = _schedule(cfg)[0]
    return _clock(h, m)


def edition_line(cfg, generated_at):
    """The masthead's edition line -- the STORY LIST's own date, never the
    render's (generation‑1 spec 3.1; transport plan lens "Observability").

    A render that runs hours after its fetch must say when the news is FROM,
    and a run that never happened must leave the page frozen and saying so.
    Both are now real: the fetch half writes `generated_at` into the story
    list and the render half reads it back as its only clock.

    Degrades exactly as the spec requires: with no date at all this renders
    NOTHING rather than falling back to the render time, because that is the
    specific lie this element exists to replace. The first draft hardcoded
    "updated 6:00 a.m. next update 6:00 p.m." against a site that ran every
    six hours; the times are honest here only because step 5 of the transport
    job made the schedule real, and they are read from the config either way."""
    if generated_at is None:
        return ""
    local = _pacific(generated_at)
    return "%s, %d %s · updated %s · next update %s Pacific" % (
        local.strftime("%A"), local.day, local.strftime("%B"),
        _clock(local.hour, local.minute), next_update(local, cfg))


def render_index(entry, page_items, roster, cfg, now, page, total, watch_slug=None,
                 list_date=None):
    esc = html.escape
    stamp = edition_line(cfg, list_date)
    sub = ('<p class="sub">%s</p>' % esc(entry["sub"])
           if entry.get("sub") and page == 1 else "")

    if page_items:
        body = []
        for label, items in group_editions(page_items, now):
            title, when = label.split("\x00")
            body.append('<div class="mark"><span>%s</span><span>%s</span></div>'
                        % (esc(title), esc(when)))
            body.extend(render_row(it, entry, cfg, now) for it in items)
        body_html = "".join(body)
    else:
        # One shared empty state. The page still exists and is still linked:
        # a missing page is a broken promise, an empty one is a true statement.
        body_html = ('<p class="empty">No items in the last %d days. '
                     'The next update is at %s Pacific.</p>'
                     % (cfg["max_item_age_days"], next_update(_pacific(now), cfg)))

    watch_html = ""
    if watch_slug:
        # The shipped How to watch feature is reached today by a card rendered
        # INSIDE the story stream; a story list has no place for a card that is
        # not a story, so it becomes a line above the list instead of vanishing.
        watch_html = ('<a class="watchlink" href="/%s/watch/">'
                      '\U0001F4FA How to watch this season →</a>'
                      % esc(watch_slug, quote=True))

    title = cfg["site_name"] if entry["key"] == "all" else "%s — %s" % (entry["name"], cfg["site_name"])
    if page > 1:
        title = "%s (page %d)" % (title, page)
    count = len(page_items)

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<meta name="description" content="%(sub_text)s">
<link rel="icon" href="%(favicon)s">
<meta property="og:title" content="%(title)s">
<meta property="og:description" content="%(sub_text)s">
<meta property="og:type" content="website">
<style>%(style)s</style>
</head>
<body>
<header class="mast">
  <p class="word"><a href="/">New PAC City</a></p>
  %(sub)s
  %(ed)s
</header>
%(scope)s
<main>
  <p class="phead">%(name)s · %(count)d item%(plural)s</p>
  %(watch)s
  <div class="list">%(body)s</div>
  %(pager)s
</main>
%(footer)s
</body>
</html>
""" % {
        "title": esc(title),
        "sub_text": esc(cfg.get("subhead", "")),
        "favicon": FAVICON,
        "style": GEN1_STYLE,
        "sub": sub,
        "ed": ('<p class="ed">%s</p>' % esc(stamp)) if stamp else "",
        "scope": render_scope_bar(roster, entry["key"]),
        "name": esc(entry["name"]),
        "count": count,
        "plural": "" if count == 1 else "s",
        "watch": watch_html,
        "body": body_html,
        "pager": render_pager(entry, page, total),
        "footer": render_footer(cfg),
    }


def render_footer(cfg):
    """One sentence on what the site is, a link to /about/, and the source
    count (spec 3.6). No coverage census, no per-school statistics — three of
    four site-map lanes proposed exactly that page and it was rejected."""
    return ("""<footer>
  <p>Every headline links to its original publisher; snippets are brief excerpts shown with
  attribution. Full stories belong to their sources.</p>
  <p>New PAC City is an independent fan site, not affiliated with or endorsed by the Pac-12
  Conference or any university. Gathering %d sources. <a href="/about/">About this site</a>.</p>
</footer>""" % len(cfg["feeds"]))


def render_about(roster, cfg, now):
    """The twelfth page, and the only one that is not a story list.

    Brand attribute 5 — visibly made by a person — lives here. It deliberately
    does NOT claim that everything was read by a person before it published:
    the reviewer that would make that true is a later phase and is not built,
    and the claim is earned when it runs."""
    esc = html.escape
    body = """
  <p class="about">New PAC City gathers what is being written, recorded and filmed about the nine
  schools of the new Pac-12, puts it in one place, and sends you to whoever made it. Every headline
  on this site is a link out. We do not host anyone else's reporting and we never will.</p>
  <p class="about">It is an independent fan site. It is not the conference, it is not a school, and
  it is not affiliated with either. It takes no money from anyone it links to.</p>
  <p class="about">The site reads %(n)d feeds and shows everything from the last %(days)d days,
  newest first. It is partisan for the nine and neutral among them: every school with anything to
  show reaches the front page. Where a school's coverage is thin, its page is short — we would rather
  show you less than pad it out.</p>
  <p class="about">Alongside each headline we try to tell you what is behind it before you click:
  who made it, what kind of thing it is, how long it is, and whether it is paywalled. Where we do not
  know, we say nothing rather than guess.</p>
""" % {"n": len(cfg["feeds"]), "days": cfg["max_item_age_days"]}

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About — %(site)s</title>
<meta name="description" content="%(sub)s">
<link rel="icon" href="%(favicon)s">
<style>%(style)s
.about { font-family: %(serif)s; font-size: 1.02rem; line-height: 1.6; color: var(--mid);
         margin-top: 1rem; max-width: 40rem; }
</style>
</head>
<body>
<header class="mast">
  <p class="word"><a href="/">New PAC City</a></p>
  <p class="sub">%(sub)s</p>
</header>
%(scope)s
<main>
  <p class="phead">About</p>
  %(body)s
</main>
%(footer)s
</body>
</html>
""" % {
        "site": esc(cfg["site_name"]),
        "sub": esc(cfg.get("subhead", "")),
        "favicon": FAVICON,
        "style": GEN1_STYLE,
        "serif": STACK_SERIF,
        "scope": render_scope_bar(roster, None),
        "body": body,
        "footer": render_footer(cfg),
    }


# --- the story list ---------------------------------------------------------
# The fetch/render split (item-review transport, step 3). collect()'s output,
# serialized: what fetch.py writes and render.py reads, so the render half can
# run on its own, hours later, on a machine with no reason to touch the
# network, which is now where it actually runs: the Hearth fetches and
# pushes the list, CI renders it. `main()` below still runs both halves in
# one process for local work, but it does so by writing the list and reading
# it back, never by handing
# `by_team` to the renderer directly, so the split is real rather than
# notional. The story list also carries the fetch's own timestamp, which the
# render half uses as "now" instead of asking the clock again — the
# Observability lens (transport plan §1): a render run hours after its fetch
# must say when the news is FROM, not when the page happened to be built.

def serialize_story_list(by_team, generated_at):
    def ser_item(it):
        out = dict(it)
        out["date"] = it["date"].isoformat() if it["date"] else None
        return out
    return {
        "generated_at": generated_at.isoformat(),
        "teams": {team: [ser_item(it) for it in items] for team, items in by_team.items()},
    }


def deserialize_story_list(data):
    def deser_item(it):
        out = dict(it)
        out["date"] = datetime.fromisoformat(it["date"]) if it["date"] else None
        return out
    by_team = {team: [deser_item(it) for it in items] for team, items in data["teams"].items()}
    at = data.get("generated_at")
    return by_team, (datetime.fromisoformat(at) if at else None)


def write_story_list(by_team, generated_at, path):
    path.write_text(
        json.dumps(serialize_story_list(by_team, generated_at), indent=2, ensure_ascii=False),
        encoding="utf-8")


def read_story_list(path):
    return deserialize_story_list(json.loads(path.read_text(encoding="utf-8")))


def render(cfg, list_path):
    """The render half: reads a story list and never touches the network."""
    by_team, now = read_story_list(list_path)
    total_items = sum(len(v) for v in by_team.values())
    if total_items == 0:
        print("ERROR: story list has no items; keeping the previous page.", file=sys.stderr)
        sys.exit(1)
    # The story list's own date is what the masthead prints, and it stays
    # distinct from the reference point editions are grouped against. A list
    # with no date of its own prints NOTHING (spec 3.1) rather than falling
    # back to the render clock, but the page still needs something to group
    # by, so grouping borrows the newest item's date. Never datetime.now():
    # the render half has no business asking the clock at all.
    list_date = now
    if now is None:
        now = max((i["date"] for v in by_team.values() for i in v if i.get("date")),
                  default=EPOCH)
    out = HERE / cfg.get("output_dir", "site")
    out.mkdir(exist_ok=True)
    TEAM_SLUGS.update({t["name"]: t["slug"] for t in cfg["teams"]})

    carriage, _ = watch_data.load()
    roster = build_roster(cfg)
    pages_written, watch_pages = 0, 0

    for entry in roster:
        items = items_for(entry, by_team)
        if entry["key"] == "all":
            items = apply_floor(items, by_team, entry["per_page"])
        per = entry["per_page"]
        total = max(1, (len(items) + per - 1) // per)
        base = out if entry["route"] == "" else out / entry["route"]
        base.mkdir(parents=True, exist_ok=True)

        watch = watch_data.for_team_name(entry["name"]) if "team" in entry else None
        watch_slug = entry["route"] if watch else None

        for page in range(1, total + 1):
            chunk = items[(page - 1) * per: page * per]
            target = base if page == 1 else base / "page" / str(page)
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.html").write_text(
                render_index(entry, chunk, roster, cfg, now, page, total, watch_slug,
                             list_date=list_date),
                encoding="utf-8")
            pages_written += 1

        if watch:
            watch_dir = base / "watch"
            watch_dir.mkdir(exist_ok=True)
            (watch_dir / "index.html").write_text(
                render_watch_page(entry["team"], watch, carriage, cfg, now), encoding="utf-8")
            watch_pages += 1

    about_dir = out / "about"
    about_dir.mkdir(exist_ok=True)
    (about_dir / "index.html").write_text(render_about(roster, cfg, now), encoding="utf-8")

    print(f"Wrote {pages_written} index pages across {len(roster)} keys + about "
          f"+ {watch_pages} watch pages — {total_items} items.")


def main():
    """Fetch, then render — one command, for local work only.

    Nothing scheduled calls this any more. Publishing is publish.py on the
    Hearth (fetch, commit, push) and render.py in CI (pages), which is what
    keeps a single path onto the site and puts a readable story list on it.
    Splits internally into the same two steps those two run separately."""
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "feeds.json"
    cfg = json.loads(cfg_path.read_text())
    list_path = HERE / "story-list.json"
    now = datetime.now(timezone.utc)
    by_team, report = collect(cfg)
    ok = [r for r in report if r[2] == "ok"]
    for team, url, status, detail in report:
        print(f"[{status}] {team}: {url} — {detail}")
    total_items = sum(len(v) for v in by_team.values())
    if not ok or total_items == 0:
        print("ERROR: no feed produced any items; keeping the previous page.", file=sys.stderr)
        sys.exit(1)
    write_story_list(by_team, now, list_path)
    print(f"Wrote story-list.json — {total_items} items from {len(ok)}/{len(report)} feeds.")
    render(cfg, list_path)


if __name__ == "__main__":
    main()
