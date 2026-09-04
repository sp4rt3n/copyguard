import os
import re
import tempfile
from pathlib import Path


def is_valid_url(url: str) -> bool:
    return bool(re.match(r'https?://', url.strip()))


def platform_name(url: str) -> str:
    url = url.lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    if "facebook.com" in url or "fb.watch" in url:
        return "Facebook"
    if "instagram.com" in url:
        return "Instagram"
    if "tiktok.com" in url:
        return "TikTok"
    if "twitter.com" in url or "x.com" in url:
        return "Twitter/X"
    if "vimeo.com" in url:
        return "Vimeo"
    return "Web"


def download_url(url: str, output_dir: str) -> dict:
    """
    Download media from any URL using yt-dlp.
    Returns { path, filename, platform, error }.
    """
    try:
        import yt_dlp
    except ImportError:
        return {"error": "yt-dlp not installed. Run: pip install yt-dlp"}

    platform = platform_name(url)
    out_template = os.path.join(output_dir, "%(title).60s.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 500 * 1024 * 1024,  # 500MB cap
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "download")
            ext = info.get("ext", "mp4")

            # Find the actual downloaded file
            safe_title = re.sub(r'[^\w\s\-.]', '', title)[:60].strip()
            candidates = list(Path(output_dir).glob("*.mp4")) + \
                         list(Path(output_dir).glob("*.webm")) + \
                         list(Path(output_dir).glob("*.mkv")) + \
                         list(Path(output_dir).glob("*.mp3")) + \
                         list(Path(output_dir).glob("*.m4a"))

            if not candidates:
                return {"error": "Download completed but file not found"}

            # Pick the newest file
            file_path = max(candidates, key=lambda p: p.stat().st_mtime)
            return {
                "path": str(file_path),
                "filename": f"{title[:80]}.{file_path.suffix.lstrip('.')}",
                "platform": platform,
                "title": title,
                "duration": info.get("duration"),
                "uploader": info.get("uploader", ""),
            }

    except Exception as e:
        err = str(e)
        if "Private video" in err or "Sign in" in err:
            return {"error": "This video is private or requires login"}
        if "not available" in err.lower():
            return {"error": "Video not available or region-locked"}
        return {"error": f"Download failed: {err[:200]}"}
