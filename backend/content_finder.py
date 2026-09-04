import re
import json
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


# ---------------------------------------------------------------------------
# HTTP helper — use curl instead of requests/yt-dlp (avoids InnerTube hangs)
# ---------------------------------------------------------------------------

_CURL_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, timeout: int = 20) -> str:
    cmd = [
        "curl", "-s", "--max-time", str(timeout), "-L",
        "-A", _CURL_UA,
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Channel ID resolution
# ---------------------------------------------------------------------------

def resolve_channel_id(channel_url: str) -> str:
    """Resolve any YouTube channel URL to its UC... channel ID.

    Handles:
      • Direct UC... IDs pasted as-is
      • /channel/UCxxxxx URLs
      • @handle URLs  (scrape channel page; fallback via YouTube search)
      • /c/ and /user/ legacy URLs
    """
    if not channel_url:
        return ""

    # 1. Direct UC... ID in the input (user pasted the raw ID)
    m = re.match(r"^\s*(UC[A-Za-z0-9_-]{22})\s*$", channel_url)
    if m:
        return m.group(1)

    # 2. UC... ID embedded in a /channel/ URL
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", channel_url)
    if m:
        return m.group(1)

    # 3. For @handle URLs, also try YouTube search to find the channel
    handle_match = re.search(r"@([A-Za-z0-9_.-]+)", channel_url)

    # Scrape the channel page directly first
    def _scrape(url: str) -> str:
        html = _curl_get(url, timeout=15)
        if not html:
            return ""
        # Try ytInitialData JSON
        m2 = re.search(r"var ytInitialData\s*=\s*({.+?});\s*</script>", html, re.DOTALL)
        if m2:
            try:
                data = json.loads(m2.group(1))
                cid = (
                    data.get("metadata", {})
                        .get("channelMetadataRenderer", {})
                        .get("externalId", "")
                )
                if cid:
                    return cid
            except Exception:
                pass
        # Raw HTML fallback
        for pattern in [
            r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
            r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
            r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
        ]:
            m3 = re.search(pattern, html)
            if m3:
                return m3.group(1)
        return ""

    cid = _scrape(channel_url)
    if cid:
        return cid

    # 4. @handle fallback: search YouTube for the channel name and grab first result's channel ID
    if handle_match:
        handle = handle_match.group(1)
        encoded = urllib.parse.quote(handle)
        search_html = _curl_get(
            f"https://www.youtube.com/results?search_query={encoded}&sp=EgIQAg%3D%3D",
            timeout=15,
        )  # sp=EgIQAg%3D%3D filters to channels only
        if search_html:
            m4 = re.search(r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', search_html)
            if m4:
                return m4.group(1)
        # Also try without filter
        search_html2 = _curl_get(
            f"https://www.youtube.com/results?search_query={urllib.parse.quote('@'+handle)}",
            timeout=15,
        )
        if search_html2:
            m5 = re.search(r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', search_html2)
            if m5:
                return m5.group(1)

    return ""


# ---------------------------------------------------------------------------
# RSS feed scraper — gets the latest 15 videos from any YouTube channel
# ---------------------------------------------------------------------------

def scrape_channel_rss(channel_id: str, official_channel_ids=None, query: str = "") -> list:
    """Fetch a channel's RSS feed and return videos related to the query.

    official_channel_ids can be a set/frozenset of UC… IDs, a single string ID,
    or None/empty — used to determine the stolen flag.
    When query is provided, videos whose title doesn't match are dropped so
    unrelated content from the same channel isn't included.
    """
    if not channel_id:
        return []

    # Normalise official_channel_ids to a set
    if official_channel_ids is None:
        _official = set()
    elif isinstance(official_channel_ids, str):
        _official = {official_channel_ids} if official_channel_ids else set()
    else:
        _official = set(official_channel_ids)

    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml_text = _curl_get(url, timeout=15)
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt":   "http://www.youtube.com/xml/schemas/2015",
        "media":"http://search.yahoo.com/mrss/",
    }

    results = []
    for entry in root.findall("atom:entry", ns):
        vid_id = (entry.findtext("yt:videoId", namespaces=ns) or "").strip()
        title  = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        ch_el  = entry.find("atom:author/atom:name", ns)
        channel = (ch_el.text or "").strip() if ch_el is not None else ""

        ch_uri = entry.findtext("atom:author/atom:uri", namespaces=ns) or ""

        if not vid_id:
            continue

        # Drop videos unrelated to the search keyword
        if query and not _title_matches_query(title, query):
            continue

        is_short = False   # RSS doesn't tell us, assume not
        vid_url  = f"https://www.youtube.com/watch?v={vid_id}"

        if _official and channel_id:
            stolen = channel_id not in _official
        else:
            stolen = None

        results.append({
            "title":        title,
            "channel":      channel,
            "channel_url":  ch_uri,
            "channel_id":   channel_id,
            "url":          vid_url,
            "views":        None,
            "views_fmt":    "—",
            "duration":     None,
            "duration_fmt": "—",
            "upload_date":  "",
            "thumbnail":    f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
            "platform":     "YouTube",
            "is_short":     is_short,
            "stolen":       stolen,
            "video_id":     vid_id,
            "source":       "rss_scrape",
        })
    return results


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_views(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _parse_views(s: str) -> int | None:
    if not s:
        return None
    s = re.sub(r"[,\s]*(views?|watching)?", "", s, flags=re.IGNORECASE).strip()
    m = re.match(r"([\d.]+)([KMB]?)", s, re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1))
        suffix = m.group(2).upper()
        if suffix == "K":
            n *= 1_000
        elif suffix == "M":
            n *= 1_000_000
        elif suffix == "B":
            n *= 1_000_000_000
        return int(n)
    except Exception:
        return None


def _parse_duration(s: str) -> int | None:
    if not s:
        return None
    parts = s.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return None
    return None


def _title_matches_query(title: str, query: str) -> bool:
    """Return True if the title is plausibly related to the query.

    Used only for RSS/supplemental results where YouTube's own search
    relevance doesn't apply. Passes if the full query OR any meaningful
    keyword word appears in the title (handles spelling differences like
    'ron soya' vs 'ron soyaa').
    """
    title_l = title.lower()
    query_l = query.lower().strip()
    if query_l in title_l:
        return True
    words = [w for w in query_l.split() if len(w) > 2]
    if not words:
        return True
    # Pass if ANY meaningful word from the query is present
    return any(w in title_l for w in words)


# ---------------------------------------------------------------------------
# YouTube search via curl + ytInitialData parsing
# ---------------------------------------------------------------------------

def _parse_search_page(html: str, query: str, official_channel_ids) -> list:
    """Extract video results from a YouTube search results HTML page.

    official_channel_ids: set of UC… IDs, single string, or None.
    """
    if official_channel_ids is None:
        official_channel_ids = set()
    elif isinstance(official_channel_ids, str):
        official_channel_ids = {official_channel_ids} if official_channel_ids else set()
    else:
        official_channel_ids = set(official_channel_ids)
    m = re.search(r"var ytInitialData\s*=\s*({.+?});\s*</script>", html, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    contents = (
        data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
    )

    results = []
    for sec in contents:
        items = sec.get("itemSectionRenderer", {}).get("contents", [])
        for item in items:
            vr = item.get("videoRenderer", {})
            if not vr:
                continue

            vid_id = vr.get("videoId", "")
            if not vid_id:
                continue

            title_runs = vr.get("title", {}).get("runs", []) or []
            title = title_runs[0].get("text", "") if title_runs else ""

            owner_runs = vr.get("ownerText", {}).get("runs", []) or [{}]
            owner_run = owner_runs[0]
            channel = owner_run.get("text", "")
            browse = (
                owner_run.get("navigationEndpoint", {})
                         .get("browseEndpoint", {})
            )
            result_channel_id = browse.get("browseId", "")
            channel_url_path = browse.get("canonicalBaseUrl", "")
            channel_url = (
                "https://www.youtube.com" + channel_url_path
                if channel_url_path and not channel_url_path.startswith("http")
                else channel_url_path
            )

            duration_text = vr.get("lengthText", {}).get("simpleText", "")
            duration_secs = _parse_duration(duration_text)

            views_runs = vr.get("viewCountText", {}).get("runs", []) or []
            views_text = "".join(r.get("text", "") for r in views_runs)
            if not views_text:
                views_text = vr.get("viewCountText", {}).get("simpleText", "") or ""
            views = _parse_views(views_text)

            published = vr.get("publishedTimeText", {}).get("simpleText", "")

            thumbnails = vr.get("thumbnail", {}).get("thumbnails", []) or []
            thumb = thumbnails[-1].get("url", "") if thumbnails else ""

            nav_url = (
                vr.get("navigationEndpoint", {})
                  .get("commandMetadata", {})
                  .get("webCommandMetadata", {})
                  .get("url", "")
            )
            is_short = "/shorts/" in nav_url or (
                duration_secs is not None and duration_secs <= 60
            )
            vid_url = (
                f"https://www.youtube.com/shorts/{vid_id}"
                if is_short
                else f"https://www.youtube.com/watch?v={vid_id}"
            )

            # Stolen flag: True = not from official channel, False = official, None = unknown
            if official_channel_ids and result_channel_id:
                stolen = result_channel_id not in official_channel_ids
            else:
                stolen = None

            results.append({
                "title": title,
                "channel": channel,
                "channel_url": channel_url,
                "channel_id": result_channel_id,
                "url": vid_url,
                "views": views,
                "views_fmt": _fmt_views(views),
                "duration": duration_secs,
                "duration_fmt": duration_text or "—",
                "upload_date": published,
                "thumbnail": thumb,
                "platform": "YouTube",
                "is_short": is_short,
                "stolen": stolen,
                "video_id": vid_id,
                "source": "youtube_search",
            })

    return results


def _run_single_query(
    search_url: str,
    limit: int,
    official_channel_ids,
    official_channel_url: str,
    query: str = "",
) -> list:
    """Convert a ytsearch:… URL to a real YouTube search and return results.
    Fetches both relevance-sorted AND upload-date-sorted pages so recently
    uploaded stolen content that ranks low by relevance is still caught.
    """
    m = re.match(r"ytsearch\d+:(.*)", search_url)
    if not m:
        return []
    yt_query = m.group(1).strip()
    encoded = urllib.parse.quote(yt_query)

    all_results: list = []
    seen_ids: set = set()

    def _merge(html: str) -> None:
        for r in _parse_search_page(html, query, official_channel_ids):
            vid = r.get("video_id")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                all_results.append(r)

    # Page 1 – relevance order (YouTube default)
    html_rel = _curl_get(
        f"https://www.youtube.com/results?search_query={encoded}", timeout=20
    )
    if html_rel:
        _merge(html_rel)

    # Page 2 – newest uploads first (sp=CAI%3D)
    # Catches recent reuploads that relevance ranking buries
    html_date = _curl_get(
        f"https://www.youtube.com/results?search_query={encoded}&sp=CAI%3D", timeout=20
    )
    if html_date:
        _merge(html_date)

    return all_results


# ---------------------------------------------------------------------------
# Search query variations
# ---------------------------------------------------------------------------

def _variations_for_type(query: str, content_type: str) -> list[str]:
    """Return search-term variations for a single content type."""
    y, yp = datetime.now().year, datetime.now().year - 1
    if content_type == "music":
        return [
            query,
            f"{query} live performance",
            f"{query} full concert",
            f"{query} official",
            f"{query} music video",
            f"{query} full show",
            f"{query} live {y}",
            f"{query} live {yp}",
            f"{query} HD",
            f"{query} shorts",
        ]
    if content_type == "event":
        return [
            query,
            f"{query} full show",
            f"{query} live",
            f"{query} full recording",
            f"{query} highlights",
            f"{query} {y}",
            f"{query} {yp}",
            f"{query} HD",
            f"{query} shorts",
        ]
    if content_type == "other":
        return [
            query,
            f"{query} full",
            f"{query} HD",
            f"{query} {y}",
            f"{query} {yp}",
            f"{query} shorts",
            f"{query} new",
        ]
    # teledrama (default)
    return [
        query,
        f'"{query}"',
        f"{query} full episode",
        f"{query} episode",
        f"{query} latest episode",
        f"{query} new episode",
        f"{query} sinhala",
        f"{query} watch online",
        f"{query} shorts",
        f"{query} {y}",
        f"{query} {yp}",
        f"{query} HD",
    ]


def build_search_queries(
    query: str,
    per_query: int = 20,
    content_type: str = "teledrama",
    extra_keywords: list | None = None,
) -> list[tuple[str, int]]:
    # Support comma-separated multi-type (e.g. "teledrama,music")
    types = [t.strip() for t in content_type.split(",") if t.strip()]
    if not types:
        types = ["teledrama"]

    # All keywords to search: primary + any extras
    all_keywords = [query]
    if extra_keywords:
        all_keywords += [k.strip() for k in extra_keywords if k.strip() and k.strip() != query]

    seen: set = set()
    variations: list = []
    for kw in all_keywords:
        for ct in types:
            for v in _variations_for_type(kw, ct):
                if v not in seen:
                    seen.add(v)
                    variations.append(v)

    return [(f"ytsearch{per_query}:{v}", per_query) for v in variations]


# ---------------------------------------------------------------------------
# Background search worker
# ---------------------------------------------------------------------------

def run_search_job(
    job_id: int,
    query: str,
    official_channel_url: str,
    queries: list[tuple[str, int]],
    skip_known: bool = True,
    content_type: str = "teledrama",
    extra_keywords: list | None = None,
):
    """
    Background worker: resolves channel ID via curl, runs all search queries
    in parallel via curl, merges results into the DB search_jobs row.
    After search completes, also scrapes the RSS feed of every stolen channel found
    to surface videos that YouTube hides from search results.
    Skips video IDs already seen in previous searches for the same show.
    """
    from database import append_search_results, get_known_video_ids, save_known_videos

    # Support multiple official channels (comma-separated URLs)
    official_channel_ids: set[str] = set()
    if official_channel_url:
        raw_urls = [u.strip() for u in official_channel_url.split(",") if u.strip()]
        for raw_url in raw_urls:
            cid = resolve_channel_id(raw_url)
            if cid:
                official_channel_ids.add(cid)
    # Keep a single representative ID for legacy DB field (first one resolved)
    official_channel_id = next(iter(official_channel_ids), "")

    already_known = get_known_video_ids(query) if skip_known else set()

    # Phase 1: run all keyword search queries in parallel
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(
                _run_single_query, url, limit, official_channel_ids, official_channel_url, query
            ): url
            for url, limit in queries
        }
        for future in as_completed(futures):
            try:
                results = future.result()
            except Exception:
                results = []

            new_results = [r for r in results if r.get("video_id") not in already_known]
            already_known.update(r["video_id"] for r in new_results if r.get("video_id"))

            save_known_videos(query, new_results)
            append_search_results(job_id, new_results, is_incremental=skip_known)

    # ── Phase 2: RSS scrape every stolen channel found in Phase 1 ──────────────
    # Catches videos YouTube hides from search (channel with many reuploads but
    # low SEO). Uses count_query=False so status doesn't flip back to running.
    from database import get_search_job
    job = get_search_job(job_id)
    if not job:
        return

    current_results = job.get("results", [])
    stolen_channel_ids = list({
        r["channel_id"]
        for r in current_results
        if r.get("stolen") is True and r.get("channel_id")
    })

    if stolen_channel_ids:
        with ThreadPoolExecutor(max_workers=6) as ex:
            rss_futures = {
                ex.submit(scrape_channel_rss, cid, official_channel_ids, query): cid
                for cid in stolen_channel_ids
            }
            for future in as_completed(rss_futures):
                try:
                    rss_results = future.result()
                except Exception:
                    rss_results = []
                new_results = [r for r in rss_results if r.get("video_id") not in already_known]
                already_known.update(r["video_id"] for r in new_results if r.get("video_id"))
                save_known_videos(query, new_results)
                append_search_results(job_id, new_results, is_incremental=skip_known, count_query=False)

    # ── Phase 3: Web-index dorks (Bing + DDG site:youtube.com) ─────────────────
    # Web search engines index ALL YouTube videos without deduplication.
    # A reuploader who copies the exact official title is invisible in YT search
    # but indexed by Bing/DDG — this phase catches them.
    for dork_fn in (search_bing_youtube, search_ddg_youtube):
        try:
            dork_results = dork_fn(query)
        except Exception:
            dork_results = []
        # Resolve channel IDs and stolen flag for web-dork results
        resolved = []
        for r in dork_results:
            if r.get("video_id") in already_known:
                continue
            # Try to enrich with full metadata via YouTube search for this video ID
            vid_html = _curl_get(
                f"https://www.youtube.com/watch?v={r['video_id']}", timeout=15
            )
            if vid_html:
                cid_m = re.search(r'"channelId"\s*:"(UC[A-Za-z0-9_-]{22})"', vid_html)
                ch_m  = re.search(r'"ownerChannelName"\s*:"([^"]+)"', vid_html)
                if cid_m:
                    r["channel_id"] = cid_m.group(1)
                    if official_channel_ids:
                        r["stolen"] = r["channel_id"] not in official_channel_ids
                if ch_m:
                    r["channel"] = ch_m.group(1)
            resolved.append(r)
        already_known.update(r["video_id"] for r in resolved if r.get("video_id"))
        save_known_videos(query, resolved)
        append_search_results(job_id, resolved, is_incremental=skip_known, count_query=False)

    # ── Phase 4: Mirror-title search ────────────────────────────────────────────
    # Fetch official channel RSS → search YouTube for each exact episode title.
    # Reuploaders who copy the exact title verbatim show up in title-specific
    # searches even though they're buried in general keyword searches.
    if official_channel_url:
        try:
            mirror_results = find_title_mirrors(official_channel_url, query, official_channel_ids)
        except Exception:
            mirror_results = []
        new_results = [r for r in mirror_results if r.get("video_id") not in already_known]
        already_known.update(r["video_id"] for r in new_results if r.get("video_id"))
        save_known_videos(query, new_results)
        append_search_results(job_id, new_results, is_incremental=skip_known, count_query=False)


# ---------------------------------------------------------------------------
# Deep-scan helpers — find videos YouTube search hides
# ---------------------------------------------------------------------------

def _yt_video_meta(video_id: str, official_channel_ids: set) -> dict | None:
    """Fetch minimal metadata for a YouTube video ID via oEmbed (no API key needed).
    Returns a result dict in the standard format, or None on failure.
    """
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    raw = _curl_get(oembed_url, timeout=10)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    title = d.get("title", "")
    channel = d.get("author_name", "")
    if not title:
        return None
    vid_url = f"https://www.youtube.com/watch?v={video_id}"
    # oEmbed doesn't give channel_id, so stolen is None (unknown)
    return {
        "title": title,
        "channel": channel,
        "channel_url": d.get("author_url", ""),
        "channel_id": "",
        "url": vid_url,
        "views": None,
        "views_fmt": "—",
        "duration": None,
        "duration_fmt": "—",
        "upload_date": "",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "platform": "YouTube",
        "is_short": False,
        "stolen": None,
        "video_id": video_id,
        "source": "web_dork",
    }


def _extract_yt_video_ids(html: str) -> list[str]:
    """Pull all unique youtube.com/watch?v= IDs out of any HTML blob."""
    ids = re.findall(r'(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})', html)
    seen = set()
    result = []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            result.append(vid)
    return result


def search_bing_youtube(query: str) -> list:
    """Bing site:youtube.com/watch dork — finds YT videos Bing indexed that
    don't surface in YouTube's own search (no deduplication, no SEO bias)."""
    dork = f'site:youtube.com/watch "{query}"'
    encoded = urllib.parse.quote(dork)
    html = _curl_get(f"https://www.bing.com/search?q={encoded}&count=30", timeout=25)
    if not html:
        return []
    ids = _extract_yt_video_ids(html)
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_yt_video_meta, vid, set()): vid for vid in ids}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    return results


def search_ddg_youtube(query: str) -> list:
    """DuckDuckGo site:youtube.com dork — second web-index engine for coverage."""
    dork = f'site:youtube.com/watch "{query}"'
    encoded = urllib.parse.quote(dork)
    html = _curl_get(f"https://html.duckduckgo.com/html/?q={encoded}", timeout=25)
    if not html:
        return []
    ids = _extract_yt_video_ids(html)
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_yt_video_meta, vid, set()): vid for vid in ids}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    return results


def find_title_mirrors(official_channel_url: str, query: str, official_channel_ids: set) -> list:
    """Mirror-search: fetch official channel RSS, then for each recent episode
    search YouTube for its exact title. Reuploaders often copy the title verbatim
    so they show in YouTube search — but are buried below the official result.
    Runs each title search in parallel and returns only non-official matches.
    """
    if not official_channel_url:
        return []

    # Resolve official channel IDs if not already done
    ids = set(official_channel_ids)
    if not ids:
        for raw in [u.strip() for u in official_channel_url.split(",") if u.strip()]:
            cid = resolve_channel_id(raw)
            if cid:
                ids.add(cid)
    if not ids:
        return []

    # Fetch RSS for first official channel to get recent episode titles
    first_id = next(iter(ids))
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={first_id}"
    xml_text = _curl_get(rss_url, timeout=15)
    if not xml_text or "<entry>" not in xml_text:
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt":   "http://www.youtube.com/xml/schemas/2015",
    }
    titles = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        if title:
            titles.append(title)
    if not titles:
        return []

    # For each title, run a YouTube search and collect non-official results
    def _search_title(title: str) -> list:
        encoded = urllib.parse.quote(title)
        html = _curl_get(
            f"https://www.youtube.com/results?search_query={encoded}", timeout=20
        )
        if not html:
            return []
        all_r = _parse_search_page(html, query, ids)
        # Only return results NOT from the official channel
        return [r for r in all_r if r.get("channel_id") and r["channel_id"] not in ids]

    results = []
    seen_ids: set = set()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_search_title, t): t for t in titles}
        for f in as_completed(futures):
            try:
                for r in f.result():
                    vid = r.get("video_id")
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        r["stolen"] = True  # not from official channel
                        results.append(r)
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Cross-platform search helpers
# ---------------------------------------------------------------------------

# Patterns to extract (platform, native_id, canonical_url) from any URL
_PLATFORM_PATTERNS = [
    (r"dailymotion\.com/video/([A-Za-z0-9]+)", "Dailymotion",
     "https://www.dailymotion.com/video/{id}"),
    (r"(?:facebook\.com/watch/?\?v=|facebook\.com/video\.php\?v=)(\d+)", "Facebook",
     "https://www.facebook.com/watch/?v={id}"),
    (r"fb\.watch/([A-Za-z0-9_-]+)", "Facebook",
     "https://fb.watch/{id}"),
    (r"tiktok\.com/@[^/?]+/video/(\d+)", "TikTok",
     "https://www.tiktok.com/video/{id}"),
    (r"vimeo\.com/(\d+)", "Vimeo",
     "https://vimeo.com/{id}"),
]


def _extract_platform_video(url: str):
    """Return (platform, video_id, canonical_url) or None if no match."""
    for pattern, platform, url_tpl in _PLATFORM_PATTERNS:
        m = re.search(pattern, url)
        if m:
            vid_id = m.group(1)
            return platform, vid_id, url_tpl.format(id=vid_id)
    return None


def _decode_ddg_url(href: str) -> str:
    """Resolve a DuckDuckGo redirect href to the real destination URL."""
    if not href:
        return ""
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            return urllib.parse.unquote(m.group(1))
    return href


def search_duckduckgo_dork(query: str, site: str = "") -> list:
    """DuckDuckGo HTML scraping with optional site: dork to find stolen content
    on non-YouTube platforms (Dailymotion, Facebook, TikTok, Vimeo)."""
    dork = f'site:{site} "{query}"' if site else f'"{query}" video'
    encoded = urllib.parse.quote(dork)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = _curl_get(url, timeout=25)
    if not html:
        return []

    results = []
    seen: set = set()

    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        raw_href = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()

        real_url = _decode_ddg_url(raw_href)
        if not real_url:
            continue

        extracted = _extract_platform_video(real_url)
        if not extracted:
            continue

        platform, vid_id, canonical_url = extracted
        composite_id = f"{platform.lower()}_{vid_id}"
        if composite_id in seen:
            continue
        seen.add(composite_id)

        results.append({
            "title":        title or query,
            "channel":      "",
            "channel_url":  "",
            "channel_id":   "",
            "url":          canonical_url,
            "views":        None,
            "views_fmt":    "—",
            "duration":     None,
            "duration_fmt": "—",
            "upload_date":  "",
            "thumbnail":    "",
            "platform":     platform,
            "is_short":     False,
            "stolen":       None,
            "video_id":     composite_id,
            "source":       "duckduckgo_dork",
        })

    return results


def search_bing_web(query: str) -> list:
    """Bing web search to find stolen content on non-YouTube platforms."""
    dork = (
        f'"{query}" '
        f'(site:dailymotion.com OR site:facebook.com/watch OR site:tiktok.com OR site:vimeo.com)'
    )
    encoded = urllib.parse.quote(dork)
    url = f"https://www.bing.com/search?q={encoded}&count=20"
    html = _curl_get(url, timeout=20)
    if not html:
        return []

    results = []
    seen: set = set()

    for m in re.finditer(
        r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        raw_url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()

        extracted = _extract_platform_video(raw_url)
        if not extracted:
            continue

        platform, vid_id, canonical_url = extracted
        composite_id = f"{platform.lower()}_{vid_id}"
        if composite_id in seen:
            continue
        seen.add(composite_id)

        results.append({
            "title":        title or query,
            "channel":      "",
            "channel_url":  "",
            "channel_id":   "",
            "url":          canonical_url,
            "views":        None,
            "views_fmt":    "—",
            "duration":     None,
            "duration_fmt": "—",
            "upload_date":  "",
            "thumbnail":    "",
            "platform":     platform,
            "is_short":     False,
            "stolen":       None,
            "video_id":     composite_id,
            "source":       "bing_search",
        })

    return results


def search_dailymotion(query: str) -> list:
    """Dailymotion public API search (no auth required)."""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://api.dailymotion.com/videos"
        f"?search={encoded}"
        f"&fields=id,title,url,owner.screenname,views_total,duration,thumbnail_480_url"
        f"&limit=20&sort=relevance"
    )
    raw = _curl_get(url, timeout=20)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []

    results = []
    for item in data.get("list", []):
        vid_id = item.get("id", "")
        if not vid_id:
            continue

        duration = item.get("duration")
        dur_fmt = ""
        if duration:
            m_secs, s = divmod(int(duration), 60)
            h, mins = divmod(m_secs, 60)
            dur_fmt = f"{h}:{mins:02d}:{s:02d}" if h else f"{mins}:{s:02d}"

        views = item.get("views_total")
        channel_name = item.get("owner.screenname", "")

        if not _title_matches_query(item.get("title", ""), query):
            continue

        results.append({
            "title":        item.get("title", query),
            "channel":      channel_name,
            "channel_url":  f"https://www.dailymotion.com/{channel_name}" if channel_name else "",
            "channel_id":   "",
            "url":          item.get("url") or f"https://www.dailymotion.com/video/{vid_id}",
            "views":        views,
            "views_fmt":    _fmt_views(views),
            "duration":     duration,
            "duration_fmt": dur_fmt or "—",
            "upload_date":  "",
            "thumbnail":    item.get("thumbnail_480_url", ""),
            "platform":     "Dailymotion",
            "is_short":     False,
            "stolen":       None,
            "video_id":     f"dailymotion_{vid_id}",
            "source":       "dailymotion_api",
        })

    return results
