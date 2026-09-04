import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from database import (
    init_db, create_scan, update_scan, get_scan, list_scans,
    create_batch, update_batch_progress, get_batch, list_batches,
    create_scan_in_batch,
    create_search_job, get_search_job, list_search_jobs,
    get_all_known_stolen, get_known_show_stats, list_monitored_shows,
    set_video_confirmed,
    get_confirmed_stolen, list_confirmed_shows,
    get_all_reviewed, get_review_summary,
    delete_known_video, clear_known_videos, clear_all_known_videos, get_all_known_for_show,
    get_confirmed_map,
)
from acoustid_scanner import scan_audio
from url_downloader import download_url, is_valid_url, platform_name
from channel_scanner import extract_channel_videos
from content_finder import build_search_queries, run_search_job

load_dotenv()

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(Path(__file__).parent / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_MB = int(os.getenv("MAX_FILE_SIZE_MB", 500))

AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}

app = FastAPI(title="Copyright Detector API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/app", StaticFiles(directory=str(frontend_path), html=True), name="frontend")


@app.get("/")
def root():
    return RedirectResponse(url="/app/")


@app.on_event("startup")
def startup():
    init_db()


def _classify_file(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"


def _compute_risk(audio_results: list) -> str:
    flagged = any(r.get("detected") for r in audio_results)
    if flagged:
        violations = sum(1 for r in audio_results if r.get("detected"))
        return "high" if violations >= 3 else "medium"
    return "low"


def _build_summary(audio_results: list) -> dict:
    music_segments = [r for r in audio_results if r.get("detected")]
    violations = []
    seen_tracks = {}

    for seg in music_segments:
        artists = seg.get("artists") or []
        artist_str = ", ".join(artists) or "Unknown artist"
        track_key = f"{seg.get('title', '')}|{artist_str}"

        if track_key in seen_tracks:
            seen_tracks[track_key]["end_fmt"] = seg["end_fmt"]
            seen_tracks[track_key]["end_sec"] = seg["end_sec"]
            seen_tracks[track_key]["segments"].append(seg["start_fmt"])
        else:
            entry = {
                "type": "music",
                "title": seg.get("title", "Unknown"),
                "artists": artists,
                "artist_str": artist_str,
                "detail": f"{seg.get('title', 'Unknown')} by {artist_str}",
                "label": seg.get("label", ""),
                "release_date": seg.get("release_date", ""),
                "isrcs": seg.get("isrcs", []),
                "mb_url": seg.get("mb_url", ""),
                "score": seg.get("score"),
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "start_fmt": seg["start_fmt"],
                "end_fmt": seg["end_fmt"],
                "segments": [seg["start_fmt"]],
                "musicbrainz_id": seg.get("musicbrainz_id"),
            }
            seen_tracks[track_key] = entry
            violations.append(entry)

    return {
        "total_violations": len(violations),
        "total_segments": len(audio_results),
        "flagged_segments": len(music_segments),
        "violations": violations,
    }


def _run_scan(scan_id: int, file_path: str, file_type: str):
    try:
        audio_results = []
        if file_type in ("audio", "video"):
            audio_results = scan_audio(file_path)

        risk = _compute_risk(audio_results)
        results = {
            "audio_scan": audio_results,
            "summary": _build_summary(audio_results),
        }
        update_scan(scan_id, "completed", results, risk)
    except Exception as e:
        update_scan(scan_id, "error", {"error": str(e)}, "unknown")
    finally:
        Path(file_path).unlink(missing_ok=True)


@app.post("/api/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    file_type = _classify_file(file.filename)
    if file_type == "unknown":
        raise HTTPException(400, "Unsupported file type — upload a video or audio file")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise HTTPException(413, f"File too large (max {MAX_FILE_MB}MB)")

    scan_id = create_scan(file.filename, file_type, len(content))
    save_path = UPLOAD_DIR / f"{scan_id}_{file.filename}"
    with open(save_path, "wb") as f:
        f.write(content)

    background_tasks.add_task(_run_scan, scan_id, str(save_path), file_type)
    return {"scan_id": scan_id, "filename": file.filename, "file_type": file_type, "status": "scanning"}


@app.get("/api/scan/{scan_id}")
def get_scan_result(scan_id: int):
    scan = get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return scan


@app.get("/api/history")
def get_history(limit: int = 50):
    return list_scans(limit)


class UrlRequest(BaseModel):
    url: str


@app.post("/api/upload-url")
async def upload_from_url(body: UrlRequest, background_tasks: BackgroundTasks):
    url = body.url.strip()
    if not is_valid_url(url):
        raise HTTPException(400, "Invalid URL — must start with http:// or https://")

    platform = platform_name(url)
    display_name = f"[{platform}] {url[:60]}"
    scan_id = create_scan(display_name, "video", 0)

    background_tasks.add_task(_run_url_scan, scan_id, url, platform)
    return {
        "scan_id": scan_id,
        "url": url,
        "platform": platform,
        "filename": display_name,
        "file_type": "video",
        "status": "scanning",
    }


def _run_url_scan(scan_id: int, url: str, platform: str):
    tmp_dir = tempfile.mkdtemp(prefix="copyguard_url_")
    try:
        dl = download_url(url, tmp_dir)
        if "error" in dl:
            update_scan(scan_id, "error", {"error": dl["error"]}, "unknown")
            return

        file_path = dl["path"]
        filename = dl["filename"]
        file_size = Path(file_path).stat().st_size

        from database import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE scans SET filename=?, file_size=? WHERE id=?",
                (f"[{platform}] {filename[:80]}", file_size, scan_id),
            )

        audio_results = scan_audio(file_path)
        risk = _compute_risk(audio_results)
        results = {
            "audio_scan": audio_results,
            "summary": _build_summary(audio_results),
            "source_url": url,
            "platform": platform,
            "video_title": dl.get("title", ""),
            "uploader": dl.get("uploader", ""),
        }
        update_scan(scan_id, "completed", results, risk)

    except Exception as e:
        update_scan(scan_id, "error", {"error": str(e)}, "unknown")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Batch / Channel scanning
# ---------------------------------------------------------------------------

class ChannelRequest(BaseModel):
    url: str
    max_videos: int = 50


@app.post("/api/scan-channel")
async def scan_channel(body: ChannelRequest, background_tasks: BackgroundTasks):
    url = body.url.strip()
    if not is_valid_url(url):
        raise HTTPException(400, "Invalid URL — must start with http:// or https://")

    extracted = extract_channel_videos(url, max_videos=min(body.max_videos, 100))
    if "error" in extracted:
        raise HTTPException(400, extracted["error"])

    videos = extracted["videos"]
    platform = extracted.get("platform", "Web")
    batch_id = create_batch(url, platform, len(videos))

    for video in videos:
        vid_url = video["url"]
        title = video["title"][:80]
        scan_id = create_scan_in_batch(f"[{platform}] {title}", "video", 0, batch_id, vid_url)
        background_tasks.add_task(_run_batch_video_scan, scan_id, vid_url, platform, batch_id)

    return {
        "batch_id": batch_id,
        "platform": platform,
        "total_videos": len(videos),
        "status": "running",
    }


def _run_batch_video_scan(scan_id: int, url: str, platform: str, batch_id: int):
    tmp_dir = tempfile.mkdtemp(prefix="copyguard_batch_")
    try:
        dl = download_url(url, tmp_dir)
        if "error" in dl:
            update_scan(scan_id, "error", {"error": dl["error"], "source_url": url}, "unknown")
            update_batch_progress(batch_id)
            return

        file_path = dl["path"]
        filename = dl.get("filename", "video")
        file_size = Path(file_path).stat().st_size

        from database import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE scans SET filename=?, file_size=? WHERE id=?",
                (f"[{platform}] {filename[:80]}", file_size, scan_id),
            )

        audio_results = scan_audio(file_path)
        risk = _compute_risk(audio_results)
        results = {
            "audio_scan": audio_results,
            "summary": _build_summary(audio_results),
            "source_url": url,
            "platform": platform,
            "video_title": dl.get("title", ""),
            "uploader": dl.get("uploader", ""),
        }
        update_scan(scan_id, "completed", results, risk)

    except Exception as e:
        update_scan(scan_id, "error", {"error": str(e), "source_url": url}, "unknown")
    finally:
        update_batch_progress(batch_id)
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/batch/{batch_id}")
def get_batch_result(batch_id: int):
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch


@app.get("/api/batches")
def list_all_batches(limit: int = 20):
    return list_batches(limit)


# ---------------------------------------------------------------------------
# Stolen content finder  (background job — unlimited results)
# ---------------------------------------------------------------------------

class StealSearchRequest(BaseModel):
    query: str
    official_channel: str = ""
    platform: str = "youtube"
    skip_known: bool = True
    content_type: str = "teledrama"
    extra_keywords: list[str] = []


@app.post("/api/find-stolen")
async def find_stolen(body: StealSearchRequest, background_tasks: BackgroundTasks):
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Search query is required")

    official_channel = body.official_channel.strip()
    extra_kw = [k.strip() for k in body.extra_keywords if k.strip()]
    queries = build_search_queries(query, per_query=20, content_type=body.content_type, extra_keywords=extra_kw)

    job_id = create_search_job(
        query=query,
        official_channel=official_channel,
        official_channel_id="",          # resolved in background
        platform="youtube",
        queries_total=len(queries),
        skip_known=body.skip_known,
    )

    background_tasks.add_task(
        run_search_job,
        job_id, query, official_channel, queries, body.skip_known, body.content_type,
        extra_kw,
    )

    return {
        "job_id": job_id,
        "query": query,
        "platform": "youtube",
        "queries_total": len(queries),
        "status": "running",
        "extra_keywords": extra_kw,
    }


@app.get("/api/find-stolen/{job_id}")
def get_stolen_job(job_id: int):
    job = get_search_job(job_id)
    if not job:
        raise HTTPException(404, "Search job not found")
    # Enrich each result with confirmed status stored in known_stolen_videos
    if job.get("query") and job.get("results"):
        cmap = get_confirmed_map(job["query"])
        if cmap:
            for r in job["results"]:
                vid = r.get("video_id")
                if vid and vid in cmap:
                    r["confirmed"] = cmap[vid]
    return job


@app.get("/api/find-stolen")
def list_stolen_jobs(limit: int = 20):
    return list_search_jobs(limit)


@app.get("/api/known-stolen/{show_name}")
def known_stolen_for_show(show_name: str):
    """All stolen videos ever found for a show across all past searches."""
    return {
        "show_name": show_name,
        "stats": get_known_show_stats(show_name),
        "videos": get_all_known_stolen(show_name),
    }


@app.get("/api/monitored-shows")
def monitored_shows():
    return list_monitored_shows()


class ConfirmRequest(BaseModel):
    confirmed: bool | None = None   # True=confirm stolen, False=dismiss, None=reset


class ChannelScanRequest(BaseModel):
    channel_url: str
    query: str
    official_channel: str = ""


@app.post("/api/scan-stolen-channel")
async def scan_stolen_channel(body: ChannelScanRequest, background_tasks: BackgroundTasks):
    """Directly scan a specific channel's RSS feed and find videos related to a show."""
    from content_finder import resolve_channel_id, scrape_channel_rss
    from database import create_search_job, append_search_results, get_known_video_ids, save_known_videos

    channel_url = body.channel_url.strip()
    query = body.query.strip()
    if not channel_url or not query:
        raise HTTPException(400, "channel_url and query are required")

    def _run():
        official_ids: set = set()
        if body.official_channel:
            for raw_url in [u.strip() for u in body.official_channel.split(",") if u.strip()]:
                cid = resolve_channel_id(raw_url)
                if cid:
                    official_ids.add(cid)

        target_id = resolve_channel_id(channel_url)
        if not target_id:
            return

        rss_results = scrape_channel_rss(target_id, official_ids, query)
        already = get_known_video_ids(query)
        new_results = [r for r in rss_results if r.get("video_id") not in already]
        save_known_videos(query, new_results)
        append_search_results(job_id, new_results, is_incremental=False)

    job_id = create_search_job(
        query=query,
        official_channel=body.official_channel,
        official_channel_id="",
        platform="youtube",
        queries_total=1,
        skip_known=False,
    )
    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/all-reviewed")
def all_reviewed_videos():
    """All manually reviewed videos (stolen + not-stolen) across every show, grouped."""
    summary = get_review_summary()
    total_stolen = sum(s["stolen_count"] for s in summary)
    total_not_stolen = sum(s["not_stolen_count"] for s in summary)
    videos = get_all_reviewed()
    # Group by show
    from collections import defaultdict
    grouped = defaultdict(lambda: {"stolen": [], "not_stolen": []})
    for v in videos:
        key = "stolen" if v["confirmed"] == 1 else "not_stolen"
        grouped[v["show_name"]][key].append(v)
    shows = []
    for s in summary:
        sn = s["show_name"]
        shows.append({
            "show_name": sn,
            "stolen_count": s["stolen_count"],
            "not_stolen_count": s["not_stolen_count"],
            "total_reviewed": s["total_reviewed"],
            "last_reviewed": s["last_reviewed"],
            "stolen": grouped[sn]["stolen"],
            "not_stolen": grouped[sn]["not_stolen"],
        })
    return {
        "total_stolen": total_stolen,
        "total_not_stolen": total_not_stolen,
        "total_reviewed": total_stolen + total_not_stolen,
        "shows": shows,
    }


@app.get("/api/confirmed-stolen")
def all_confirmed_stolen():
    """All manually confirmed stolen videos across every show, grouped by show."""
    shows = list_confirmed_shows()
    result = []
    for s in shows:
        videos = get_confirmed_stolen(s["show_name"])
        result.append({
            "show_name": s["show_name"],
            "confirmed_count": s["confirmed_count"],
            "last_confirmed": s["last_confirmed"],
            "videos": videos,
        })
    return result


@app.get("/api/confirmed-stolen/{show_name}")
def confirmed_stolen_for_show(show_name: str):
    """All confirmed stolen videos for a specific show."""
    videos = get_confirmed_stolen(show_name)
    return {
        "show_name": show_name,
        "confirmed_count": len(videos),
        "videos": videos,
    }


@app.get("/api/known-stolen-all/{show_name}")
def all_known_for_show(show_name: str):
    """All tracked videos for a show (stolen + official + unreviewed), newest first."""
    videos = get_all_known_for_show(show_name)
    return {"show_name": show_name, "total": len(videos), "videos": videos}


@app.delete("/api/known-stolen/{show_name}/{video_id}")
def remove_known_video(show_name: str, video_id: str):
    """Remove a single video from the known list so it resurfaces on the next scan."""
    delete_known_video(show_name, video_id)
    return {"ok": True, "video_id": video_id}


@app.delete("/api/known-stolen/{show_name}")
def reset_known_for_show(show_name: str):
    """Clear ALL tracked videos for a show — next scan will be a completely fresh start."""
    clear_known_videos(show_name)
    return {"ok": True, "show_name": show_name}


class ReportStolenRequest(BaseModel):
    url: str
    show_name: str


@app.post("/api/report-stolen")
async def report_stolen_url(body: ReportStolenRequest, background_tasks: BackgroundTasks):
    """User manually reports a stolen video URL.
    Fetches metadata via oEmbed, saves as confirmed stolen, then scans that channel."""
    import re, urllib.request
    url = body.url.strip().split("&t=")[0]  # strip timestamp
    show_name = body.show_name.strip()
    if not show_name:
        raise HTTPException(400, "show_name is required")

    # Extract video ID
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})|youtu\.be/([A-Za-z0-9_-]{11})|shorts/([A-Za-z0-9_-]{11})", url)
    if not m:
        raise HTTPException(400, "Could not extract video ID from URL")
    video_id = m.group(1) or m.group(2) or m.group(3)
    clean_url = f"https://www.youtube.com/watch?v={video_id}"

    # Fetch metadata via oEmbed (no API key needed)
    meta = {"title": "", "channel": "", "channel_url": "", "channel_id": ""}
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={clean_url}&format=json"
        with urllib.request.urlopen(oembed_url, timeout=10) as r:
            import json as _json
            d = _json.loads(r.read())
            meta["title"] = d.get("title", "")
            meta["channel"] = d.get("author_name", "")
            meta["channel_url"] = d.get("author_url", "")
    except Exception:
        pass

    # Also try to get channel_id from the watch page
    try:
        from content_finder import _curl_get
        import re as _re
        html = _curl_get(clean_url, timeout=12)
        cid_m = _re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', html)
        ch_m  = _re.search(r'"ownerChannelName"\s*:\s*"([^"]+)"', html)
        if cid_m:
            meta["channel_id"] = cid_m.group(1)
        if ch_m and not meta["channel"]:
            meta["channel"] = ch_m.group(1)
    except Exception:
        pass

    # Save as confirmed stolen
    from database import save_known_videos, set_video_confirmed
    video_record = {
        "video_id": video_id,
        "title": meta["title"],
        "channel": meta["channel"],
        "channel_id": meta["channel_id"],
        "channel_url": meta["channel_url"],
        "url": clean_url,
        "stolen": True,
        "source": "manual_report",
        "platform": "YouTube",
    }
    save_known_videos(show_name, [video_record])
    set_video_confirmed(show_name, video_id, True)

    # If we have a channel ID, scan their entire channel in background
    channel_scan_job_id = None
    if meta["channel_id"] or meta["channel_url"]:
        from content_finder import build_search_queries, run_search_job
        from database import get_known_video_ids
        scan_channel_url = meta["channel_url"] or ""
        # Build a small job just for the channel RSS scan
        queries = build_search_queries(show_name, per_query=20, content_type="teledrama")
        from database import create_search_job
        job_id = create_search_job(
            query=show_name,
            official_channel="",
            official_channel_id="",
            platform="youtube",
            queries_total=1,
            skip_known=False,
        )
        channel_scan_job_id = job_id

        def _scan_channel():
            from content_finder import resolve_channel_id, scrape_channel_rss
            from database import append_search_results, get_known_video_ids, save_known_videos, get_search_job, get_conn
            cid = meta["channel_id"] or resolve_channel_id(scan_channel_url)
            if not cid:
                with get_conn() as conn:
                    conn.execute("UPDATE search_jobs SET status='completed' WHERE id=?", (job_id,))
                return
            rss = scrape_channel_rss(cid, set(), show_name)
            already = get_known_video_ids(show_name)
            new_r = [r for r in rss if r.get("video_id") not in already]
            for r in new_r:
                r["stolen"] = True
            save_known_videos(show_name, new_r)
            append_search_results(job_id, new_r, is_incremental=False)

        background_tasks.add_task(_scan_channel)

    return {
        "ok": True,
        "video_id": video_id,
        "title": meta["title"],
        "channel": meta["channel"],
        "channel_id": meta["channel_id"],
        "channel_scan_job_id": channel_scan_job_id,
    }


@app.delete("/api/database/clear-all")
def clear_entire_database():
    """Wipe all tracked videos and search jobs across every show."""
    deleted = clear_all_known_videos()
    return {"ok": True, "deleted_videos": deleted}


@app.patch("/api/known-stolen/{show_name}/{video_id}")
def set_confirmation(show_name: str, video_id: str, body: ConfirmRequest):
    """Manually confirm or dismiss a suspected stolen video."""
    set_video_confirmed(show_name, video_id, body.confirmed)
    return {"ok": True, "video_id": video_id, "confirmed": body.confirmed}

