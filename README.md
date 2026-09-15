# Endorfin podcast feed

This repository publishes the podcast feed consumed by edgarkozak.com:

https://edgarautomates-cmyk.github.io/endorfin-podcast-feed/feed.json

The generator retrieves the `@endorfinworld/videos` tab with flat yt-dlp; that tab is the only episode universe, so RSS-only Shorts cannot enter the feed. RSS and the prior feed only provide trustworthy dates/metadata for tab entries. An undateable new tab entry fails closed. The configured music playlist is excluded when lookup works, with deterministic `live set`/`DJ set` title fallback, and up to 50 podcast videos are emitted newest first. Titles are split into `title` and `guests` only for deterministic high-confidence markers such as `with`, `feat.`, or a clear dash suffix; `listTitle` always equals the resulting episode title.

## Operations

Run locally with Python and yt-dlp:

```text
uv run --with pytest pytest -q
uv run --with yt-dlp python generator.py --output feed.json
```

The weekly GitHub Actions workflow runs Mondays at 04:17 UTC and can also be
started with `workflow_dispatch`. GitHub Pages serves the repository root;
`.nojekyll` keeps the JSON path direct and the public endpoint is CORS-friendly
for normal browser fetches.

## Durability and recovery

Generation retrieves and validates the complete candidate before changing
anything. It writes through a temporary file and atomically replaces
`feed.json` only after validation succeeds. When an existing feed is replaced,
the prior bytes are retained as `feed.previous.json`; Git history retains every
committed version. Any retrieval, parsing, or validation failure leaves the
existing `feed.json` untouched. To recover manually, restore a known-good
version from Git and rerun the tests before pushing.

`feed.json` is intentionally committed so Pages remains available between
workflow runs. Do not edit it manually; fix the generator or source metadata
instead.
