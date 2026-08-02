#!/usr/bin/env python3
"""How-to-watch: join the schedule to the carriage fact base.

Two committed data files describe the season from different angles —
data/schedule-2026.json says which channel each game is on, and
data/carriage-2026.json says which buyable PLAN carries each channel. This
module turns them into the rows and columns the watch page renders.

Everything here exists because the two files do not join cleanly on their own:

  * channel names differ between them and within the schedule itself
    ("CBS Sports Network" and "CBSSN" are the same channel, 19 games apart);
  * kickoff times carry no time zone, and each school publishes in its own;
  * the free over-the-air path has no provider record at all.

Standard library only, same as build.py. No fetching — both inputs are
snapshots, and the watch page is a static render of what we already checked.
"""

import json
import re
from pathlib import Path


def _data_dir():
    """Find data/ whether this module sits beside it or one level down, so the
    same file works from a src/ subdirectory and from a repository root."""
    here = Path(__file__).parent
    for candidate in (here / "data", here.parent / "data"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("cannot locate the data/ directory next to watch_data.py")


DATA = _data_dir()

# --- Channel identity -------------------------------------------------------
# The schedule's `tv` field carries 14 spellings for 11 channels; `tv_raw` is
# worse (scraped logo filenames, one typo). Match on identity, never on
# substring: Philo carries "CBS News 24/7", which is a news channel and not the
# CBS broadcast affiliate, and a substring match silently treats them as one.
CHANNEL_ALIAS = {
    "cbs": "CBS",
    "cbssn": "CBSSN", "cbs sports network": "CBSSN", "cbs sports net": "CBSSN",
    "cw": "CW", "the cw": "CW",
    "usa": "USA", "usa network": "USA",
    "espn": "ESPN",
    "btn": "BTN", "big ten network": "BTN",
    "fs1": "FS1",
    "tnt": "TNT", "tnt sports (hbo max)": "TNT",
    "mw+": "MWPlus", "mwplus": "MWPlus",
    # In the schedule but NOT in the carriage fact base — both free over the
    # air, so the antenna column answers them and no streaming plan can.
    "fox": "FOX",
    "nbc": "NBC",
}

# Channels the carriage file has no column for. Not an omission to paper over:
# the page must say so rather than render an empty row as "not carried".
OFF_MATRIX = {"FOX", "NBC"}

CHANNEL_LABEL = {
    "CBS": "CBS", "CBSSN": "CBS Sports Network", "CW": "The CW",
    "USA": "USA Network", "ESPN": "ESPN", "BTN": "Big Ten Network",
    "FS1": "FS1", "TNT": "TNT", "MWPlus": "MW+", "FOX": "FOX", "NBC": "NBC",
}

# --- Kickoff times ----------------------------------------------------------
# Each school's own schedule page publishes kickoff in that school's home zone,
# and the data does not record which. Established by cross-check:
# against a schedule published uniformly in Pacific time, Texas State ran a
# consistent 2h offset (Central) and Utah State, Colorado State and Boise State
# a consistent 1h offset (Mountain). The four Pacific schools are Pacific under
# either reading, so labelling is safe for all eight.
#
# We LABEL rather than convert. A Beavers fan reading /beavs/watch/ wants the
# time their team's site published; silently shifting it would invent a fact.
HOME_ZONE = {
    "boise-state": "MT", "colorado-state": "MT", "utah-state": "MT",
    "texas-state": "CT",
    "fresno-state": "PT", "oregon-state": "PT",
    "san-diego-state": "PT", "washington-state": "PT",
}

TIME_UNKNOWN = {"tba", "tbd", ""}
ZONE_RE = re.compile(r"\b([PMCE][DS]?T)\b", re.I)


def normalize_time(raw, team_id):
    """Return (display_time, is_known). Formats vary — '4 p.m.', '4:00 PM',
    '4:15 PM PDT', 'TBA', and one '4:00 or 8:00 PM'. Output is one shape with
    an explicit zone, because an unlabelled hour is wrong for most readers."""
    s = (raw or "").strip()
    if s.lower() in TIME_UNKNOWN:
        return "Time TBA", False
    zone = HOME_ZONE.get(team_id, "")
    found = ZONE_RE.search(s)
    if found:                                  # already zoned; keep the source's
        zone = found.group(1).upper()
        s = ZONE_RE.sub("", s).strip()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", s, re.I)
    if not m:
        return f"{s} {zone}".strip(), True     # e.g. '4:00 or 8:00' — pass through, labelled
    hour, minute, half = m.group(1), m.group(2) or "00", m.group(3).lower()
    return f"{hour}:{minute} {half}m {zone}".strip(), True


# --- The free option --------------------------------------------------------
# carriage-2026.json holds six streaming providers and no over-the-air entry,
# yet the settled design pre-selects a free column.
#
# These eight verdicts are modelled per home ZIP, and they are NOT the answer
# to "can I watch this free" — they are one reference point. Most fans do not
# live in their team's college town, so the reach of an antenna in that town
# cannot stand in for every reader. An earlier version graded each cell by the
# home market's signal and was wrong for exactly that reason.
#
# What is actually true per cell is simpler and stronger: the game IS broadcast
# free over the air. Whether YOU can receive it depends on where you are. So the
# cell states the broadcast fact and a footnote carries the reception risk,
# with the home market quoted as the one signal measurement we have.
ANTENNA_MARKETS = {
    "boise-state": ("Boise 83725", "strong", "All five networks good on an indoor antenna."),
    "san-diego-state": ("San Diego 92182", "strong", "All five good; transmitters on two mountains ~180° apart."),
    "fresno-state": ("Fresno 93740", "strong", "All good; FOX is double the distance and off-axis."),
    "oregon-state": ("Corvallis 97331", "strong", "All five workable. The CW exists only as a subchannel of NBC's KMTR."),
    "texas-state": ("San Marcos 78666", "fair", "All five only fair — attic or small outdoor antenna."),
    "colorado-state": ("Fort Collins 80521", "fair", "CBS and The CW need a large outdoor antenna. FOX is easy."),
    "utah-state": ("Logan 84322", "weak", "Salt Lake City signal unusable across the Wasatch; needs the Cache County translator. CW unconfirmed — sources conflict."),
    "washington-state": ("Pullman 99163", "fails", "The free path largely fails. CW and NBC unreachable even with a large antenna; CBS genuinely uncertain."),
}
OTA_CHANNELS = {"CBS", "CW", "FOX", "NBC"}


def antenna_column(team_id):
    """A synthetic plan column for the free path. Priced $0. It carries the
    home market's verdict for the footnote to quote, but that verdict never
    decides a cell — see the note above ANTENNA_MARKETS."""
    market, verdict, note = ANTENNA_MARKETS[team_id]
    return {
        "provider": "antenna", "provider_label": "Antenna",
        "plan": "One-time hardware", "price": 0.0, "price_status": "published",
        "synthetic": True, "market": market, "verdict": verdict, "note": note,
    }


# --- Loading ----------------------------------------------------------------

def load():
    carriage = json.loads((DATA / "carriage-2026.json").read_text())
    schedule = json.loads((DATA / "schedule-2026.json").read_text())
    return carriage, schedule


# feeds.json names the teams; schedule-2026.json keys them by slug. Gonzaga is
# absent on purpose — the watch feature is football-only, so eight teams have a
# page and Gonzaga simply has no watch link.
TEAM_BY_NAME = {
    "Boise State": "boise-state", "Colorado State": "colorado-state",
    "Fresno State": "fresno-state", "Oregon State": "oregon-state",
    "San Diego State": "san-diego-state", "Texas State": "texas-state",
    "Utah State": "utah-state", "Washington State": "washington-state",
}


PROVIDER_LABEL = {
    "directv-stream": "DirecTV Stream", "fubo": "Fubo", "hulu-live-tv": "Hulu + Live TV",
    "philo": "Philo", "sling": "Sling", "youtube-tv": "YouTube TV",
}


def plans(carriage):
    """Every buyable plan across the file, cheapest first. 17 of them, which is
    why the design pre-selects columns rather than rendering all of them."""
    out = []
    for pid, prov in carriage["providers"].items():
        for name, meta in (prov.get("plans") or {}).items():
            out.append({
                "provider": pid, "provider_label": PROVIDER_LABEL.get(pid, pid),
                "plan": name, "price": meta.get("price"),
                "price_status": meta.get("price_status"),
                "promo": meta.get("promo"), "synthetic": False,
            })
    return sorted(out, key=lambda p: (p["price"] is None, p["price"] or 0))


def cell_state(carriage, column, channel):
    """One of the three agreed states — 'yes' / 'no' / 'unsure' — plus 'n/a'
    for a channel this column structurally cannot answer.

    'unsure' exists because a cell we never observed must be recorded as
    unknown and never rounded down to 'no'. A wrong 'no' tells someone their
    money is wasted on a service that would have worked."""
    if column["synthetic"]:                       # the antenna column
        if channel not in OTA_CHANNELS:
            return "no", "Not broadcast over the air on any network."
        # Broadcast free over the air, everywhere this network reaches. Whether
        # a given reader can receive it is a question about their address, not
        # about the game, and it belongs in a footnote rather than in a cell
        # graded by one town's signal.
        return "yes", "Broadcast free over the air — reception depends on where you live."
    if channel in OFF_MATRIX:
        # We never collected carriage for FOX or NBC, so we do not know whether
        # this plan carries them. That is "unsure", not "not applicable" —
        # rendering it as a dash would claim the question doesn't apply, when in
        # fact we simply never asked it.
        return "unsure", f"We have no carriage data for {CHANNEL_LABEL[channel]}."
    cell = carriage["providers"][column["provider"]]["channels"].get(channel)
    if cell is None:
        return "unsure", "Never checked."
    per_plan = cell.get("plans") or {}
    if column["plan"] in per_plan:
        value = per_plan[column["plan"]]
        if value is True:
            return "yes", cell.get("note") or ""
        if value is False:
            return "no", cell.get("note") or ""
        return "unsure", cell.get("tier_note") or cell.get("unknown_reason") or "Plan not established."
    if cell.get("carried") is True:               # carried, but no tier observed
        return "unsure", "Carried, but we did not observe which plan."
    if cell.get("carried") is False:
        return "no", cell.get("note") or ""
    return "unsure", cell.get("unknown_reason") or "Not established."


def games(schedule, team_id):
    """A team's season as watch-page rows, newest last (schedule order)."""
    rows = []
    for g in schedule["teams"][team_id]["games"]:
        raw = (g.get("tv") or "").strip()
        channel = CHANNEL_ALIAS.get(raw.lower())
        display, timed = normalize_time(g.get("time"), team_id)
        rows.append({
            "date": g["date"], "time": display, "time_known": timed,
            "opponent": g["opponent"], "home_away": g.get("home_away"),
            # The conference championship sits on a team's schedule as a
            # placeholder at a neutral site, but the team plays it only if it
            # qualifies. Counting it as a game a subscription buys you would
            # overstate every column by one.
            "conditional": bool(g.get("placeholder")) and g.get("home_away") == "neutral",
            "channel": channel, "channel_raw": raw,
            "channel_label": CHANNEL_LABEL.get(channel, raw) if channel else None,
            "off_matrix": channel in OFF_MATRIX if channel else False,
            "tv_confidence": g.get("tv_confidence") or g.get("confidence"),
        })
    return rows


def coverage(carriage, column, rows):
    """How many of a team's games this column actually gets you, and over how
    many calendar months you would have to hold it.

    The months figure is deliberately NOT multiplied by the price: a computed
    season total embeds an assumption about cancelling on time and goes wrong
    silently. Show the unit price and the months as two facts, and let the
    reader do a multiplication they can see."""
    counts = {"yes": 0, "no": 0, "unsure": 0, "n/a": 0, "no_channel": 0}
    months = set()
    for r in rows:
        if r["conditional"]:      # counted nowhere; the team may not play it
            continue
        if not r["channel"]:
            counts["no_channel"] += 1
            continue
        state = cell_state(carriage, column, r["channel"])[0]
        counts[state] += 1
        if state == "yes":
            months.add(r["date"][:7])
    counts["months"] = len(months)
    return counts


def latest_checked(providers, key):
    """The newest date anything in the carriage file claims to have been
    checked. Cells were re-checked the day after collection started."""
    dates = []
    for prov in providers:
        if prov.get("checked"):
            dates.append(prov["checked"])
        for cell in (prov.get(key) or {}).values():
            if cell.get("checked"):
                dates.append(cell["checked"])
    return max(dates) if dates else ""


def preselect(columns):
    """The three columns the page leads with: the free path,
    the cheapest that gets you some of the season, and the cheapest that gets
    you the most of it. Computed per team rather than fixed, because the third
    one genuinely differs — San Diego State and Utah State both play a Big Ten
    Network game, and BTN is on YouTube TV's $82.99 Base plan but not on the
    $64.99 plan named "Sports".

    Three is also the most a phone fits: measured at 390px, three columns need
    exactly the 362px available and a fourth needs 399px."""
    antenna = columns[0]
    priced = [c for c in columns[1:] if c["price"] is not None]
    if not priced:
        return [antenna]
    best = max(c["coverage"]["yes"] for c in priced)
    full = min((c for c in priced if c["coverage"]["yes"] == best), key=lambda c: c["price"])

    # The middle column is the CHEAPEST plan that still covers a real share of
    # the season — a quarter of the announced games. Picking "most games below
    # the full price" instead would land on a second tier of the same provider
    # $18 apart, which spans none of the trade-off the page exists to show.
    floor = max(1, -(-best // 4))
    cheaper = [c for c in priced if c["price"] < full["price"]]
    qualifying = [c for c in cheaper if c["coverage"]["yes"] >= floor]
    if qualifying:
        partial = min(qualifying, key=lambda c: (c["price"], -c["coverage"]["yes"]))
    else:
        usable = [c for c in cheaper if c["coverage"]["yes"] > 0]
        if not usable:
            return [antenna, full]
        partial = max(usable, key=lambda c: (c["coverage"]["yes"], -c["price"]))
    return [antenna, partial, full]


def omission(column):
    """Why a column is kept out of the comparison table: anything we cannot
    show a single game for belongs in a line of text under the table, not in a
    column of its own. Returns None to keep it.

    Two reasons, kept apart deliberately — 'carries none of your games' is a
    finding, 'we could not establish anything' is a gap in our work, and
    collapsing them would publish an unsupported negative about a named
    company.

    Pre-selected columns are never omitted: in Pullman and Logan the free
    column is all question marks, and that is the most useful thing the page
    can tell those fans."""
    cov = column["coverage"]
    if cov["yes"] > 0:
        return None
    if cov["unsure"] == 0:
        return "no-games"
    return "unestablished"


def build(team_id):
    carriage, schedule = load()
    rows = games(schedule, team_id)
    columns = [antenna_column(team_id)] + plans(carriage)
    for c in columns:
        c["coverage"] = coverage(carriage, c, rows)
    lead = preselect(columns)
    lead_ids = {(c["provider"], c["plan"]) for c in lead}
    for c in columns:
        c["preselected"] = (c["provider"], c["plan"]) in lead_ids
        c["omit"] = None if c["preselected"] else omission(c)
    counted = [r for r in rows if not r["conditional"]]
    known = sum(1 for r in counted if r["channel"])
    return {
        "team": team_id, "rows": rows, "columns": columns, "lead": lead,
        "games_total": len(counted), "games_known": known,
        "games_tbd": len(counted) - known,
        "conditional": len(rows) - len(counted),
        # The file headers carry the date collection STARTED; several cells and
        # games were re-checked the day after. Stamp the page with the latest
        # date the data itself claims, so "checked on" is not older than the
        # freshest thing on the page.
        "carriage_checked": latest_checked(carriage["providers"].values(), "channels"),
        "schedule_checked": max(
            (g.get("checked") for t in schedule["teams"].values() for g in t["games"]
             if g.get("checked")), default=schedule["collected"]),
    }


def for_team_name(name):
    """Entry point for build.py. Returns None for a team with no football
    schedule, which is how Gonzaga ends up with no watch page."""
    team_id = TEAM_BY_NAME.get(name)
    return build(team_id) if team_id else None


if __name__ == "__main__":
    import sys
    team = sys.argv[1] if len(sys.argv) > 1 else "oregon-state"
    out = build(team)
    for r in out["rows"]:
        print(f'{r["date"]}  {r["time"]:>16}  {r["opponent"][:22]:24} {r["channel_label"] or "— no network yet"}')
    print()
    for c in out["columns"]:
        cov = c["coverage"]
        price = "free" if c["price"] == 0 else (f'${c["price"]}' if c["price"] is not None else "price not found")
        print(f'{c["provider_label"]:16}{c["plan"]:24}{price:>16}   '
              f'{cov["yes"]:>2} yes  {cov["unsure"]:>2} unsure  {cov["no"]:>2} no  '
              f'{cov["no_channel"]} no network yet')
