#!/usr/bin/env python3
"""New PAC City — article-page extraction.

The feed step (fetch.py/build.py) only ever sees what a publisher puts in its
RSS: a title, a summary, sometimes an `author` field. That is not enough for
an editorial reviewer agent to judge a story, and it is measurably not enough
for bylines — the Spokesman-Review carries no `author` in its feed at all, for
any of the 52 items in our corpus from that source, and yet the page itself
names the writer three different ways (JSON-LD, an inline dataLayer blob, and
a "By <name>" link). This module fetches ONE article page and pulls out the
three things the reviewer needs that the feed cannot supply: the body text,
the byline, and whether the page is behind a paywall.

Standard library only, matching build.py/watch_data.py. No dependency on
either of those modules — this is meant to be run against a single URL
independent of a fetch.py run, and it must never write article body text to
disk. The text is returned to the caller and lives in memory for as long as
that caller keeps it; nothing here persists it.

Two things this module got wrong before it existed, worth stating because
they are exactly the mistakes future edits to this file would reintroduce:

  * Reading only <p> tags under the longest <article> element finds body
    text but silently throws away structured author data that sits outside
    it (JSON-LD in <head>, a `rel="author"` link in a byline strip). Byline
    extraction here deliberately looks at the WHOLE page, in a fixed order
    (JSON-LD, then <meta author> tags, then rel="author"), and stops at the
    first level that actually yields a person.

  * An athletics-department press release has no human author. 196 of the
    492 items in our corpus are exactly this — institutional sources whose
    RSS feed carries no author because none exists. A byline reader that
    guesses one from context (a coach's name in the prose, the org's own
    name) is worse than a reader that returns nothing, because a fabricated
    byline looks exactly as confident as a real one. So this module refuses
    to invent: it only reports a name that a machine-readable field
    explicitly attributes as a person, and it explicitly rejects a
    candidate that equals the outlet's own name or is typed as an
    Organization — an org is a publisher, never a byline.

The paywall verdict follows the same "never guess" rule from the other
direction. It is one of "paywall", "free", or None, and None (unknown) is
the correct answer far more often than either of the other two — this
module fires "paywall" or "free" only on a concrete signal (a publisher's
own schema.org `isAccessibleForFree` / `paywall_status`-style field, or an
HTTP 402, or unambiguous "subscribe to keep reading" wall text sitting where
the body should be). A bare HTTP 403 is deliberately NOT treated as a
paywall signal here: two sources in our feed corpus (oregonlive.com,
YourCentralValley) return 403 to every automated fetch, including ours, and
that is bot-blocking, not evidence of a subscription wall — collapsing the
two would fabricate a paywall verdict for a source we simply couldn't
reach. A 403 is recorded as a fetch failure and nothing else.
"""

import gzip
import json
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

USER_AGENT = "NewPACCityBot/0.1 (news aggregator; links and attribution only)"
FETCH_TIMEOUT_S = 20
MIN_REQUEST_GAP_S = 1.5

# Elements whose text is never body copy or byline data.
STRIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside", "form"})
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Phrases that, sitting where an article body should be, mean we hit a wall
# rather than the story. Kept short and literal on purpose — a long fuzzy
# list starts guessing, and guessing is exactly what the paywall verdict must
# never do.
PAYWALL_TEXT_SIGNALS = (
    "subscribe to continue reading",
    "subscribe to keep reading",
    "to continue reading this article",
    "you have reached your limit of free articles",
    "this article is for subscribers only",
    "this content is for subscribers",
    "already a subscriber? sign in",
    "sign in or subscribe to continue",
    "become a subscriber to continue",
)
PAYWALL_STATUS_RE = re.compile(
    r'["\']?paywall_status["\']?\s*[:=]\s*["\']([a-zA-Z_-]+)["\']', re.I)
PAYWALL_STATUS_VALUES = {"premium", "metered", "paywall", "subscriber", "locked"}

# Below this, an <article> element is assumed not to be the story — see the
# fallback in extract_body().
MIN_ARTICLE_WORDS = 60

_last_request_ts = [0.0]
_robots_cache = {}


# --- Fetching ----------------------------------------------------------------

def _throttle():
    """Enforce the >=1.5s gap between requests this module makes, across
    both robots.txt lookups and article fetches — both hit the network."""
    now = time.monotonic()
    wait = MIN_REQUEST_GAP_S - (now - _last_request_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_request_ts[0] = time.monotonic()


def _get(url):
    """One throttled GET with our UA. Raises urllib.error.* or OSError on
    failure — callers decide how to record that, this never raises past a
    caller that doesn't want it to (see fetch_article)."""
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        raw = resp.read()
        status = resp.status
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return status, raw.decode("utf-8", "replace")


def robots_allowed(url):
    """Check robots.txt for this host, once per host per process. A host we
    cannot reach at all for robots.txt is treated as allowing nothing —
    silence about robots.txt is not permission."""
    parsed = urlparse(url)
    host = (parsed.scheme, parsed.netloc)
    if host not in _robots_cache:
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            status, text = _get(robots_url)
            if status == 404:
                rp.allow_all = True
            else:
                rp.parse(text.splitlines())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                rp.allow_all = True
            else:
                rp.disallow_all = True
        except Exception:
            rp.disallow_all = True
        _robots_cache[host] = rp
    return _robots_cache[host].can_fetch(USER_AGENT, url)


def fetch_article(url):
    """Fetch one article page. Never raises — a dead source must never kill
    a run. Returns a dict:
        url, ok (bool), status (int|None), error (str|None),
        robots_blocked (bool), html (str|None)

    `html` is handed to the caller in memory only; nothing in this module
    writes it to disk, and callers of this module must not either."""
    if not robots_allowed(url):
        return {"url": url, "ok": False, "status": None, "error": "robots.txt disallows",
                "robots_blocked": True, "html": None}
    try:
        status, text = _get(url)
        return {"url": url, "ok": True, "status": status, "error": None,
                "robots_blocked": False, "html": text}
    except urllib.error.HTTPError as e:
        return {"url": url, "ok": False, "status": e.code, "error": f"HTTP {e.code}",
                "robots_blocked": False, "html": None}
    except Exception as e:
        return {"url": url, "ok": False, "status": None, "error": f"{type(e).__name__}: {e}",
                "robots_blocked": False, "html": None}


# --- A minimal DOM ------------------------------------------------------------
# stdlib-only means no BeautifulSoup/lxml tree, so we build the small amount
# of tree structure we actually need (parent/child, attrs, text) with
# html.parser.HTMLParser and a stack. Tolerant of the mismatched/unclosed
# tags real news pages ship — an unmatched close tag is ignored rather than
# raising, and anything still open at EOF is simply left open.

class _Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []   # list of _Node or str
        self.text = ""       # only used for the synthetic root


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("[root]", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # unmatched close tag — ignore, tolerate malformed markup

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def _parse(html_text):
    tb = _TreeBuilder()
    tb.feed(html_text)
    return tb.root


def _walk(node):
    """Yield every _Node in the tree, depth-first, root excluded."""
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _text_of(node, skip_tags=STRIP_TAGS):
    """Concatenate the text under a node, skipping subtrees rooted at a tag
    in skip_tags."""
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in skip_tags:
            parts.append(_text_of(child, skip_tags))
    return "".join(parts)


def _collapse(s):
    return re.sub(r"\s+", " ", s).strip()


# --- Body text -----------------------------------------------------------------

def extract_body(html_text):
    """Baseline body extraction: strip script/style/nav/header/footer/aside/
    form, prefer the longest <article> element on the page, take its <p>
    text runs, collapse whitespace. Returns (text, word_count).

    "Longest" is measured by extracted paragraph length, not raw HTML size —
    a page can carry several <article> elements (related-story cards, the
    real story), and only counting the <p> text they actually contain keeps
    a card full of markup from outscoring the real body."""
    root = _parse(html_text)
    articles = [n for n in _walk(root) if n.tag == "article"]
    candidates = articles or [root]

    best_text, best_len = "", -1
    for node in candidates:
        # Remove stripped subtrees implicitly: _text_of already skips them,
        # but we want *paragraph* text, so gather each <p> not nested in a
        # stripped tag by walking and tracking ancestry.
        chunks = []

        def visit(n, blocked):
            for child in n.children:
                if isinstance(child, str):
                    continue
                if child.tag in STRIP_TAGS:
                    continue
                if child.tag == "p" and not blocked:
                    t = _collapse(_text_of(child))
                    if t:
                        chunks.append(t)
                visit(child, blocked)

        visit(node, False)
        text = "\n\n".join(chunks)
        if len(text) > best_len:
            best_text, best_len = text, len(text)

    # An <article> element that holds almost no paragraph text is not the
    # story — some sites wrap each teaser card in one and put the body outside
    # them all. Measured 2026-09-06: The Arbiter returned 15 words this way
    # while the page carried a ~790-word article. Falling back to the whole
    # document (nav, header, footer and aside already stripped) recovers it.
    # The threshold is deliberately low: it rescues a page that returned
    # nothing useful, and never overrides an <article> that actually worked.
    if len(best_text.split()) < MIN_ARTICLE_WORDS and articles:
        chunks = []

        def visit_root(n):
            for child in n.children:
                if isinstance(child, str) or child.tag in STRIP_TAGS:
                    continue
                if child.tag == "p":
                    t = _collapse(_text_of(child))
                    if t:
                        chunks.append(t)
                visit_root(child)

        visit_root(root)
        fallback = "\n\n".join(chunks)
        if len(fallback.split()) > len(best_text.split()):
            best_text = fallback

    return best_text, len(best_text.split())


# --- Byline --------------------------------------------------------------------

_JOB_TITLE_SPLIT = re.compile(r"\s*,\s*")
NON_PERSON_TYPES = re.compile(r"organi[sz]ation|newsmediaorganization|corporation|brand", re.I)

# A newsroom credit is not a byline. Found by measurement on 2026-09-06 and not
# anticipated: four Spokesman-Review items carry "From staff reports" and two
# Seattle Times items carry "Seattle Times staff" in the same JSON-LD author
# field that holds the real names. Those pass every other rule here — not the
# site's own name, not typed as an Organization — so they would have been
# printed in the place a writer's name goes.
#
# The CEO's ruling, 2026-09-06: a newsroom credit is simply blank, and is NOT
# recorded as a distinct kind of byline. An item credited to a newsroom is
# therefore indistinguishable from one with no author at all, which is
# deliberate — what the site promises is crediting a human, and neither has one.
#
# Kept short and literal on purpose, for the same reason the paywall phrases
# are: a long fuzzy list starts guessing. Word boundaries matter, so that
# \bstaff\b cannot match a person surnamed Stafford.
NEWSROOM_CREDIT = re.compile(
    r"\b(staff|newsroom|editorial board|wire reports?|correspondents?)\b", re.I)

# Some sites wrap the name in link boilerplate ("View all posts by HunterFKWG"),
# which the rel="author" path reads verbatim. Strip the wrapper, keep the name.
BYLINE_BOILERPLATE = re.compile(r"^\s*(?:view\s+)?all\s+posts\s+by\s+", re.I)


def _clean_name(raw):
    """Strip a trailing job-title clause ('Jane Smith, Sports Editor' ->
    'Jane Smith'). Only the first comma-separated segment is kept — a name
    itself is not expected to contain a comma."""
    if not raw:
        return ""
    parts = _JOB_TITLE_SPLIT.split(raw.strip())
    return parts[0].strip()


_CONJOINED = re.compile(r"\s+(?:and|&)\s+", re.I)


def _split_conjoined(raw):
    """Two writers in one string -> two names. Deliberately timid.

    Measured 2026-09-06: a University Star piece credits "Juan Pereira Casanoba
    and Luke Landa" in one feed field, and the page's JSON-LD names only the
    first — so without this, a real co-author is silently dropped.

    It splits ONLY into exactly two parts that each read like a full name (two
    or more words). That guard is the whole safety of it: "Track and Field"
    would otherwise become a writer called "Track"."""
    parts = _CONJOINED.split(raw.strip())
    if len(parts) == 2 and all(len(_clean_name(p).split()) >= 2 for p in parts):
        return [p.strip() for p in parts]
    return [raw]


def _site_names(html_text, ld_objects):
    """Names this page identifies itself by, so a byline candidate that IS
    the outlet (an organisation, not a person) can be rejected. Pulled from
    JSON-LD publisher/organization names and og:site_name — never guessed."""
    names = set()
    for obj in ld_objects:
        for pub_key in ("publisher", "sourceOrganization", "provider"):
            pub = obj.get(pub_key) if isinstance(obj, dict) else None
            if isinstance(pub, dict) and pub.get("name"):
                names.add(pub["name"].strip().lower())
        if isinstance(obj, dict) and obj.get("@type") and \
                NON_PERSON_TYPES.search(str(obj.get("@type"))) and obj.get("name"):
            names.add(obj["name"].strip().lower())
    for m in re.finditer(
            r'<meta[^>]+(?:property|name)=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
            html_text, re.I):
        names.add(m.group(1).strip().lower())
    for m in re.finditer(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:site_name["\']',
            html_text, re.I):
        names.add(m.group(1).strip().lower())
    return names


def _find_ld_json(root):
    """Every <script type="application/ld+json"> block on the page, parsed.
    A block may be a single object or a list of objects; a page may carry
    more than one block. Unparseable blocks are skipped, not raised."""
    objects = []
    for n in _walk(root):
        if n.tag == "script" and (n.attrs.get("type") or "").lower() == "application/ld+json":
            raw = "".join(c for c in n.children if isinstance(c, str))
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            objects.extend(parsed if isinstance(parsed, list) else [parsed])
    return objects


def _is_person_entry(entry):
    """True unless an entry explicitly types itself as something other than
    a person. A bare string or an object with no @type is given the benefit
    of the doubt at this stage — the org-name check downstream is what
    catches an organisation with no @type of its own."""
    if isinstance(entry, str):
        return True
    if isinstance(entry, dict):
        t = str(entry.get("@type") or "")
        return not NON_PERSON_TYPES.search(t)
    return False


def _names_from_ld_author(value):
    out = []
    items = value if isinstance(value, list) else [value]
    persons = [it for it in items if _is_person_entry(it)]
    pool = persons or items  # if everything got typed non-Person, still look — the org-equality check catches it
    for it in pool:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict) and it.get("name"):
            out.append(str(it["name"]))
    return out


def _valid_names(raw_names, site_names):
    """Candidates that survive every person test, in order: strip link
    boilerplate, strip a job-title clause, then reject the outlet's own name
    and any newsroom credit. A rejected candidate yields nothing — never a
    placeholder, and never the string that was rejected."""
    out, seen = [], set()
    expanded = []
    for raw in raw_names:
        expanded.extend(_split_conjoined(BYLINE_BOILERPLATE.sub("", raw or "")))
    for raw in expanded:
        name = _clean_name(raw)
        if not name:
            continue
        if name.lower() in site_names:
            continue
        if NEWSROOM_CREDIT.search(name):
            continue
        # A page commonly names its author more than once — JSON-LD and a
        # rel="author" link, or two blocks carrying the same entry. Order is
        # kept; the duplicate is dropped.
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def merge_bylines(page_names, feed_author):
    """The byline for an item, given what its page said and what its feed said.

    Both are the publisher's own attribution, so neither outranks the other —
    but they can disagree in count, and that disagreement is not symmetrical.
    Measured 2026-09-06: a University Star piece whose feed `author` reads
    "Juan Pereira Casanoba and Luke Landa" exposes only the first of the two in
    its JSON-LD, so preferring the page silently dropped a real co-author.

    So: take whichever source names MORE people, and let the page win a tie
    because its structured data needs no normalising. Deliberately NOT a union
    — merging two spellings of one person ("J. Smith" and "Jane Smith") would
    invent a second author, which is the failure this whole field exists to
    avoid."""
    page = list(page_names or [])
    feed = _valid_names([feed_author], set()) if (feed_author or "").strip() else []
    return feed if len(feed) > len(page) else page


def extract_byline(html_text, root=None):
    """First hit wins: JSON-LD author -> <meta author>/<meta article:author>
    -> rel="author". A "hit" means a level actually produces a validated
    person name; a level that exists but resolves to nothing but the
    outlet's own name (an org, not a byline) falls through to the next
    level rather than returning empty. Returns a list of names (possibly
    empty), never a string."""
    root = root if root is not None else _parse(html_text)
    ld_objects = _find_ld_json(root)
    site_names = _site_names(html_text, ld_objects)

    # a. JSON-LD
    ld_names = []
    for obj in ld_objects:
        if isinstance(obj, dict) and "author" in obj:
            ld_names.extend(_names_from_ld_author(obj["author"]))
    hit = _valid_names(ld_names, site_names)
    if hit:
        return hit

    # b. <meta name="author"> / <meta property="article:author">
    meta_names = []
    for m in re.finditer(
            r'<meta[^>]+(?:name|property)=["\'](?:author|article:author)["\'][^>]+content=["\']([^"\']+)["\']',
            html_text, re.I):
        meta_names.append(m.group(1))
    for m in re.finditer(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:author|article:author)["\']',
            html_text, re.I):
        meta_names.append(m.group(1))
    hit = _valid_names(meta_names, site_names)
    if hit:
        return hit

    # c. rel="author"
    rel_names = []
    for n in _walk(root):
        rel = n.attrs.get("rel") or ""
        if "author" in rel.split():
            t = _collapse(_text_of(n))
            if t:
                rel_names.append(t)
    hit = _valid_names(rel_names, site_names)
    return hit


# --- Paywall -------------------------------------------------------------------

def _find_key_recursive(obj, key):
    """Every value found under `key` anywhere in a JSON-LD structure,
    including nested under things like hasPart."""
    found = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for v in obj.values():
            found.extend(_find_key_recursive(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_find_key_recursive(v, key))
    return found


def extract_paywall(fetch_result, html_text=None, root=None, body_text=None):
    """One of "paywall", "free", None, plus which arm fired (or None).
    Returns (verdict, arm) where arm is a short label for the measurement —
    e.g. "A:isAccessibleForFree", "A:paywall_status", "B:http-402",
    "B:wall-text" — or None when no arm fired.

    Arm A (publisher declares it) is checked first and wins outright:
    isAccessibleForFree=False anywhere in the JSON-LD (including nested
    under hasPart) beats a True found elsewhere on the same page, because a
    free teaser fragment inside a paywalled article is still a paywalled
    article. Arm B (we hit one) only fires on a concrete, literal signal —
    HTTP 402, or one of a short list of explicit wall phrases sitting in the
    page — never on a bare fetch failure. A 403 is exactly such a bare
    failure here (see module docstring) and never produces "paywall"."""
    if not fetch_result["ok"]:
        if fetch_result.get("status") == 402:
            return "paywall", "B:http-402"
        return None, None

    html_text = html_text if html_text is not None else fetch_result.get("html") or ""
    root = root if root is not None else _parse(html_text)
    ld_objects = _find_ld_json(root)

    free_flags = _find_key_recursive(ld_objects, "isAccessibleForFree")
    norm = []
    for f in free_flags:
        if isinstance(f, bool):
            norm.append(f)
        elif isinstance(f, str):
            if f.strip().lower() == "true":
                norm.append(True)
            elif f.strip().lower() == "false":
                norm.append(False)
    if False in norm:
        return "paywall", "A:isAccessibleForFree"

    m = PAYWALL_STATUS_RE.search(html_text)
    if m and m.group(1).strip().lower() in PAYWALL_STATUS_VALUES:
        return "paywall", "A:paywall_status"

    if True in norm:
        return "free", "A:isAccessibleForFree"

    haystack = (body_text or "")[:4000].lower() or html_text[:8000].lower()
    for phrase in PAYWALL_TEXT_SIGNALS:
        if phrase in haystack:
            return "paywall", "B:wall-text"

    return None, None


# --- One-call convenience ------------------------------------------------------

def extract(url):
    """Fetch one URL and return every extraction in one dict. `body` is
    returned to the caller in memory only — this function does not write it
    anywhere, and neither should whoever calls it.

        {url, ok, status, error, robots_blocked,
         body, word_count, byline, paywall, paywall_arm}
    """
    result = fetch_article(url)
    out = {
        "url": url, "ok": result["ok"], "status": result["status"],
        "error": result["error"], "robots_blocked": result["robots_blocked"],
        "body": None, "word_count": None, "byline": [], "paywall": None,
        "paywall_arm": None,
    }
    if not result["ok"]:
        out["paywall"], out["paywall_arm"] = extract_paywall(result)
        return out

    html_text = result["html"]
    root = _parse(html_text)
    body, word_count = extract_body(html_text)
    out["body"] = body
    out["word_count"] = word_count
    out["byline"] = extract_byline(html_text, root=root)
    out["paywall"], out["paywall_arm"] = extract_paywall(
        result, html_text=html_text, root=root, body_text=body)
    return out


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else (
        "http://www.spokesman.com/stories/2026/sep/02/"
        "gonzaga-scheduled-to-host-incarnate-word-for-fifth/")
    r = extract(target)
    print(f"url:      {r['url']}")
    print(f"ok:       {r['ok']}  status={r['status']}  error={r['error']}  "
          f"robots_blocked={r['robots_blocked']}")
    print(f"byline:   {r['byline']}")
    print(f"paywall:  {r['paywall']}  (arm: {r['paywall_arm']})")
    print(f"words:    {r['word_count']}")
    if r["body"]:
        print(f"body[:300]: {r['body'][:300]!r}")
