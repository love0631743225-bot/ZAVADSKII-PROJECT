"""
YouTube Mass Uploader v2.0
- Читает задачи из Google Sheets
- Скачивает видео с Google Drive
- Поддерживает несколько Google аккаунтов в одном профиле AdsPower
- Поддерживает каналы бренда (channel_id)
- Загружает как PRIVATE
- Публикует по расписанию (upload_time)
- Пишет статус в Sheets
- Удаляет видео после загрузки
"""

import requests
import time
import json
import logging
import os
import sys
from datetime import datetime
from dataclasses import dataclass, field
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

import sys
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(r'C:\Users\BOT\Desktop\upload_log.txt', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
ADSPOWER_BASE_URL = "http://local.adspower.net:50326"
ADSPOWER_API_KEY  = "a1826249fb936686a49c82524f89dc65008917e3c27de454"
CREDENTIALS_FILE  = r"C:\Users\BOT\Desktop\credentials.json"
SPREADSHEET_NAME  = "YoutubeUploader"
TEMP_VIDEO_DIR    = "C:\\temp_videos"
VIDEOS_DIR        = r"C:\Users\BOT\Desktop\videos"   # Папка с видео на ПК
THUMBNAILS_DIR    = r"C:\Users\BOT\Desktop\thumbnails"  # Папка с превью на ПК
UPLOAD_DELAY      = 5
MAX_RETRIES       = 3

# Колонки Google Sheets
COL_PROFILE_ID     = 1   # A
COL_CHANNEL        = 2   # B
COL_VIDEO_URL      = 3   # C
COL_THUMBNAIL      = 4   # D
COL_TITLE          = 5   # E
COL_DESCRIPTION    = 6   # F
COL_UPLOAD_TIME    = 7   # G
COL_COUNTRY        = 8   # H
COL_STATUS         = 9   # I
COL_GOOGLE_ACCOUNT = 10  # J
COL_CHANNEL_ID     = 11  # K
COL_YOUTUBE_ID     = 12  # L  ← FIX: добавлена недостающая константа


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────
class SheetsManager:
    def __init__(self):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        self.client = gspread.authorize(creds)
        self.sheet  = self.client.open(SPREADSHEET_NAME).sheet1
        logger.info("✅ Google Sheets подключён")

    def get_cell(self, row: list, col: int) -> str:
        """Безопасно получить значение ячейки"""
        try:
            return row[col - 1].strip()
        except IndexError:
            return ""

    def get_pending_tasks(self) -> list:
        """Задачи со статусом пустой — ещё не загружены"""
        rows  = self.sheet.get_all_values()
        tasks = []
        for i, row in enumerate(rows[1:], start=2):
            status = self.get_cell(row, COL_STATUS)
            if status == "":
                profile_id = self.get_cell(row, COL_PROFILE_ID)
                video_url  = self.get_cell(row, COL_VIDEO_URL)
                if not profile_id or not video_url:
                    continue
                tasks.append({
                    "row":            i,
                    "profile_id":     profile_id,
                    "channel":        self.get_cell(row, COL_CHANNEL),
                    "video_url":      video_url,
                    "title":          self.get_cell(row, COL_TITLE),
                    "description":    self.get_cell(row, COL_DESCRIPTION),
                    "upload_time":    self.get_cell(row, COL_UPLOAD_TIME),
                    "country":        self.get_cell(row, COL_COUNTRY),
                    "google_account": self.get_cell(row, COL_GOOGLE_ACCOUNT),
                    "channel_id":     self.get_cell(row, COL_CHANNEL_ID),
                    "thumbnail":      self.get_cell(row, COL_THUMBNAIL),
                })
        return tasks

    def get_uploaded_tasks(self) -> list:
        """Задачи со статусом uploaded — ждут публикации"""
        rows  = self.sheet.get_all_values()
        tasks = []
        for i, row in enumerate(rows[1:], start=2):
            status = self.get_cell(row, COL_STATUS)
            if status == "uploaded":
                tasks.append({
                    "row":            i,
                    "profile_id":     self.get_cell(row, COL_PROFILE_ID),
                    "channel":        self.get_cell(row, COL_CHANNEL),
                    "upload_time":    self.get_cell(row, COL_UPLOAD_TIME),
                    "video_id":       self.get_cell(row, COL_YOUTUBE_ID),
                    "google_account": self.get_cell(row, COL_GOOGLE_ACCOUNT),
                    "channel_id":     self.get_cell(row, COL_CHANNEL_ID),
                })
        return tasks

    def set_status(self, row: int, status: str):
        # Цвета для статусов (текст не пишем, только цвет)
        colors = {
            "uploaded":      {"red": 1.0, "green": 0.95, "blue": 0.4},   # жёлтый
            "published":     {"red": 0.2, "green": 0.8,  "blue": 0.2},   # зелёный
            "error":         {"red": 0.9, "green": 0.2,  "blue": 0.2},   # красный
            "error_publish": {"red": 0.9, "green": 0.2,  "blue": 0.2},   # красный
        }
        color = colors.get(status, {"red": 1.0, "green": 1.0, "blue": 1.0})

        # Очищаем текст в ячейке - только цвет!
        self.sheet.update_cell(row, COL_STATUS, "")

        # Красим ячейку
        try:
            self.sheet.format(f"I{row}", {
                "backgroundColor": color
            })
        except Exception as e:
            logger.warning(f"  Цвет не установлен: {e}")

        logger.info(f"  Sheets строка {row} → {status} (цвет)")

    def set_video_id(self, row: int, video_id: str):
        """После загрузки сохраняем youtube_id в колонку L"""
        self.sheet.update_cell(row, COL_YOUTUBE_ID, video_id)


# ─── GOOGLE DRIVE ─────────────────────────────────────────────────────────────
class DriveManager:
    def __init__(self):
        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        self.service = build("drive", "v3", credentials=creds)
        os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)
        logger.info("✅ Google Drive подключён")

    def extract_file_id(self, url: str) -> str:
        if "/d/" in url:
            return url.split("/d/")[1].split("/")[0]
        if "id=" in url:
            return url.split("id=")[1].split("&")[0]
        return url

    def download_video(self, url: str, filename: str) -> str:
        # Если в колонке указано имя локального файла - просто берём с диска
        if not url.startswith("http"):
            local_path = os.path.join(LOCAL_VIDEOS_DIR, url)
            if os.path.exists(local_path):
                logger.info(f"  ✅ Используем локальный файл: {local_path}")
                # Копируем во временную папку чтобы потом удалить копию
                import shutil
                dest_path = os.path.join(TEMP_VIDEO_DIR, filename)
                shutil.copy(local_path, dest_path)
                return dest_path
            else:
                raise FileNotFoundError(f"Локальный файл не найден: {local_path}")

        # Иначе скачиваем с Google Drive (старая логика)
        file_id   = self.extract_file_id(url)
        dest_path = os.path.join(TEMP_VIDEO_DIR, filename)
        logger.info(f"  Скачиваем с Drive: {file_id}")

        request    = self.service.files().get_media(fileId=file_id)
        fh         = io.FileIO(dest_path, "wb")
        downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)

        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.info(f"  Скачано: {int(status.progress() * 100)}%")
        fh.close()
        logger.info(f"  ✅ Видео скачано: {dest_path}")
        return dest_path

    def delete_video(self, path: str):
        try:
            os.remove(path)
            logger.info(f"  🗑️ Удалён: {path}")
        except Exception as e:
            logger.warning(f"  Не удалось удалить {path}: {e}")


# ─── ADSPOWER API ─────────────────────────────────────────────────────────────
class AdsPowerAPI:
    def _get(self, endpoint: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        params["api_key"] = ADSPOWER_API_KEY
        resp = requests.get(f"{ADSPOWER_BASE_URL}{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def open_browser(self, profile_id: str) -> dict:
        data = self._get("/api/v1/browser/start", {"user_id": profile_id})
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower: {data.get('msg')}")
        return data["data"]

    def close_browser(self, profile_id: str):
        try:
            self._get("/api/v1/browser/stop", {"user_id": profile_id})
        except Exception as e:
            logger.warning(f"Закрытие профиля {profile_id}: {e}")


# ─── ДАЛЬШЕ ИДУТ: YouTubeUploader, MassUploadManager ──
# (части 3, 4 — будут добавлены следующими шагами)
