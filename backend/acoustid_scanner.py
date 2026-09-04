import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ACOUSTID_KEY = os.getenv("ACOUSTID_API_KEY", "8XaBELgH")
ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MB_URL = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "CopyGuard/1.0 (copyright-checker)"}

SEGMENT_DURATION = 30   # seconds per window
MAX_SEGMENTS = 24       # covers up to 12 min at 30s steps; 24 min at 60s steps


def get_duration(file_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=15
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _extract_clip(file_path: str, start_sec: int, duration: int = 30) -> str | None:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = [
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", file_path,
        "-t", str(duration), "-ar", "44100", "-ac", "1", "-f", "wav", tmp.name
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and Path(tmp.name).stat().st_size > 2000:
            return tmp.name
    except Exception:
        pass
    Path(tmp.name).unlink(missing_ok=True)
    return None


def _fingerprint(wav_path: str) -> tuple[str | None, int]:
    try:
        r = subprocess.run(["fpcalc", "-json", wav_path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None, 0
        data = json.loads(r.stdout)
        return data.get("fingerprint"), int(data.get("duration", 0))
    except FileNotFoundError:
        raise RuntimeError("fpcalc not found — run: sudo apt install libchromaprint-tools")
    except Exception:
        return None, 0


def _query_acoustid(fingerprint: str, duration: int) -> list[dict]:
    try:
        resp = requests.post(
            ACOUSTID_URL,
            data={
                "client": ACOUSTID_KEY,
                "fingerprint": fingerprint,
                "duration": duration,
                "meta": "recordings+releasegroups+compress",
            },
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return []

    if data.get("status") != "ok":
        return []

    hits = []
    for result in data.get("results", []):
        score = result.get("score", 0)
        if score < 0.5:
            continue
        for rec in result.get("recordings", []):
            artists = [a.get("name", "") for a in rec.get("artists", [])]
            releases = rec.get("releasegroups", [])
            hits.append({
                "score": round(score * 100),
                "title": rec.get("title", "Unknown"),
                "artists": artists,
                "album": releases[0].get("title", "") if releases else "",
                "acoustid_id": result.get("id"),
                "musicbrainz_id": rec.get("id"),
            })
    return hits


def _fetch_mb_details(mbid: str) -> dict:
    """Fetch label, ISRC, release date, and rights info from MusicBrainz."""
    try:
        resp = requests.get(
            f"{MB_URL}/recording/{mbid}",
            params={"inc": "labels+releases+artist-credits+isrcs", "fmt": "json"},
            headers=MB_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()

        # Extract ISRCs
        isrcs = data.get("isrcs", [])

        # Extract label + release date from first release
        label_name = ""
        release_date = ""
        releases = data.get("releases", [])
        if releases:
            r0 = releases[0]
            release_date = r0.get("date", "")
            label_info = r0.get("label-info", [])
            if label_info:
                label = label_info[0].get("label") or {}
                label_name = label.get("name", "")

        return {
            "isrcs": isrcs,
            "label": label_name,
            "release_date": release_date,
            "mb_url": f"https://musicbrainz.org/recording/{mbid}",
        }
    except Exception:
        return {}


def _fmt_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def scan_audio(file_path: str) -> list[dict]:
    """
    Scan audio/video in 30-second segments.
    Returns one result dict per segment with exact timestamps.
    """
    total_duration = get_duration(file_path)
    if total_duration <= 0:
        return [{"detected": False, "error": "Could not read file duration"}]

    # Widen step for long files to stay within MAX_SEGMENTS
    step = SEGMENT_DURATION
    if total_duration > MAX_SEGMENTS * step:
        step = int(total_duration / MAX_SEGMENTS)

    segments = []
    mb_cache = {}  # avoid re-fetching same recording

    start = 0
    while start < total_duration and len(segments) < MAX_SEGMENTS:
        end = min(start + SEGMENT_DURATION, total_duration)

        wav = _extract_clip(file_path, start_sec=start, duration=SEGMENT_DURATION)
        if not wav:
            segments.append({
                "start_sec": start, "end_sec": int(end),
                "start_fmt": _fmt_time(start), "end_fmt": _fmt_time(int(end)),
                "detected": False, "error": "Could not extract audio",
            })
            start += step
            continue

        try:
            fp, dur = _fingerprint(wav)
        finally:
            Path(wav).unlink(missing_ok=True)

        if not fp:
            segments.append({
                "start_sec": start, "end_sec": int(end),
                "start_fmt": _fmt_time(start), "end_fmt": _fmt_time(int(end)),
                "detected": False, "error": "Fingerprint failed",
            })
            start += step
            continue

        hits = _query_acoustid(fp, dur)

        if hits:
            # Enrich top hit with MusicBrainz details
            top = hits[0]
            mbid = top.get("musicbrainz_id")
            if mbid and mbid not in mb_cache:
                time.sleep(0.5)  # MusicBrainz rate limit: 1 req/sec
                mb_cache[mbid] = _fetch_mb_details(mbid)
            mb = mb_cache.get(mbid, {}) if mbid else {}

            segments.append({
                "start_sec": start,
                "end_sec": int(end),
                "start_fmt": _fmt_time(start),
                "end_fmt": _fmt_time(int(end)),
                "detected": True,
                "title": top["title"],
                "artists": top["artists"],
                "album": top["album"],
                "score": top["score"],
                "label": mb.get("label", ""),
                "release_date": mb.get("release_date", ""),
                "isrcs": mb.get("isrcs", []),
                "mb_url": mb.get("mb_url", ""),
                "acoustid_id": top.get("acoustid_id"),
                "musicbrainz_id": mbid,
                "all_hits": hits,
            })
        else:
            segments.append({
                "start_sec": start, "end_sec": int(end),
                "start_fmt": _fmt_time(start), "end_fmt": _fmt_time(int(end)),
                "detected": False,
            })

        start += step
        time.sleep(0.35)  # AcoustID: 3 req/sec max

    return segments
