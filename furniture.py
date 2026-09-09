#!/usr/bin/env python3
"""The site's furniture: policy pages, robots.txt, the sitemap, the 404.

Everything here is prose and plumbing that has nothing to do with gathering
news, which is why it lives beside build.py rather than inside it. build.py is
the story machine; this is the paperwork a public site is expected to carry.

The page texts were drafted, reviewed line by line and approved by the CEO on
2026-09-09; the drafts and the reasoning behind each decision are in the
Studio at projects/new-pac-city/work-site-furniture-drafts.md. Two of them say
things that are true only while the site behaves a certain way -- the privacy
page describes exactly what the counter collects, and robots.txt states the
rule we ask others to keep. **If the site's behaviour changes, these change in
the same commit.** A policy page that has drifted from the software is worse
than no policy page, because it is a promise the site is quietly breaking.
"""

# The counter. Free non-commercial tier, no cookies, no personal data.
# Named here once so the privacy page and the script tag can never disagree
# about which service is running.
GC_CODE = "newpaccity"
ANALYTICS = ('<script data-goatcounter="https://%s.goatcounter.com/count"'
             ' async src="//gc.zgo.at/count.js"></script>' % GC_CODE)

# The date the policy TEXT last changed -- never the build date. The privacy
# page promises "the date below will change" if what we collect changes, and a
# stamp that moved every twelve hours would make that promise meaningless.
# Change this by hand, in the same commit that changes a page.
LAST_UPDATED = "9 September 2026"

CONTACT_GENERAL = "hello@newpac.city"
CONTACT_REMOVALS = "removals@newpac.city"


# --- the policy pages -------------------------------------------------------
# Each is a body fragment; the shell, the masthead and the scope bar come from
# build.render_static_page. Written in the About page's voice -- short
# declarative sentences, "we", plain words -- and deliberately not in legal
# boilerplate, which would describe a commercial site this one is not.

PRIVACY = """
<p><b>There are no accounts here, no advertising, and no cookies. We do not know who you
are and we have no way to find out.</b></p>

<h2>What we count</h2>

<p>We use <a href="https://www.goatcounter.com" target="_blank" rel="noopener">GoatCounter</a>,
a small privacy-focused counter, to learn whether anyone is visiting and which headlines get
clicked. For each page view it records the page, the time, the site that referred you if there
was one, and general facts about your browser, screen size, language and country.</p>

<p><b>It sets no cookie.</b> It stores nothing in your browser &mdash; no cookie, no local
storage, no cache entry, nothing. <b>It does not store your IP address</b> or your full browser
string.</p>

<p>To avoid counting one person reloading a page as ten visitors, it scrambles your IP address
and browser together into a meaningless string, holds that in memory for at most eight hours,
and never writes it down. There is no identifier that survives that, so nothing here can
recognise you tomorrow, and nothing can follow you to another website.</p>

<p><b>We look at this for one reason:</b> to find out which schools and which publishers people
actually read, so we can gather better. It is not sold, not shared, and not used for
advertising, because there is no advertising.</p>

<h2>What our host sees</h2>

<p>The site is hosted on GitHub Pages. When your browser asks GitHub for a page, GitHub receives
the ordinary information any web server receives &mdash; your IP address, the page you asked
for, your browser's user-agent string &mdash; and keeps it for a period, to protect the service
against abuse. We never see it. We cannot query it, export it, or connect it to you. GitHub's
handling of that data is covered by the <a
href="https://docs.github.com/site-policy/privacy-policies/github-privacy-statement"
target="_blank" rel="noopener">GitHub Privacy Statement</a>.</p>

<h2>When you click a headline</h2>

<p>Every headline on this site is a link to somebody else's website. Once you click, you are
their visitor, under their privacy policy, and they may well collect a great deal more than we
do. We have no control over that and no visibility into it.</p>

<h2>Email</h2>

<p>If you write to us, we have your email address and whatever you put in the message, for as
long as we keep the message. We use it to answer you. We don't add you to anything, because
there is nothing to be added to.</p>

<h2>If this ever changes</h2>

<p>If New PAC City ever accepts advertising, adds anything that can identify you, or changes
what it counts, <b>this page will say so, and the date below will change.</b> We would rather
lose the measurement than break this page's promise quietly.</p>
"""

TERMS = """
<p>New PAC City is a free, independent fan site. Using it means accepting the following, which
we have tried to write in English.</p>

<h2>What this site is</h2>

<p>We gather headlines from public feeds published by news outlets, universities, blogs and
podcasts, and we link to them. <b>We do not host, republish or reproduce anyone's articles.</b>
What you see here is a headline, an attribution, sometimes a brief excerpt, and a link to the
original. Everything behind that link belongs to whoever made it, and is governed by their
terms, not ours.</p>

<h2>What we don't promise</h2>

<p>The site is provided as it is. We don't promise it will be accurate, complete, current, or
available. Feeds break, publishers change their URLs, and headlines are sometimes written to
mislead &mdash; we pass them along as published and we do not verify them. <b>Nothing here is a
substitute for reading the original.</b></p>

<p>We are not responsible for anything on the sites we link to, and a link is not an
endorsement.</p>

<h2>What's ours, and what you may do with it</h2>

<p>The site's design, its editorial choices about what to gather and how to group it, and the
code that builds it are ours. The headlines and excerpts are not &mdash; they belong to their
publishers and appear here under attribution.</p>

<p>You are welcome to read the site, link to it, and share links to it. If you want the
underlying data for something interesting, write to us and ask; the answer will usually be
yes.</p>

<h2>Crawling this site &mdash; the line we draw, and the line we keep</h2>

<p><b>We hold ourselves to a rule, and we ask the same of anyone who visits us
automatically.</b></p>

<p>This site exists by reading feeds that other people publish for exactly that purpose. When we
do it, our fetcher says truthfully what it is, reads only feeds a publisher offers for
syndication, consults <code>robots.txt</code> before fetching any page beyond them, takes
headlines rather than articles, and sends every reader back to the publisher with their name on
it. <b>We never disguise ourselves to get past a block, even when honesty costs us a source</b>
&mdash; and it has. Where a publisher's rules permit indexing but forbid scraping, we comply
with both the letter and the spirit of that.</p>

<p>So we draw the same line here, and it is not a complaint about machines:</p>

<ul>
<li><b>Crawl us to send people somewhere &mdash; welcome.</b> Search engines and the retrieval
bots that answer someone's question with a citation and a link both do what this whole site
does. They are the reason anyone finds us.</li>
<li><b>Crawl us to train a model &mdash; no.</b> Our <a href="/robots.txt">robots.txt</a> says
so by name. A training crawler takes the work and returns nothing to the publishers whose
reporting this site points at, and we are not willing to be the doorway through which that
happens to them.</li>
</ul>

<p><code>robots.txt</code> is a request, not a lock, and we know it. We are stating the rule we
expect to be honoured, in the same terms we honour other people's.</p>

<h2>Not affiliated with anyone</h2>

<p>New PAC City is not the Pac-12 Conference, is not any university, and is not affiliated with,
endorsed by, or sponsored by either. Team names, logos and marks belong to their owners and are
used here to say which school a story is about &mdash; nothing more. We take no money from
anyone we link to.</p>

<h2>If you're a publisher</h2>

<p>If you'd rather we didn't link to you, or you want a specific item removed, see <a
href="/contact/">Contact</a>. We will act on it, and we will not argue with you about it.</p>

<h2>Changes, and the boring line at the end</h2>

<p>We may change these terms; the date below says when we last did. These terms are governed by
the laws of the State of Oregon, without regard to its conflict-of-law rules.</p>
"""

CONTACT = """
<p><b>Anything at all &mdash; <a href="mailto:%(general)s">%(general)s</a></b><br>
<b>Removal requests &mdash; <a href="mailto:%(removals)s">%(removals)s</a></b></p>

<p>One person reads both. It is a fan site, not a newsroom, so answers may take a few days
&mdash; except removals, which we do first.</p>

<h2>If you're a publisher and want something removed</h2>

<p>Write to <a href="mailto:%(removals)s">%(removals)s</a> with the link, and say whether you
want the single item removed or your whole publication dropped from the site. <b>We will do it,
and we won't argue.</b> You do not need to send a formal legal notice, cite a statute, or
explain yourself. We host none of your content &mdash; every headline here is a link back to you
&mdash; but it's your work and it's your call.</p>

<p>If you'd rather block us at the source instead, our fetcher identifies itself honestly in
its user-agent string, and consults <code>robots.txt</code> before fetching any page beyond the
feed you publish. We never disguise what we are. Pulling your feed out of our list is also
something we will simply do &mdash; see above.</p>

<h2>If something is broken</h2>

<p>Wrong school on a story, a dead link, a headline that's been mangled, a feed showing stale
items &mdash; please tell us. This is a small site and these get fixed quickly.</p>

<h2>If we're missing a source</h2>

<p>We are always looking for good independent coverage of the nine schools, particularly for the
ones where it is thin. Send us the feed.</p>
""" % {"general": CONTACT_GENERAL, "removals": CONTACT_REMOVALS}

ACCESSIBILITY = """
<p>We want this site to be usable by everyone, including people using screen readers, keyboard
navigation, magnification, or a phone in bright sunlight.</p>

<p><b>What we've done.</b> The site is plain HTML. Headings are real headings and links are real
links. Every page declares its language, and starts with a &ldquo;skip to the main
content&rdquo; link so you do not have to walk through the navigation every time. Navigation works from the
keyboard, and the focused element is always visibly outlined. Tap targets are sized for fingers.
The site follows your device's light or dark setting rather than forcing one on you, and the
text resizes with your browser's settings without the layout breaking. The only script on the
site is a page counter; nothing you see or click depends on JavaScript.</p>

<p><b>What we know isn't certain yet.</b> We have not had the site independently audited against
WCAG, so this statement describes what we built for, not a certified result.</p>

<p><b>If something doesn't work for you, please tell us</b> &mdash; <a
href="mailto:%(general)s">%(general)s</a>. A specific report (&ldquo;the team nav is unreachable
in NVDA&rdquo;) is worth more to us than a general one, but send either.</p>
""" % {"general": CONTACT_GENERAL}

NOT_FOUND = """
<p>This is a small site that rebuilds itself twice a day, so pages move around more than they
would on a normal one &mdash; a story that was on page 3 last week may be on page 6 now.</p>

<p><b><a href="/">Go to the front page &rarr;</a></b></p>

<p>Or use the navigation above to jump to a school.</p>
"""

# slug, nav/heading label, <title> text, body. Order is the footer order.
PAGES = [
    ("about", "About", "About", None),          # rendered by build.render_about
    ("privacy", "Privacy", "Privacy", PRIVACY),
    ("terms", "Terms", "Terms of use", TERMS),
    ("contact", "Contact", "Contact", CONTACT),
    ("accessibility", "Accessibility", "Accessibility", ACCESSIBILITY),
]

# The pages the footer links to, and the ones the sitemap carries.
POLICY_PAGES = [p for p in PAGES if p[3] is not None]


# --- robots.txt -------------------------------------------------------------
# Three categories, not two. Search indexers crawl to refer; retrieval bots
# crawl to cite, with a link back; training crawlers ingest and return nothing.
# The providers now run these as separately-named bots, which is the only
# reason this policy is expressible at all.
#
# Blocking Google-Extended does NOT affect Google Search: it is a training
# opt-out token, and Googlebot rides the wildcard group and indexes normally.
# Same for Applebot-Extended versus Applebot.
#
# ClaudeBot is on the disallow list, and the site was built with Claude. The
# rule is applied by category, never by relationship -- the exception nobody
# would have noticed is the one that would make the whole statement worthless.

TRAINING_CRAWLERS = [
    "GPTBot", "ClaudeBot", "anthropic-ai", "Google-Extended", "Applebot-Extended",
    "meta-externalagent", "CCBot", "Bytespider", "Amazonbot", "cohere-ai",
    "Diffbot", "ImagesiftBot", "Omgilibot", "AI2Bot", "Timpibot", "PanguBot",
    "YouBot", "Scrapy",
]

RETRIEVAL_BOTS = [
    "OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User",
    "PerplexityBot", "Perplexity-User",
]


def robots_txt(cfg):
    base = cfg.get("site_url", "").rstrip("/")
    return """# New PAC City — an independent fan site about the new Pac-12.
# Every headline here is a link out to the publisher who wrote it.
# Removal requests and questions: %(removals)s
#
# The rule we keep, and the rule we ask for:
# We read feeds that publishers offer for syndication. Our fetcher states truthfully
# what it is, consults robots.txt before fetching any page beyond the feed, takes
# headlines rather than articles, and sends every reader back to the source with
# attribution. We never disguise ourselves to get past
# a block, even when that costs us a source.
# So: crawl us to send people somewhere. Do not crawl us to train a model.

Sitemap: %(base)s/sitemap.xml

# ---------------------------------------------------------------------------
# Everyone not named below: welcome.
# ---------------------------------------------------------------------------
User-agent: *
Allow: /

# ---------------------------------------------------------------------------
# Retrieval and answering bots: welcome, explicitly. You fetch a page to answer
# someone's question and you cite the source with a link. That is what this
# whole site does.
# ---------------------------------------------------------------------------
%(retrieval)s
Allow: /

# ---------------------------------------------------------------------------
# Model training crawlers: no. You take the work and return nothing to the
# publishers whose reporting this site points at. We are not willing to be the
# doorway through which that happens to them.
# ---------------------------------------------------------------------------
%(training)s
Disallow: /
""" % {
        "removals": CONTACT_REMOVALS,
        "base": base,
        "retrieval": "\n".join("User-agent: %s" % b for b in RETRIEVAL_BOTS),
        "training": "\n".join("User-agent: %s" % b for b in TRAINING_CRAWLERS),
    }


# --- sitemap ----------------------------------------------------------------
# changefreq and priority are deliberately omitted: Google has said publicly it
# ignores both, so including them is cargo cult.

def sitemap_xml(cfg, paths, lastmod):
    base = cfg.get("site_url", "").rstrip("/")
    day = lastmod.strftime("%Y-%m-%d") if lastmod else None
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in paths:
        out.append("  <url>")
        out.append("    <loc>%s%s</loc>" % (base, path))
        if day:
            out.append("    <lastmod>%s</lastmod>" % day)
        out.append("  </url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"
