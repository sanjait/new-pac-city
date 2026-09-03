# New PAC City

**The new PAC-12, all in one place.** A homepage plus a page per team for every school in the new Pac-12 Conference — football, men's basketball, and women's basketball — refreshed automatically twice a day, at 6 a.m. and 6 p.m. Pacific.

🌐 Live site: coming to [newpac.city](https://newpac.city)

## How it works

`build.py` (Python, standard library only — no dependencies) fetches ~30 verified RSS feeds listed in `feeds.json`, groups the headlines by team, and writes a static site to `docs/`: a homepage (`docs/index.html`) with a lead story and a tile per team, plus one page per team at its own slug — `docs/broncos/`, `docs/rams/`, `docs/fsudogs/`, `docs/zags/`, `docs/beavs/`, `docs/aztecs/`, `docs/bobcats/`, `docs/aggies/`, `docs/cougs/`. A scheduled GitHub Action reruns it every 6 hours and publishes the result via GitHub Pages. No server, no database, no tracking, no accounts.

Rebuild locally:

```bash
python3 build.py
```

### Team config (`feeds.json`)

Each team entry carries its identity and official school colors: `slug` (the URL segment, e.g. `beavs`), `nickname`, `primary`/`secondary` (light-mode colors) and `primary_dark`/`secondary_dark` (dark-mode variants), plus an optional `primary_text` override for when the official primary doesn't pass WCAG AA contrast as text on light backgrounds. `primary`/`secondary` paint decoration (stripes, wash tints, cap bars); the text-safe variant is used wherever a team color renders as foreground text or a link.

## Aggregation policy

This site shows **headlines, brief snippets, source names, and links to the original publishers — nothing more**. Full stories belong to their sources; every click goes to them. Feeds are fetched once per 6-hour cycle, respecting robots.txt and crawl-delay directives.

New PAC City is an independent fan project, not affiliated with or endorsed by the Pac-12 Conference or any university. Source or publisher who'd like a feed removed or adjusted: open an issue.

## License

Code is MIT-licensed (see LICENSE). Headlines and snippets remain the property of their original publishers.
