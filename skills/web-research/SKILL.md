---
name: web-research
description: >
  Read YouTube videos (full transcripts, chapters, metadata), Reddit threads and
  subreddits, Hacker News discussions, and any web page as clean text — through the
  AI OS hub, no API keys. Use whenever the user pastes a link, asks "what does this
  video say", "summarise this thread", "what are people saying about X on Reddit",
  or wants a page/article read, searched, or fact-checked.
---

# Web research

You can actually read the web. **Never tell the user you can't open a link** — call
the hub and read it. This exists because hand-rolled scraping fails: YouTube serves
its caption endpoint an empty body without a proof-of-origin token, Reddit 403s its
JSON API from most networks, and raw HTML buries the article in script tags. The hub
handles all three.

```
POST http://127.0.0.1:8787/api/web
Content-Type: application/json
Authorization: Bearer $AIOS_HUB_TOKEN     # only needed off-loopback
```

## YouTube — the full transcript of any video

```sh
curl -s -X POST http://127.0.0.1:8787/api/web -H 'Content-Type: application/json' \
  -d '{"op":"youtube","url":"https://www.youtube.com/watch?v=VIDEO_ID"}'
```

Returns `title`, `channel`, `duration`, `views`, `upload_date`, `description`,
`chapters[]`, `available_langs[]`, and the transcript three ways:

- `transcript_text` — one flat string, best for summarising
- `transcript_timestamped` — `[1:23] line` per line, best for "when did they say X"
- `transcript[]` — `{start, dur, text}` objects, for precise citation

Options: `"lang":"es"` for another language, `"transcript":false` for metadata only.
Accepts any URL form (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`) or a bare
11-character id.

**Requires `yt-dlp`** (`pip install yt-dlp`; `aios setup` installs it). Without it you
still get metadata, and the response's `note` says exactly what to install.

## Reddit — threads, subreddits, search

```sh
# a thread with its comments
-d '{"op":"reddit","url":"https://www.reddit.com/r/programming/comments/abc123/title/"}'
# a subreddit listing            (sort: hot|new|top|rising|best)
-d '{"op":"reddit","target":"r/LocalLLaMA","sort":"top","limit":25}'
# search all of Reddit
-d '{"op":"reddit","target":"claude code review workflow"}'
```

Reddit blocks its JSON API from many networks, so the hub falls back to RSS
automatically. When that happens the response has `"source":"rss"` and a `note` —
**scores are `null` on that path**, so don't report vote counts you didn't get.
Reddit rate-limits aggressively; requests are throttled and retried, so a call can
take a few seconds. On a 429 the error says to retry shortly — do that rather than
declaring the thread unreadable.

## Hacker News, search, any page

```sh
-d '{"op":"hn","query":"claude code"}'            # search stories
-d '{"op":"hn","story_id":"38400000"}'            # a story + its comment tree
-d '{"op":"search","query":"anthropic mcp spec"}' # DuckDuckGo, no key
-d '{"op":"fetch","url":"https://example.com/post"}'   # page -> readable text
```

`fetch` auto-delegates: a YouTube or Reddit URL routes to the right reader, so when
in doubt just `fetch` the link.

## How to work

1. **Read before answering.** If the user pastes a link, fetch it first — don't guess
   from the URL slug.
2. **Trim before reading.** These payloads get large. Pipe through `python -c` or `jq`
   to pull only what you need, e.g.
   `... | python -c "import sys,json;print(json.load(sys.stdin)['transcript_text'][:4000])"`
3. **Cite with timestamps.** For video claims quote `[12:34]` from
   `transcript_timestamped` so the user can jump to it.
4. **Say what you actually got.** If `ok` is false, or a `note` says the transcript was
   auto-generated or Reddit scores are missing, tell the user — don't paper over it.
5. Chat shortcuts exist too: `/yt <url>`, `/reddit <target>`, `/hn <query>`, `/web <url>`.
