"""
Space Media Archiver — всё в одном файле
Запуск GUI:        python archiver.py
Запуск CLI:        python archiver.py --topics "apollo 13" --limit 200
Источники:         NASA + Internet Archive + Library of Congress + Wikimedia
Лицензии:          только Public Domain / CC0 / No-Known-Restrictions
"""

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote, quote_plus

import requests

# ─── Конфигурация ────────────────────────────────────────────────────────────

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

# Wikimedia требует осмысленный User-Agent, иначе 403
USER_AGENT = "SpaceMediaArchiver/1.0 (educational; contact: archiver@example.com)"
HEADERS = {"User-Agent": USER_AGENT}

ALLOWED_LICENSES = {
    "public domain", "publicdomain", "pd",
    "cc0", "creative commons cc0",
    "no known copyright", "no known restrictions",
    "us government work", "nasa",
}

_metadata_lock = threading.Lock()


# ─── Контроллер паузы/остановки ──────────────────────────────────────────────

class Control:
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
        self._resume.set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def wait_if_paused(self):
        if self._pause.is_set() and not self._stop.is_set():
            self._resume.wait()


_NULL_CONTROL = Control()


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def _safe_name(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE)
    return text.strip("_").lower()[:80] or "untitled"


def _http_get(url, params=None, stream=False):
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(url, params=params, headers=HEADERS, stream=stream, timeout=DEFAULT_TIMEOUT)
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


def _license_ok(license_text) -> bool:
    if not license_text:
        return False
    s = license_text.lower().strip()
    return any(k in s for k in ALLOWED_LICENSES)


# ─── NASA ────────────────────────────────────────────────────────────────────

NASA_SEARCH_URL = "https://images-api.nasa.gov/search"


def fetch_nasa(query, media_type, limit, root, control=_NULL_CONTROL):
    folder = root
    os.makedirs(folder, exist_ok=True)

    # NASA images-api не требует api_key (это открытый endpoint, ключ нужен только для api.nasa.gov)
    r = _http_get(NASA_SEARCH_URL, params={"q": query, "media_type": media_type})
    items = r.json().get("collection", {}).get("items", [])[:limit]

    saved = 0
    for item in items:
        control.wait_if_paused()
        if control.is_stopped():
            break
        try:
            data = (item.get("data") or [{}])[0]
            nasa_id = data.get("nasa_id") or "unknown"
            assets = _http_get(item["href"]).json()
            assets = assets if isinstance(assets, list) else []

            target = None
            for url in assets:
                low = url.lower()
                if media_type == "image" and ("orig" in low or low.endswith((".jpg", ".png", ".tif"))):
                    target = url; break
                if media_type == "video" and (low.endswith(".mp4") and "orig" in low):
                    target = url; break
                if media_type == "audio" and low.endswith((".mp3", ".wav", ".m4a")):
                    target = url; break
            if not target and assets:
                target = assets[0]
            if not target:
                continue

            ext = target.rsplit(".", 1)[-1].split("?")[0]
            filename = f"nasa_{_safe_name(nasa_id)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(target, dest):
                _append_metadata(folder, {
                    "filename": filename, "source": "NASA Image and Video Library",
                    "id": nasa_id, "title": data.get("title"),
                    "description": data.get("description"), "date_created": data.get("date_created"),
                    "license": "Public Domain (NASA)", "credit": data.get("photographer") or "NASA",
                    "url": target, "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
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


def fetch_internet_archive(query, media_type, limit, root, control=_NULL_CONTROL):
    folder = root
    os.makedirs(folder, exist_ok=True)

    ia_type = {"video": "movies", "audio": "audio", "image": "image"}.get(media_type, "movies")
    params = {
        "q": f'{query} AND mediatype:{ia_type}',
        "fl[]": ["identifier", "title", "creator", "licenseurl", "rights", "date"],
        "rows": limit, "output": "json",
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
                    picked = f; break
            if not picked:
                for f in files:
                    if f.get("name", "").lower().endswith(wanted_ext):
                        picked = f; break
            if not picked:
                continue

            file_url = IA_DOWNLOAD_URL.format(identifier=ident, filename=quote(picked["name"]))
            ext = picked["name"].rsplit(".", 1)[-1]
            filename = f"ia_{_safe_name(ident)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(file_url, dest):
                _append_metadata(folder, {
                    "filename": filename, "source": "Internet Archive", "id": ident,
                    "title": doc.get("title"), "creator": doc.get("creator"), "date": doc.get("date"),
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


def fetch_loc(query, media_type, limit, root, control=_NULL_CONTROL):
    folder = root
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
                for res in (item.get("resources") or []):
                    for key in ("video", "audio", "url"):
                        if res.get(key):
                            target = res[key]; break
                    if target:
                        break
            if not target:
                continue
            target = target if target.startswith("http") else "https:" + target
            ext = target.rsplit(".", 1)[-1].split("?")[0]
            if len(ext) > 5:
                ext = "jpg" if media_type == "image" else "mp4"
            ident = item.get("id") or item.get("url", "")
            filename = f"loc_{_safe_name(ident)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(target, dest):
                _append_metadata(folder, {
                    "filename": filename, "source": "Library of Congress", "id": ident,
                    "title": item.get("title"), "date": item.get("date"),
                    "license": rights or "No known restrictions", "credit": "Library of Congress",
                    "url": item.get("url"), "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("LOC item failed: %s", e)
    logger.info("LOC %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Wikimedia Commons ───────────────────────────────────────────────────────

WM_API = "https://commons.wikimedia.org/w/api.php"


def fetch_wikimedia(query, media_type, limit, root, control=_NULL_CONTROL):
    folder = root
    os.makedirs(folder, exist_ok=True)

    file_kind = "video" if media_type == "video" else ("audio" if media_type == "audio" else "bitmap")
    search_params = {
        "action": "query", "format": "json", "list": "search",
        "srsearch": f"{query} filetype:{file_kind}",
        "srnamespace": 6, "srlimit": limit,
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
            info = _http_get(WM_API, params={
                "action": "query", "format": "json", "titles": title,
                "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
            }).json()
            page = next(iter(info.get("query", {}).get("pages", {}).values()), {})
            ii = (page.get("imageinfo") or [{}])[0]
            ext_meta = ii.get("extmetadata", {})
            license_short = (ext_meta.get("LicenseShortName", {}) or {}).get("value", "")
            artist = re.sub(r"<[^>]+>", "", (ext_meta.get("Artist", {}) or {}).get("value", "Unknown")).strip()

            if not (_license_ok(license_short) or "public domain" in license_short.lower() or "cc0" in license_short.lower()):
                continue

            file_url = ii.get("url")
            if not file_url:
                continue
            ext = file_url.rsplit(".", 1)[-1].split("?")[0]
            filename = f"wm_{_safe_name(title)}.{ext}"
            dest = os.path.join(folder, filename)

            if _download_file(file_url, dest):
                _append_metadata(folder, {
                    "filename": filename, "source": "Wikimedia Commons", "id": title,
                    "license": license_short, "credit": artist,
                    "url": f"https://commons.wikimedia.org/wiki/{quote_plus(title)}",
                    "downloaded": datetime.utcnow().strftime("%Y-%m-%d"),
                })
                saved += 1
        except Exception as e:
            logger.error("Wikimedia item %s failed: %s", title, e)
    logger.info("Wikimedia %s '%s': %d items", media_type, query, saved)
    return saved


# ─── Оркестратор ─────────────────────────────────────────────────────────────

SOURCES = {
    "nasa": fetch_nasa,
    "internet_archive": fetch_internet_archive,
    "loc": fetch_loc,
    "wikimedia": fetch_wikimedia,
}


def run(topics, media_types, sources, limit, root, workers, control=None, on_progress=None):
    if control is None:
        control = Control()
    os.makedirs(root, exist_ok=True)
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for topic in topics:
            for mt in media_types:
                for src in sources:
                    jobs.append(ex.submit(SOURCES[src], topic, mt, limit, root, control))
        total = 0
        for fut in as_completed(jobs):
            try:
                total += fut.result() or 0
                if on_progress:
                    on_progress(total)
            except Exception as e:
                logger.error("Job failed: %s", e)
    logger.info("DONE. Total saved: %d -> %s", total, root)
    return total


# ─── GUI (tkinter) ───────────────────────────────────────────────────────────

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    class TextHandler(logging.Handler):
        def __init__(self, log_queue):
            super().__init__()
            self.log_queue = log_queue

        def emit(self, record):
            try:
                self.log_queue.put(self.format(record))
            except Exception:
                pass

    class App:
        def __init__(self, root):
            self.root = root
            root.title("Space Media Archiver")
            root.geometry("780x620")
            root.minsize(680, 540)

            self.control = None
            self.worker = None
            self.log_queue = queue.Queue()
            self.total_saved = 0

            self._build_ui()
            handler = TextHandler(self.log_queue)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
            logger.addHandler(handler)
            self._poll_log_queue()
            root.protocol("WM_DELETE_WINDOW", self.on_exit)

        def _build_ui(self):
            pad = {"padx": 8, "pady": 4}

            params = ttk.LabelFrame(self.root, text="Параметры поиска")
            params.pack(fill="x", **pad)

            ttk.Label(params, text="Ключевые слова (через запятую):").grid(row=0, column=0, sticky="w", padx=6, pady=6)
            self.keywords_var = tk.StringVar(value="apollo 13, voyager")
            ttk.Entry(params, textvariable=self.keywords_var, width=60).grid(row=0, column=1, columnspan=3, sticky="we", padx=6, pady=6)

            ttk.Label(params, text="Сколько материалов на тему:").grid(row=1, column=0, sticky="w", padx=6, pady=6)
            self.limit_var = tk.IntVar(value=200)
            ttk.Spinbox(params, from_=1, to=10000, increment=50, textvariable=self.limit_var, width=10).grid(row=1, column=1, sticky="w", padx=6, pady=6)

            ttk.Label(params, text="Тип материалов:").grid(row=1, column=2, sticky="e", padx=6, pady=6)
            self.media_var = tk.StringVar(value="both")
            mf = ttk.Frame(params); mf.grid(row=1, column=3, sticky="w", padx=6, pady=6)
            ttk.Radiobutton(mf, text="Видео", variable=self.media_var, value="video").pack(side="left")
            ttk.Radiobutton(mf, text="Картинки", variable=self.media_var, value="image").pack(side="left")
            ttk.Radiobutton(mf, text="И то, и другое", variable=self.media_var, value="both").pack(side="left")

            ttk.Label(params, text="Папка для сохранения:").grid(row=2, column=0, sticky="w", padx=6, pady=6)
            self.out_var = tk.StringVar(value=os.path.abspath("archive_db"))
            ttk.Entry(params, textvariable=self.out_var, width=50).grid(row=2, column=1, columnspan=2, sticky="we", padx=6, pady=6)
            ttk.Button(params, text="Обзор...", command=self._browse).grid(row=2, column=3, sticky="w", padx=6, pady=6)
            params.columnconfigure(1, weight=1)

            ctrl = ttk.Frame(self.root); ctrl.pack(fill="x", **pad)
            self.btn_start = ttk.Button(ctrl, text="▶ Запуск", command=self.on_start)
            self.btn_pause = ttk.Button(ctrl, text="⏸ Пауза", command=self.on_pause, state="disabled")
            self.btn_resume = ttk.Button(ctrl, text="⏵ Продолжить", command=self.on_resume, state="disabled")
            self.btn_stop = ttk.Button(ctrl, text="⏹ Стоп", command=self.on_stop, state="disabled")
            self.btn_exit = ttk.Button(ctrl, text="✕ Выход", command=self.on_exit)
            for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop):
                b.pack(side="left", padx=4, pady=4)
            self.btn_exit.pack(side="right", padx=4, pady=4)

            st = ttk.Frame(self.root); st.pack(fill="x", **pad)
            self.status_var = tk.StringVar(value="Готов к запуску")
            ttk.Label(st, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
            self.counter_var = tk.StringVar(value="Скачано: 0")
            ttk.Label(st, textvariable=self.counter_var, anchor="e").pack(side="right")

            log_frame = ttk.LabelFrame(self.root, text="Лог работы")
            log_frame.pack(fill="both", expand=True, **pad)
            theme = getattr(self.root, "_theme", {"panel": "#111", "fg": "#ddd", "accent": "#a26bff", "border": "#2a2a30"})
            self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled",
                                    bg=theme["panel"], fg=theme["fg"],
                                    insertbackground=theme["accent"],
                                    selectbackground=theme["accent"], selectforeground="#ffffff",
                                    relief="flat", borderwidth=0,
                                    highlightthickness=1, highlightbackground=theme["border"],
                                    highlightcolor=theme["accent"],
                                    font=("Consolas", 9))
            scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scroll.set)
            self.log_text.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

        def _browse(self):
            path = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
            if path:
                self.out_var.set(path)

        def _poll_log_queue(self):
            try:
                while True:
                    msg = self.log_queue.get_nowait()
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
            except queue.Empty:
                pass
            self.root.after(120, self._poll_log_queue)

        def _media_types(self):
            v = self.media_var.get()
            return ["video", "image"] if v == "both" else [v]

        def _topics(self):
            return [t.strip() for t in self.keywords_var.get().split(",") if t.strip()]

        def on_start(self):
            if self.worker and self.worker.is_alive():
                messagebox.showinfo("Уже запущено", "Загрузка уже идёт.")
                return
            topics = self._topics()
            if not topics:
                messagebox.showwarning("Нет ключевых слов", "Введите хотя бы одно ключевое слово.")
                return
            try:
                limit = int(self.limit_var.get())
            except (TypeError, tk.TclError):
                messagebox.showwarning("Неверное число", "Укажите количество материалов.")
                return

            out = self.out_var.get().strip() or "archive_db"
            media = self._media_types()

            self.control = Control()
            self.total_saved = 0
            self.counter_var.set("Скачано: 0")
            self.status_var.set("Работает...")
            self._set_buttons(running=True, paused=False)
            logger.info("=== Старт: темы=%s, лимит=%d, типы=%s, папка=%s ===", topics, limit, media, out)

            def progress(total):
                self.total_saved = total
                self.root.after(0, lambda: self.counter_var.set(f"Скачано: {total}"))

            def worker():
                try:
                    run(topics, media, list(SOURCES.keys()), limit, out, 8, self.control, progress)
                except Exception as e:
                    logger.error("Сбой: %s", e)
                finally:
                    self.root.after(0, self._on_done)

            self.worker = threading.Thread(target=worker, daemon=True)
            self.worker.start()

        def on_pause(self):
            if self.control:
                self.control.pause()
                self.status_var.set("Пауза")
                self._set_buttons(running=True, paused=True)
                logger.info("=== Пауза ===")

        def on_resume(self):
            if self.control:
                self.control.resume()
                self.status_var.set("Работает...")
                self._set_buttons(running=True, paused=False)
                logger.info("=== Продолжаем ===")

        def on_stop(self):
            if self.control:
                self.control.stop()
                self.status_var.set("Останавливается...")
                logger.info("=== Остановка по запросу ===")

        def on_exit(self):
            if self.worker and self.worker.is_alive():
                if not messagebox.askyesno("Выход", "Загрузка идёт. Прервать и выйти?"):
                    return
                if self.control:
                    self.control.stop()
            self.root.destroy()
            os._exit(0)

        def _on_done(self):
            self.status_var.set(f"Готово. Всего скачано: {self.total_saved}")
            self._set_buttons(running=False, paused=False)
            logger.info("=== Завершено. Скачано: %d ===", self.total_saved)

        def _set_buttons(self, running, paused):
            self.btn_start.configure(state="disabled" if running else "normal")
            self.btn_pause.configure(state="normal" if running and not paused else "disabled")
            self.btn_resume.configure(state="normal" if running and paused else "disabled")
            self.btn_stop.configure(state="normal" if running else "disabled")

    root = tk.Tk()
    _apply_dark_theme(root)
    App(root)
    root.mainloop()


def _apply_dark_theme(root):
    import tkinter as tk
    from tkinter import ttk

    BG = "#0f0f10"            # фон окна, почти чёрный
    PANEL = "#1a1a1d"         # панели, рамки
    PANEL_HOVER = "#242428"
    FG = "#e6e6e6"            # основной текст
    FG_MUTED = "#9a9aa3"
    ACCENT = "#a26bff"        # фиолетовый акцент
    ACCENT_HOVER = "#b886ff"
    ACCENT_DARK = "#5b3aa0"
    BORDER = "#2a2a30"
    DISABLED = "#3a3a40"

    root.configure(bg=BG)
    root.option_add("*Background", BG)
    root.option_add("*Foreground", FG)
    root.option_add("*selectBackground", ACCENT_DARK)
    root.option_add("*selectForeground", FG)

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    troughcolor=PANEL, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TLabelframe", background=BG, foreground=ACCENT, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=("Segoe UI", 10, "bold"))

    style.configure("TButton",
                    background=PANEL, foreground=FG,
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL,
                    padding=(12, 6), relief="flat")
    style.map("TButton",
              background=[("active", ACCENT), ("pressed", ACCENT_HOVER), ("disabled", PANEL)],
              foreground=[("active", "#ffffff"), ("disabled", DISABLED)],
              bordercolor=[("active", ACCENT)])

    style.configure("TEntry",
                    fieldbackground=PANEL, foreground=FG,
                    insertcolor=ACCENT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])

    style.configure("TSpinbox",
                    fieldbackground=PANEL, foreground=FG,
                    background=PANEL, bordercolor=BORDER, arrowcolor=ACCENT)
    style.map("TSpinbox", bordercolor=[("focus", ACCENT)])

    style.configure("TRadiobutton", background=BG, foreground=FG, focuscolor=BG)
    style.map("TRadiobutton",
              background=[("active", BG)],
              foreground=[("active", ACCENT)],
              indicatorcolor=[("selected", ACCENT), ("!selected", BORDER)])

    style.configure("Vertical.TScrollbar",
                    background=PANEL, troughcolor=BG, bordercolor=BG,
                    arrowcolor=FG_MUTED, lightcolor=PANEL, darkcolor=PANEL)
    style.map("Vertical.TScrollbar",
              background=[("active", ACCENT_DARK)])

    # значения для tk.Text/виджетов вне ttk
    root._theme = {
        "bg": BG, "panel": PANEL, "fg": FG, "fg_muted": FG_MUTED,
        "accent": ACCENT, "border": BORDER,
    }


# ─── CLI / Entry point ───────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Public-domain space media archiver (GUI by default)")
    p.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI window)")
    p.add_argument("--topics", nargs="+", help="Search topics, e.g. 'apollo 13' 'voyager'")
    p.add_argument("--media", nargs="+", default=["image", "video"], choices=["image", "video", "audio"])
    p.add_argument("--sources", nargs="+", default=list(SOURCES.keys()), choices=list(SOURCES.keys()))
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out", default="archive_db")
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.cli or args.topics:
        if not args.topics:
            print("Для CLI-режима укажи --topics", file=sys.stderr)
            sys.exit(2)
        run(args.topics, args.media, args.sources, args.limit, args.out, args.workers)
    else:
        launch_gui()
