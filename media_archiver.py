"""
Space Media Archiver
- Качает public domain / CC0 материалы из NASA + Internet Archive + LOC + Wikimedia
- Параллельная загрузка по темам
- Структурированные папки + metadata.json по каждому источнику
- Жёсткий фильтр по лицензиям (только PD / CC0 / NoKnownCopyright)
- Сохраняет credit/url для апелляций по Content ID
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote_plus

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("archiver")

DEFAULT_TIMEOUT = 60
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2

# Можно переопределить переменной окружения NASA_API_KEY
NASA_API_KEY = os.environ.get("NASA_API_KEY", "1DoBGOx3c5tlxnsTf87SGh3spaAOYTegLH65m5XG")

ALLOWED_LICENSES = {
    "public domain",
    "publicdomain",
    "pd",
    "cc0",
    "creative commons cc0",
    "no known copyright",
    "no known restrictions",
    "us government work",
    "nasa",
}

_metadata_lock = threading.Lock()


class Control:
    """Pause / stop controller passed through fetch jobs."""

    def __init__(self):
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    def pause(self):
        self._pause.set()
        self._resume.clear()

    def resume(self):
        self._pause.clear()
        self._resume.set()

    def stop(self):
        self._stop.set()
        self._resume.set()  # unblock anyone waiting

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def wait_if_paused(self):
        if self._pause.is_set() and not self._stop.is_set():
            self._resume.wait()


_NULL_CONTROL = Control()


def _safe_name(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE)
    return text.strip("_").lower()[:80] or "untitled"


def _http_get(url, params=None, stream=False):
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(url, params=params, stream=stream, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            wait = RETRY_BACKOFF ** attempt
            logger.warning("GET %s failed (%s), retry in %ds", url, e, wait)
            time.sleep(wait)
    raise last_err


def _download_file(url: str, dest_path: str) -> bool:
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True
    tmp_path = dest_path + ".part"
    try:
        with _http_get(url, stream=True) as r:
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 15):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        logger.error("Download failed %s: %s", url, e)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def _append_metadata(folder: str, entry: dict) -> None:
    meta_path = os.path.join(folder, "metadata.json")
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append(entry)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def _license_ok(license_text: str | None) -> bool:
    if not license_text:
        return False
    s = license_text.lower().strip()
    return any(k in s for k in ALLOWED_LICENSES)


# ─── NASA ────────────────────────────────────────────────────────────────────

NASA_SEARCH_URL = "https://images-api.nasa.gov/search"


def fetch_nasa(query: str, media_type: str, limit: int, root: str, control: Control = _NULL_CONTROL) -> int:
    folder = os.path.join(root, "nasa", media_type, _safe_name(query))
    os.makedirs(folder, exist_ok=True)

    r = _http_get(NASA_SEARCH_URL, params={"q": query, "media_type": media_type, "api_key": NASA_API_KEY})
    items = r.json().get("collection", {}).get("items", [])[:limit]

    saved = 0
    for item in items:
        control.wait_if_paused()
        if control.is_stopped():
            break
        try:
            data = (item.get("data") or [{}])[0]
            nasa_id = data.get("nasa_id") or "unknown"
            asset_resp = _http_get(item["href"]).json()
            assets = asset_resp if isinstance(asset_resp, list) else []

            target = None
            for url in assets:
                low = url.lower()
                if media_type == "image" and ("orig" in low or low.endswith((".jpg", ".png", ".tif"))):
                    target = url
                    break
                if media_type == "video" and (low.endswith(".mp4") and "orig" in low):
                    target = url
                    break
                if media_type == "audio" and low.endswith((".mp3", ".wav", ".m4a")):
                    target = url
                    break
            if not target and assets:
                target = assets[0]
            if not target:
                continue

            ext = target.rsplit(".", 1)[-1].split("?")[0]
            filename = f"{_safe_name(nasa_id)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(target, dest):
                _append_metadata(folder, {
                    "filename": filename,
                    "source": "NASA Image and Video Library",
                    "id": nasa_id,
                    "title": data.get("title"),
                    "description": data.get("description"),
                    "date_created": data.get("date_created"),
                    "license": "Public Domain (NASA)",
                    "credit": data.get("photographer") or "NASA",
                    "url": target,
                    "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("NASA item failed: %s", e)
    logger.info("NASA %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Internet Archive ────────────────────────────────────────────────────────

IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_META_URL = "https://archive.org/metadata/{identifier}"
IA_DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"


def fetch_internet_archive(query: str, media_type: str, limit: int, root: str, control: Control = _NULL_CONTROL) -> int:
    folder = os.path.join(root, "internet_archive", media_type, _safe_name(query))
    os.makedirs(folder, exist_ok=True)

    ia_type = {"video": "movies", "audio": "audio", "image": "image"}.get(media_type, "movies")
    params = {
        "q": f'{query} AND mediatype:{ia_type}',
        "fl[]": ["identifier", "title", "creator", "licenseurl", "rights", "date"],
        "rows": limit,
        "output": "json",
    }
    r = _http_get(IA_SEARCH_URL, params=params)
    docs = r.json().get("response", {}).get("docs", [])

    saved = 0
    for doc in docs:
        control.wait_if_paused()
        if control.is_stopped():
            break
        ident = doc.get("identifier")
        if not ident:
            continue
        license_field = doc.get("licenseurl") or doc.get("rights") or ""
        # Internet Archive часто помечает PD через licenseurl creativecommons.org/publicdomain
        # либо через rights="public domain". Если нет явного PD/CC0 — пропускаем.
        if not _license_ok(license_field) and "publicdomain" not in license_field.lower() and "cc0" not in license_field.lower():
            continue
        try:
            meta = _http_get(IA_META_URL.format(identifier=ident)).json()
            files = meta.get("files", [])
            wanted_ext = {
                "video": (".mp4", ".mov", ".webm", ".mkv"),
                "audio": (".mp3", ".ogg", ".flac", ".wav"),
                "image": (".jpg", ".jpeg", ".png", ".tif"),
            }[media_type]

            picked = None
            for f in files:
                name = f.get("name", "").lower()
                if name.endswith(wanted_ext) and f.get("source") == "original":
                    picked = f
                    break
            if not picked:
                for f in files:
                    name = f.get("name", "").lower()
                    if name.endswith(wanted_ext):
                        picked = f
                        break
            if not picked:
                continue

            file_url = IA_DOWNLOAD_URL.format(identifier=ident, filename=quote_plus(picked["name"]))
            ext = picked["name"].rsplit(".", 1)[-1]
            filename = f"{_safe_name(ident)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(file_url, dest):
                _append_metadata(folder, {
                    "filename": filename,
                    "source": "Internet Archive",
                    "id": ident,
                    "title": doc.get("title"),
                    "creator": doc.get("creator"),
                    "date": doc.get("date"),
                    "license": license_field or "Public Domain",
                    "credit": doc.get("creator") or "Unknown",
                    "url": f"https://archive.org/details/{ident}",
                    "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("IA item %s failed: %s", ident, e)
    logger.info("Internet Archive %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Library of Congress ─────────────────────────────────────────────────────

LOC_SEARCH_URL = "https://www.loc.gov/search/"


def fetch_loc(query: str, media_type: str, limit: int, root: str, control: Control = _NULL_CONTROL) -> int:
    folder = os.path.join(root, "loc", media_type, _safe_name(query))
    os.makedirs(folder, exist_ok=True)

    fa_format = {"image": "online-format:image", "video": "online-format:film, video", "audio": "online-format:audio"}.get(media_type, "online-format:image")
    params = {"q": query, "fo": "json", "fa": fa_format, "c": limit}
    r = _http_get(LOC_SEARCH_URL, params=params)
    results = r.json().get("results", [])[:limit]

    saved = 0
    for item in results:
        control.wait_if_paused()
        if control.is_stopped():
            break
        rights = (item.get("rights") or "") + " " + (item.get("rights_advisory") or [""])[0] if isinstance(item.get("rights_advisory"), list) else (item.get("rights") or "")
        if not (_license_ok(rights) or "no known restrictions" in rights.lower()):
            continue
        try:
            target = None
            if media_type == "image":
                imgs = item.get("image_url") or []
                if isinstance(imgs, list) and imgs:
                    target = imgs[-1]
            else:
                resources = item.get("resources") or []
                for res in resources:
                    for key in ("video", "audio", "url"):
                        if res.get(key):
                            target = res[key]
                            break
                    if target:
                        break
            if not target:
                continue
            target = target if target.startswith("http") else "https:" + target
            ext = target.rsplit(".", 1)[-1].split("?")[0]
            if len(ext) > 5:
                ext = "jpg" if media_type == "image" else "mp4"
            ident = item.get("id") or item.get("url", "")
            filename = f"{_safe_name(ident)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(target, dest):
                _append_metadata(folder, {
                    "filename": filename,
                    "source": "Library of Congress",
                    "id": ident,
                    "title": item.get("title"),
                    "date": item.get("date"),
                    "license": rights or "No known restrictions",
                    "credit": "Library of Congress",
                    "url": item.get("url"),
                    "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("LOC item failed: %s", e)
    logger.info("LOC %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Wikimedia Commons ───────────────────────────────────────────────────────

WM_API = "https://commons.wikimedia.org/w/api.php"


def fetch_wikimedia(query: str, media_type: str, limit: int, root: str, control: Control = _NULL_CONTROL) -> int:
    folder = os.path.join(root, "wikimedia", media_type, _safe_name(query))
    os.makedirs(folder, exist_ok=True)

    mime_filter = {"image": "filew:bitmap|drawing", "video": "filew:video", "audio": "filew:audio"}.get(media_type)

    search_params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": f"{query} filetype:{ 'video' if media_type=='video' else ('audio' if media_type=='audio' else 'bitmap') }",
        "srnamespace": 6,
        "srlimit": limit,
    }
    r = _http_get(WM_API, params=search_params)
    hits = r.json().get("query", {}).get("search", [])

    saved = 0
    for hit in hits:
        control.wait_if_paused()
        if control.is_stopped():
            break
        title = hit.get("title")
        if not title:
            continue
        try:
            info_params = {
                "action": "query",
                "format": "json",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|mime",
            }
            info = _http_get(WM_API, params=info_params).json()
            pages = info.get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            ii = (page.get("imageinfo") or [{}])[0]
            ext_meta = ii.get("extmetadata", {})
            license_short = (ext_meta.get("LicenseShortName", {}) or {}).get("value", "")
            artist = (ext_meta.get("Artist", {}) or {}).get("value", "Unknown")
            artist = re.sub(r"<[^>]+>", "", artist).strip()

            if not (_license_ok(license_short) or "public domain" in license_short.lower() or "cc0" in license_short.lower()):
                continue

            file_url = ii.get("url")
            if not file_url:
                continue
            ext = file_url.rsplit(".", 1)[-1].split("?")[0]
            filename = f"{_safe_name(title)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(file_url, dest):
                _append_metadata(folder, {
                    "filename": filename,
                    "source": "Wikimedia Commons",
                    "id": title,
                    "license": license_short,
                    "credit": artist,
                    "url": f"https://commons.wikimedia.org/wiki/{quote_plus(title)}",
                    "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("Wikimedia item %s failed: %s", title, e)
    logger.info("Wikimedia %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Orchestrator ────────────────────────────────────────────────────────────

SOURCES = {
    "nasa": fetch_nasa,
    "internet_archive": fetch_internet_archive,
    "loc": fetch_loc,
    "wikimedia": fetch_wikimedia,
}


def run(topics, media_types, sources, limit, root, workers, control: Control = None, on_progress=None):
    if control is None:
        control = Control()
    os.makedirs(root, exist_ok=True)
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for topic in topics:
            for mt in media_types:
                for src in sources:
                    fn = SOURCES[src]
                    jobs.append(ex.submit(fn, topic, mt, limit, root, control))
        total = 0
        for fut in as_completed(jobs):
            try:
                got = fut.result() or 0
                total += got
                if on_progress:
                    on_progress(total)
            except Exception as e:
                logger.error("Job failed: %s", e)
    logger.info("DONE. Total saved: %d -> %s", total, root)
    return total


def parse_args():
    p = argparse.ArgumentParser(description="Public-domain space media archiver")
    p.add_argument("--topics", nargs="+", required=True, help="Search topics, e.g. 'apollo 13' 'voyager'")
    p.add_argument("--media", nargs="+", default=["image", "video"], choices=["image", "video", "audio"])
    p.add_argument("--sources", nargs="+", default=list(SOURCES.keys()), choices=list(SOURCES.keys()))
    p.add_argument("--limit", type=int, default=50, help="Items per topic per source")
    p.add_argument("--out", default="archive_db", help="Output root folder")
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.topics, args.media, args.sources, args.limit, args.out, args.workers)
