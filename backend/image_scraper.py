import asyncio
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

RIGHTS_SITES = [
    "gettyimages", "shutterstock", "alamy", "corbis",
    "depositphotos", "istockphoto", "adobestock", "dreamstime",
    "stockphoto", "123rf", "bigstockphoto", "masterfile",
]


async def _scrape_tineye(page, image_path: str) -> list[dict]:
    hits = []
    try:
        await page.goto("https://tineye.com/", timeout=20000)
        upload_input = page.locator('input[type="file"]')
        await upload_input.set_input_files(image_path)
        await page.wait_for_url("**/search/**", timeout=20000)
        await page.wait_for_timeout(2000)

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        for link in soup.find_all("a", href=True):
            url = link["href"].lower()
            for site in RIGHTS_SITES:
                if site in url:
                    hits.append({"source": site, "url": link["href"], "engine": "TinEye"})
                    break

        # Also count total matches
        match_text = soup.find(string=lambda t: t and "match" in t.lower() and any(c.isdigit() for c in t))
        total = 0
        if match_text:
            import re
            nums = re.findall(r"\d+", match_text)
            total = int(nums[0]) if nums else 0

        return hits, total
    except Exception as e:
        return [], 0


async def _scrape_yandex(page, image_path: str) -> list[dict]:
    hits = []
    total = 0
    try:
        await page.goto("https://yandex.com/images/", timeout=20000)
        # Click the camera icon to open upload
        camera_btn = page.locator('button.input__btn[data-type="file"], .cbir-button, [aria-label*="image"], .cbir-panel__full-container button').first
        await camera_btn.click(timeout=5000)
        await page.wait_for_timeout(500)

        upload_input = page.locator('input[type="file"]').first
        await upload_input.set_input_files(image_path)
        await page.wait_for_url("**/search*", timeout=20000)
        await page.wait_for_timeout(2500)

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        for link in soup.find_all("a", href=True):
            url = link["href"].lower()
            for site in RIGHTS_SITES:
                if site in url:
                    hits.append({"source": site, "url": link["href"], "engine": "Yandex"})
                    break

        return hits, total
    except Exception:
        return [], 0


async def _run_image_scan(image_path: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )

        tineye_hits, tineye_total = [], 0
        yandex_hits = []

        try:
            page1 = await context.new_page()
            tineye_hits, tineye_total = await _scrape_tineye(page1, image_path)
            await page1.close()
        except Exception:
            pass

        try:
            page2 = await context.new_page()
            yandex_hits, _ = await _scrape_yandex(page2, image_path)
            await page2.close()
        except Exception:
            pass

        await browser.close()

    all_hits = tineye_hits + yandex_hits
    # Deduplicate by source
    seen = set()
    unique_hits = []
    for h in all_hits:
        k = h.get("source")
        if k not in seen:
            seen.add(k)
            unique_hits.append(h)

    return {
        "detected": len(unique_hits) > 0,
        "type": "image",
        "copyright_sources": unique_hits,
        "total_matching_pages": tineye_total,
        "engines_checked": ["TinEye", "Yandex Images"],
    }


def scan_image(image_path: str) -> dict:
    """Scrape TinEye and Yandex reverse image search for copyright hits."""
    try:
        return asyncio.run(_run_image_scan(image_path))
    except Exception as e:
        return {"detected": False, "error": str(e), "type": "image"}


def extract_thumbnail(video_path: str, out_path: str) -> bool:
    """Extract the first frame from a video."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False
