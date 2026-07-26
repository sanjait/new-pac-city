# New PAC City

**The new PAC-12, all in one place.** One fast page of the latest news for every team in the new Pac-12 Conference — football, men's basketball, and women's basketball — refreshed automatically every 6 hours.

🌐 Live site: coming to [newpac.city](https://newpac.city)

## How it works

`build.py` (Python, standard library only — no dependencies) fetches ~30 verified RSS feeds listed in `feeds.json`, groups the headlines by team, and writes a single static page to `docs/index.html`. A scheduled GitHub Action reruns it every 6 hours and publishes the result via GitHub Pages. No server, no database, no tracking, no accounts.

Rebuild locally:

```bash
python3 build.py
```

## Aggregation policy

This site shows **headlines, brief snippets, source names, and links to the original publishers — nothing more**. Full stories belong to their sources; every click goes to them. Feeds are fetched once per 6-hour cycle, respecting robots.txt and crawl-delay directives.

New PAC City is an independent fan project, not affiliated with or endorsed by the Pac-12 Conference or any university. Source or publisher who'd like a feed removed or adjusted: open an issue.

## License

Code is MIT-licensed (see LICENSE). Headlines and snippets remain the property of their original publishers.
