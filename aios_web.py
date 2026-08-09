#!/usr/bin/env python3
"""
AIOS Web — real access to YouTube, Reddit, Hacker News and the open web.

The agents already have a shell, so they *could* curl these themselves. In
practice that fails constantly: YouTube transcripts live behind an undocumented
InnerTube endpoint, Reddit 403s anything without a real User-Agent, and raw HTML
buries the article in 200KB of script tags. So each source gets a proper reader
here, and the agents call one tool that returns clean text.

    youtube(url)   video metadata + FULL transcript (+ chapters), no API key
    reddit(url)    a post with its comment tree, a subreddit, or a search
    hackernews(q)  story + comment threads
    fetch(url)     any page, reduced to readable text
    search(q)      DuckDuckGo HTML endpoint (no key)

Pure standard library. Every function returns a dict with an "ok" flag and, on
failure, an "error" that says what to do about it — never a silent empty string,
because an agent that gets "" will confidently tell you the video had no content.
"""
from __future__ import annotations

import gzip
import html as _html
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = int(os.environ.get("AIOS_WEB_TIMEOUT", "25"))


def _get(url: str, headers: dict | None = None, data: bytes | None = None,
         timeout: int | None = None) -> str:
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
         "Accept-Encoding": "gzip, deflate"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _err(msg: str, **extra) -> dict:
    return {"ok": False, "error": msg, **extra}


# --------------------------------------------------------------------------- #
# YouTube                                                                      #
# --------------------------------------------------------------------------- #
_NO_TRANSCRIPT_HINT = (
    "No transcript could be retrieved ({why}). YouTube now serves the caption "
    "endpoint an empty response unless the request carries a proof-of-origin token, "
    "so AIOS uses yt-dlp for this. Install it and retry:  pip install yt-dlp   "
    "(or `aios setup` installs it for you).{extra}")

_YT_ID = re.compile(
    r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/|/v/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str:
    url = (url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = _YT_ID.search(url)
    return m.group(1) if m else ""


def _yt_player(vid: str) -> dict | None:
    """InnerTube player endpoint. The ANDROID client returns caption tracks
    without the PO-token dance the web client now requires."""
    body = json.dumps({
        "videoId": vid,
        "context": {"client": {"clientName": "ANDROID", "clientVersion": "19.09.37",
                               "androidSdkVersion": 30, "hl": "en", "gl": "US"}},
    }).encode()
    url = ("https://www.youtube.com/youtubei/v1/player"
           "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8&prettyPrint=false")
    try:
        return json.loads(_get(url, data=body,
                               headers={"X-YouTube-Client-Name": "3",
                                        "X-YouTube-Client-Version": "19.09.37"}))
    except Exception:
        return None


def _yt_scrape(vid: str) -> dict | None:
    """Fallback: pull ytInitialPlayerResponse out of the watch page."""
    try:
        page = _get(f"https://www.youtube.com/watch?v={vid}&hl=en")
    except Exception:
        return None
    m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*(?:var|</script>)",
                  page, re.S)
    if not m:
        m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;", page, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _caption_tracks(pr: dict) -> list:
    try:
        return (pr.get("captions", {})
                  .get("playerCaptionsTracklistRenderer", {})
                  .get("captionTracks", []) or [])
    except Exception:
        return []


def _fetch_transcript(track: dict) -> list:
    """Return [{start, dur, text}]. json3 is cleanest; XML is the fallback."""
    base = track.get("baseUrl", "")
    if not base:
        return []
    try:
        raw = _get(base + ("&" if "?" in base else "?") + "fmt=json3")
        data = json.loads(raw)
        out = []
        for ev in data.get("events", []):
            segs = ev.get("segs") or []
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if text and text != "\n":
                out.append({"start": round(ev.get("tStartMs", 0) / 1000, 2),
                            "dur": round(ev.get("dDurationMs", 0) / 1000, 2),
                            "text": text})
        if out:
            return out
    except Exception:
        pass
    try:  # legacy XML
        xml = _get(base)
        out = []
        for m in re.finditer(r'<text start="([\d.]+)"(?: dur="([\d.]+)")?[^>]*>(.*?)</text>',
                             xml, re.S):
            t = _html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
            if t:
                out.append({"start": float(m.group(1)),
                            "dur": float(m.group(2) or 0), "text": t})
        return out
    except Exception:
        return []


def _fmt_ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60}:{s % 60:02d}"


def _ytdlp():
    """yt-dlp if it's importable. It is the only thing that reliably gets captions:
    YouTube now serves the timedtext endpoint an empty 200 unless the request
    carries a proof-of-origin token, and yt-dlp is what keeps up with that."""
    try:
        import yt_dlp  # noqa: PLC0415
        return yt_dlp
    except Exception:
        return None


def _youtube_via_ytdlp(vid: str, lang: str, want_transcript: bool) -> dict | None:
    mod = _ytdlp()
    if not mod:
        return None
    try:
        with mod.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True,
                            "writesubtitles": True, "writeautomaticsub": True,
                            "subtitleslangs": [lang], "extract_flat": False}) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            out = {
                "ok": True, "video_id": vid, "source": "yt-dlp",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": info.get("title", ""), "channel": info.get("uploader", ""),
                "channel_id": info.get("channel_id", ""),
                "duration_sec": int(info.get("duration") or 0),
                "duration": _fmt_ts(int(info.get("duration") or 0)),
                "views": int(info.get("view_count") or 0),
                "likes": info.get("like_count"),
                "upload_date": info.get("upload_date", ""),
                "description": (info.get("description") or "")[:8000],
                "keywords": (info.get("tags") or [])[:25],
                "live": bool(info.get("is_live")),
                "chapters": [{"t": int(c.get("start_time") or 0),
                              "ts": _fmt_ts(int(c.get("start_time") or 0)),
                              "title": (c.get("title") or "")[:120]}
                             for c in (info.get("chapters") or [])][:60],
            }
            if not want_transcript:
                return out

            manual = info.get("subtitles") or {}
            auto = info.get("automatic_captions") or {}
            out["available_langs"] = sorted(set(manual) | {k for k in auto if len(k) <= 5})

            def pick(store):
                for key in (lang, f"{lang}-orig", "en", "en-US", "en-GB"):
                    if key in store:
                        return store[key], key
                return None, ""
            tracks, key = pick(manual)
            is_auto = False
            if not tracks:
                tracks, key = pick(auto)
                is_auto = True
            if not tracks:
                out.update(transcript=[], transcript_text="",
                           note="This video has no captions in that language.")
                return out

            fmt = next((t for t in tracks if t.get("ext") == "json3"), None) or \
                next((t for t in tracks if t.get("ext") in ("srv1", "vtt")), None)
            if not fmt:
                out.update(transcript=[], transcript_text="",
                           note="Caption track exists but no parsable format was offered.")
                return out

            raw = y.urlopen(fmt["url"]).read().decode("utf-8", "replace")
            segs = []
            if fmt.get("ext") == "json3":
                for ev in json.loads(raw).get("events", []):
                    txt = "".join(s.get("utf8", "") for s in (ev.get("segs") or [])).strip()
                    if txt and txt != "\n":
                        segs.append({"start": round(ev.get("tStartMs", 0) / 1000, 2),
                                     "dur": round(ev.get("dDurationMs", 0) / 1000, 2),
                                     "text": txt})
            else:  # vtt / srv1
                for m in re.finditer(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->.*?\n(.+?)\n\n",
                                     raw + "\n\n", re.S):
                    t = re.sub(r"<[^>]+>", "", m.group(5)).strip().replace("\n", " ")
                    if t:
                        segs.append({"start": int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                     + int(m.group(3)), "dur": 0, "text": t})
            out["transcript"] = segs
            out["transcript_lang"] = key
            out["transcript_auto"] = is_auto
            out["transcript_text"] = " ".join(s["text"] for s in segs)
            out["transcript_timestamped"] = "\n".join(
                f"[{_fmt_ts(s['start'])}] {s['text']}" for s in segs)
            out["words"] = len(out["transcript_text"].split())
            return out
    except Exception as e:
        return {"_ytdlp_error": str(e)[:300]}


def youtube(url: str, want_transcript: bool = True, lang: str = "en") -> dict:
    """Everything that happened in a video: metadata, chapters, full transcript."""
    vid = youtube_id(url)
    if not vid:
        return _err("Not a YouTube URL or video id. Give a full link "
                    "(https://youtube.com/watch?v=…) or the 11-character id.")

    # yt-dlp first — it's the only path that reliably returns caption text.
    got = _youtube_via_ytdlp(vid, lang, want_transcript)
    ytdlp_err = ""
    if got and got.get("ok"):
        return got
    if got:
        ytdlp_err = got.get("_ytdlp_error", "")

    pr = _yt_player(vid) or _yt_scrape(vid)
    if not pr:
        return _err("Could not reach YouTube (network blocked, or the page shape "
                    "changed). Try again, or check connectivity.", video_id=vid)

    status = (pr.get("playabilityStatus") or {})
    vd = pr.get("videoDetails") or {}
    if not vd and status.get("status") not in (None, "OK"):
        return _err(f"YouTube says: {status.get('status')} — "
                    f"{status.get('reason') or 'video unavailable'}", video_id=vid)

    out = {
        "ok": True, "video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
        "title": vd.get("title", ""), "channel": vd.get("author", ""),
        "channel_id": vd.get("channelId", ""),
        "duration_sec": int(vd.get("lengthSeconds") or 0),
        "duration": _fmt_ts(int(vd.get("lengthSeconds") or 0)),
        "views": int(vd.get("viewCount") or 0),
        "description": (vd.get("shortDescription") or "")[:8000],
        "keywords": (vd.get("keywords") or [])[:25],
        "live": bool(vd.get("isLiveContent")),
    }

    # Chapters, when the uploader defined them in the description.
    chapters = []
    for m in re.finditer(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s+[-–—]?\s*(.+)$",
                         out["description"], re.M):
        parts = [int(x) for x in m.group(1).split(":")]
        secs = parts[0] * 60 + parts[1] if len(parts) == 2 else \
            parts[0] * 3600 + parts[1] * 60 + parts[2]
        chapters.append({"t": secs, "ts": m.group(1), "title": m.group(2).strip()[:120]})
    out["chapters"] = chapters[:60]

    if not want_transcript:
        return out

    tracks = _caption_tracks(pr)
    if not tracks:  # the InnerTube clients often omit captions — the watch page has them
        alt = _yt_scrape(vid)
        if alt:
            tracks = _caption_tracks(alt)
    if not tracks:
        out["transcript"] = []
        out["transcript_text"] = ""
        out["note"] = _NO_TRANSCRIPT_HINT.format(
            why="no caption tracks were offered", extra=ytdlp_err)
        return out

    def rank(t):
        code = (t.get("languageCode") or "").lower()
        return (0 if code.startswith(lang.lower()) else 1,
                0 if t.get("kind") != "asr" else 1)  # prefer human over auto
    track = sorted(tracks, key=rank)[0]

    segs = _fetch_transcript(track)
    out["transcript_lang"] = track.get("languageCode", "")
    out["transcript_auto"] = track.get("kind") == "asr"
    out["available_langs"] = sorted({t.get("languageCode", "") for t in tracks})
    out["transcript"] = segs
    out["transcript_text"] = " ".join(s["text"] for s in segs)
    out["transcript_timestamped"] = "\n".join(
        f"[{_fmt_ts(s['start'])}] {s['text']}" for s in segs)
    out["words"] = len(out["transcript_text"].split())
    if not segs:
        out["note"] = _NO_TRANSCRIPT_HINT.format(
            why="the caption URL returned an empty body", extra=ytdlp_err)
    return out


# --------------------------------------------------------------------------- #
# Reddit                                                                       #
# --------------------------------------------------------------------------- #
_RD_LAST = [0.0]  # last Reddit request time, so back-to-back calls self-throttle
_RD_GAP = float(os.environ.get("AIOS_REDDIT_GAP", "1.5"))


def _reddit_get(url: str, tries: int = 4) -> str:
    """Reddit rate-limits hard (429) and 403s its JSON API from many networks.
    Space requests out, back off on 429, and let 403 through so the caller can
    fall back to RSS."""
    import time
    last = None
    for i in range(tries):
        wait = _RD_GAP - (time.time() - _RD_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _RD_LAST[0] = time.time()
        try:
            return _get(url)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and i < tries - 1:
                time.sleep(4 * (i + 1))  # 4s, 8s, 12s
                continue
            raise
    raise last if last else RuntimeError("reddit fetch failed")


# Once Reddit 403s the JSON API we stop asking: retrying it on every call burns two
# requests and pushes us into 429 territory before the RSS fallback even runs.
_RD_JSON_BLOCKED_UNTIL = [0.0]


def _reddit_json(path: str, params: str = "") -> dict | list | None:
    """Reddit's public JSON. Blocked (403) on many networks — callers fall back to RSS."""
    import time
    if time.time() < _RD_JSON_BLOCKED_UNTIL[0]:
        return None
    for host in ("https://www.reddit.com", "https://old.reddit.com"):
        try:
            raw = _reddit_get(host + path + params, tries=1)
            if raw.lstrip()[:1] in ("{", "["):
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
            break
        except Exception:
            continue
    _RD_JSON_BLOCKED_UNTIL[0] = time.time() + 900  # re-probe in 15 minutes
    return None


# Reddit blocks its JSON API from a lot of networks but still serves RSS, so RSS is
# the fallback that keeps this working without an OAuth app. It carries titles,
# authors and bodies — but not scores, which is why score is None on this path.
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)


def _rss_entries(xml: str) -> list[dict]:
    out = []
    for block in _ENTRY.findall(xml):
        def pick(pat, default=""):
            m = re.search(pat, block, re.S)
            return _html.unescape(m.group(1)).strip() if m else default
        content = pick(r'<content type="html">(.*?)</content>')
        # RSS wraps link posts in "submitted by /u/x [link] [comments]" — that's
        # chrome, not the post, and reporting it as the body reads as a real answer.
        body_txt = readable(_html.unescape(content))
        if re.fullmatch(r"\s*submitted by\s+/u/\S+\s*(\[link\]|\[comments\]|\s)*",
                        body_txt, re.I):
            body_txt = ""
        out.append({
            "title": re.sub(r"\s+", " ", pick(r"<title>(.*?)</title>")),
            "author": pick(r"<name>(.*?)</name>").lstrip("/").replace("u/", "", 1),
            "url": (re.search(r'<link[^>]+href="([^"]+)"', block) or [None, ""])[1]
                   if re.search(r'<link[^>]+href="([^"]+)"', block) else "",
            "updated": pick(r"<updated>(.*?)</updated>"),
            "body": body_txt[:4000],
        })
    return out


def _reddit_rss(path: str, params: str = "") -> list[dict] | None:
    try:
        xml = _reddit_get("https://www.reddit.com" + path + params)
    except Exception:
        return None
    return _rss_entries(xml) if "<entry>" in xml else None


def _walk_comments(children: list, depth: int = 0, limit: int = 200,
                   out: list | None = None) -> list:
    out = out if out is not None else []
    for c in children:
        if len(out) >= limit:
            break
        d = c.get("data") or {}
        if c.get("kind") == "more":
            continue
        if d.get("body"):
            out.append({"author": d.get("author", "[deleted]"), "score": d.get("score", 0),
                        "depth": depth, "body": d["body"][:4000],
                        "created": d.get("created_utc")})
        replies = d.get("replies")
        if isinstance(replies, dict):
            _walk_comments((replies.get("data") or {}).get("children", []),
                           depth + 1, limit, out)
    return out


def reddit(target: str, limit: int = 60, sort: str = "top") -> dict:
    """A post with its comments, a subreddit's listing, or a search.

    Tries the JSON API first (richer: scores, ratios, full trees). Reddit 403s that
    from many networks, so RSS is the fallback — same content, no scores.
    """
    t = (target or "").strip()
    if not t:
        return _err("Give a Reddit URL, r/subreddit, or a search phrase.")

    try:
        # 1) A specific post -> post body + comment tree
        if "/comments/" in t:
            path = urllib.parse.urlparse(t if t.startswith("http")
                                         else "https://www.reddit.com" + t).path.rstrip("/")
            data = _reddit_json(path + ".json", f"?limit={limit}&sort={sort}")
            if data and len(data) >= 2:
                post = ((data[0].get("data") or {}).get("children") or [{}])[0].get("data", {})
                comments = _walk_comments((data[1].get("data") or {}).get("children", []),
                                          limit=limit)
                return {"ok": True, "kind": "post", "source": "json",
                        "title": post.get("title", ""), "subreddit": post.get("subreddit", ""),
                        "author": post.get("author", ""), "score": post.get("score", 0),
                        "upvote_ratio": post.get("upvote_ratio"),
                        "num_comments": post.get("num_comments", 0),
                        "url": "https://www.reddit.com" + post.get("permalink", ""),
                        "link": post.get("url", ""), "created": post.get("created_utc"),
                        "selftext": (post.get("selftext") or "")[:20000],
                        "flair": post.get("link_flair_text"),
                        "comments": comments, "comments_returned": len(comments)}
            ents = _reddit_rss(path + "/.rss", f"?limit={limit}&sort={sort}")
            if ents is None:
                return _err("Reddit blocked both its JSON API and RSS for this post "
                            "(403/429). Wait a minute and retry.")
            head, rest = (ents[0] if ents else {}), ents[1:]
            return {"ok": True, "kind": "post", "source": "rss",
                    "title": head.get("title", ""), "author": head.get("author", ""),
                    "url": head.get("url", ""), "selftext": head.get("body", ""),
                    "score": None, "num_comments": len(rest),
                    "comments": [{"author": e["author"], "body": e["body"], "score": None,
                                  "depth": 0, "created": e.get("updated")} for e in rest],
                    "comments_returned": len(rest),
                    "note": "Read via RSS (Reddit blocked the JSON API) — scores unavailable."}

        # 2) A subreddit listing
        m = re.search(r"(?:^|/)r/([A-Za-z0-9_]+)", t)
        if m and " " not in t.strip():
            sub = m.group(1)
            listing = sort if sort in ("hot", "new", "top", "rising", "best") else "hot"
            data = _reddit_json(f"/r/{sub}/{listing}.json", f"?limit={min(limit, 100)}")
            if data:
                posts = [{"title": (c.get("data") or {}).get("title", ""),
                          "author": (c.get("data") or {}).get("author", ""),
                          "score": (c.get("data") or {}).get("score", 0),
                          "num_comments": (c.get("data") or {}).get("num_comments", 0),
                          "flair": (c.get("data") or {}).get("link_flair_text"),
                          "url": "https://www.reddit.com" + (c.get("data") or {}).get("permalink", ""),
                          "selftext": ((c.get("data") or {}).get("selftext") or "")[:600]}
                         for c in (data.get("data") or {}).get("children", [])]
                return {"ok": True, "kind": "subreddit", "source": "json",
                        "subreddit": sub, "sort": listing, "posts": posts, "count": len(posts)}
            ents = _reddit_rss(f"/r/{sub}/{listing}/.rss", f"?limit={min(limit, 100)}")
            if ents is None:
                return _err(f"Could not read r/{sub} — private, banned, or Reddit "
                            f"blocked this network (403/429). Retry shortly.")
            posts = [{"title": e["title"], "author": e["author"], "url": e["url"],
                      "score": None, "selftext": e["body"][:600]} for e in ents]
            return {"ok": True, "kind": "subreddit", "source": "rss", "subreddit": sub,
                    "sort": listing, "posts": posts, "count": len(posts),
                    "note": "Read via RSS (Reddit blocked the JSON API) — scores unavailable."}

        # 3) Search
        q = urllib.parse.quote(t)
        data = _reddit_json("/search.json", f"?q={q}&limit={min(limit, 100)}&sort=relevance")
        if data:
            posts = [{"title": (c.get("data") or {}).get("title", ""),
                      "subreddit": (c.get("data") or {}).get("subreddit", ""),
                      "score": (c.get("data") or {}).get("score", 0),
                      "num_comments": (c.get("data") or {}).get("num_comments", 0),
                      "url": "https://www.reddit.com" + (c.get("data") or {}).get("permalink", ""),
                      "selftext": ((c.get("data") or {}).get("selftext") or "")[:600]}
                     for c in (data.get("data") or {}).get("children", [])]
            return {"ok": True, "kind": "search", "source": "json", "query": t,
                    "posts": posts, "count": len(posts)}
        ents = _reddit_rss("/search.rss", f"?q={q}&limit={min(limit, 100)}&sort=relevance")
        if ents is None:
            return _err("Reddit search is blocked from this network right now (403/429).")
        return {"ok": True, "kind": "search", "source": "rss", "query": t,
                "posts": [{"title": e["title"], "author": e["author"], "url": e["url"],
                           "score": None, "selftext": e["body"][:600]} for e in ents],
                "count": len(ents),
                "note": "Read via RSS (Reddit blocked the JSON API) — scores unavailable."}
    except urllib.error.HTTPError as e:
        return _err(f"Reddit HTTP {e.code} — it heavily rate-limits unauthenticated "
                    f"reads. Wait a minute and retry.")
    except Exception as e:
        return _err(f"Reddit read failed: {e}")


# --------------------------------------------------------------------------- #
# Hacker News                                                                  #
# --------------------------------------------------------------------------- #
def hackernews(query: str = "", story_id: str = "", limit: int = 20) -> dict:
    try:
        if story_id:
            d = json.loads(_get(f"https://hn.algolia.com/api/v1/items/{story_id}"))

            def walk(node, depth=0, out=None):
                out = out if out is not None else []
                for ch in (node.get("children") or []):
                    if ch.get("text"):
                        out.append({"author": ch.get("author"), "depth": depth,
                                    "text": re.sub(r"<[^>]+>", "",
                                                   _html.unescape(ch["text"]))[:3000]})
                    walk(ch, depth + 1, out)
                return out
            return {"ok": True, "kind": "story", "title": d.get("title"),
                    "url": d.get("url"), "points": d.get("points"),
                    "author": d.get("author"), "comments": walk(d)[:150]}
        d = json.loads(_get("https://hn.algolia.com/api/v1/search?query=" +
                            urllib.parse.quote(query) + f"&hitsPerPage={limit}"))
        return {"ok": True, "kind": "search", "query": query,
                "hits": [{"title": h.get("title"), "url": h.get("url"),
                          "points": h.get("points"), "id": h.get("objectID"),
                          "comments": h.get("num_comments")} for h in d.get("hits", [])]}
    except Exception as e:
        return _err(f"Hacker News read failed: {e}")


# --------------------------------------------------------------------------- #
# Generic page reader + search                                                 #
# --------------------------------------------------------------------------- #
_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header|form)\b[^>]*>.*?</\1>",
                   re.S | re.I)


def readable(html_text: str) -> str:
    """HTML → text, without dragging in a parser dependency."""
    s = _DROP.sub(" ", html_text)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"</(p|div|li|h[1-6]|tr|br)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def fetch(url: str, max_chars: int = 20000) -> dict:
    """Any page as readable text. Delegates when it recognises the source."""
    url = (url or "").strip()
    if not url:
        return _err("No URL given.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urllib.parse.urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return youtube(url)
    if "reddit.com" in host:
        return reddit(url)
    try:
        raw = _get(url)
    except urllib.error.HTTPError as e:
        return _err(f"HTTP {e.code} fetching {url}")
    except Exception as e:
        return _err(f"Could not fetch {url}: {e}")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:300]
    text = readable(raw)
    return {"ok": True, "kind": "page", "url": url, "title": title,
            "text": text[:max_chars], "chars": len(text),
            "truncated": len(text) > max_chars}


def search(query: str, limit: int = 10) -> dict:
    """DuckDuckGo's HTML endpoint — no API key, no tracking."""
    try:
        raw = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    except Exception as e:
        return _err(f"Search failed: {e}")
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                         raw, re.S):
        href, title = m.group(1), _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if href.startswith("//duckduckgo.com/l/?uddg="):
            q = urllib.parse.parse_qs(urllib.parse.urlparse("https:" + href).query)
            href = (q.get("uddg") or [href])[0]
        if "duckduckgo.com/y.js" in href or "ad_domain=" in href:
            continue  # sponsored slot, not a result
        if title:
            results.append({"title": title, "url": href})
        if len(results) >= limit:
            break
    if not results:
        return _err("No results parsed (DuckDuckGo may have changed its HTML).")
    return {"ok": True, "query": query, "results": results}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    arg = " ".join(sys.argv[2:])
    fn = {"youtube": youtube, "yt": youtube, "reddit": reddit, "hn": hackernews,
          "fetch": fetch, "search": search}.get(cmd)
    if not fn:
        print("usage: aios_web.py [youtube|reddit|hn|fetch|search] <arg>")
        sys.exit(1)
    print(json.dumps(fn(arg), indent=2)[:6000])
