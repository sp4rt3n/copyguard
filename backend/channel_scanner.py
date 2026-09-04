import re
from url_downloader import platform_name


def extract_channel_videos(channel_url: str, max_videos: int = 50) -> dict:
    """
    Use yt-dlp flat-playlist extraction to list all video URLs from a
    YouTube channel, playlist, or Facebook page.

    Returns { platform, videos: [{url, title}], error? }
    """
    try:
        import yt_dlp
    except ImportError:
        return {"error": "yt-dlp not installed"}

    platform = platform_name(channel_url)

    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "playlist_items": f"1-{max_videos}",
        "ignoreerrors": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if not info:
                return {"error": "Could not extract channel info. Check the URL."}

            entries = info.get("entries") or []
            # Flatten one level (channel → playlist → videos)
            flat = []
            for e in entries:
                if e is None:
                    continue
                if e.get("_type") == "playlist":
                    flat.extend(e.get("entries") or [])
                else:
                    flat.append(e)

            videos = []
            for entry in flat[:max_videos]:
                if not entry:
                    continue
                url = (
                    entry.get("webpage_url")
                    or entry.get("url")
                    or entry.get("id")
                )
                if not url:
                    continue
                # For YouTube, reconstruct a proper watch URL from ID if needed
                vid_id = entry.get("id", "")
                if "youtube" in channel_url.lower() and vid_id and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                title = entry.get("title") or entry.get("id") or "Unknown"
                videos.append({"url": url, "title": title})

            if not videos:
                return {"error": "No videos found at this URL. Make sure it's a public channel or playlist."}

            return {"platform": platform, "videos": videos}

    except Exception as e:
        err = str(e)
        if "Private" in err or "login" in err.lower():
            return {"error": "This channel is private or requires login."}
        return {"error": f"Extraction failed: {err[:300]}"}
