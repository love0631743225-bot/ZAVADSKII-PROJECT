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
COL_YOUTUBE_ID     = 12  # L  ← FIX #1: добавлена недостающая константа
COL_INTERNAL_STATUS = 13 # M  ← FIX #3: внутренний текстовый статус (state machine).
                         #         Колонка I остаётся только цветной для пользователя.


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
            # FIX #3: читаем state machine из скрытой колонки M, а не из I (цветной)
            internal_status = self.get_cell(row, COL_INTERNAL_STATUS)
            youtube_id      = self.get_cell(row, COL_YOUTUBE_ID)
            if internal_status == "" and youtube_id == "":
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
            # FIX #3: читаем state machine из скрытой колонки M
            internal_status = self.get_cell(row, COL_INTERNAL_STATUS)
            if internal_status == "uploaded":
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

        # FIX #3: пишем текстовый статус в скрытую колонку M
        # (колонка I остаётся только цветной, как ты хотел)
        try:
            self.sheet.update_cell(row, COL_INTERNAL_STATUS, status)
        except Exception as e:
            logger.warning(f"  Internal status не сохранён: {e}")

        logger.info(f"  Sheets строка {row} → {status} (цвет + internal)")

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
            local_path = os.path.join(VIDEOS_DIR, url)
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


# ─── YOUTUBE UPLOADER ────────────────────────────────────────────────────────
class YouTubeUploader:
    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver
        self.wait   = WebDriverWait(driver, 60)

    # ── Переключение аккаунта ──────────────────────────────────────────────
    def switch_google_account(self, email: str):
        """Переключиться на нужный Google аккаунт"""
        if not email:
            return
        logger.info(f"  Переключаемся на аккаунт: {email}")
        try:
            self.driver.get("https://accounts.google.com/AccountChooser")
            time.sleep(3)

            # Ищем нужный аккаунт в списке
            accounts = self.driver.find_elements(By.CSS_SELECTOR, "[data-email]")
            for acc in accounts:
                if acc.get_attribute("data-email") == email:
                    acc.click()
                    time.sleep(3)
                    logger.info(f"  ✅ Аккаунт выбран: {email}")
                    return

            logger.warning(f"  Аккаунт {email} не найден в списке")
        except Exception as e:
            logger.warning(f"  Переключение аккаунта: {e}")

    # ── Переключение канала ────────────────────────────────────────────────
    def switch_channel(self, channel_id: str):
        """Переключиться на нужный канал (основной или бренд)"""
        if not channel_id:
            return
        logger.info(f"  Переключаемся на канал: {channel_id}")
        try:
            self.driver.get(f"https://studio.youtube.com/channel/{channel_id}")
            time.sleep(6)

            # Проверяем что страница загрузилась
            current_url = self.driver.current_url
            logger.info(f"  URL после переключения: {current_url}")

            # Ждём появления элементов Studio
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#upload-icon, ytcp-icon-button, #avatar-btn"))
                )
            except Exception:
                pass

            time.sleep(3)
            logger.info(f"  ✅ Канал выбран: {channel_id}")
        except Exception as e:
            logger.warning(f"  Переключение канала: {e}")

    # ── Загрузка превью ──────────────────────────────────────────────────
    def upload_thumbnail(self, thumbnail_path: str):
        """Загрузить превью к видео"""
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            return
        try:
            thumb_input = self.driver.find_element(
                By.CSS_SELECTOR, "input[type='file'][accept*='image']"
            )
            thumb_input.send_keys(os.path.abspath(thumbnail_path))
            logger.info(f"  ✅ Превью загружено: {thumbnail_path}")
            time.sleep(3)
        except Exception as e:
            logger.warning(f"  Превью: {e}")

    # ── Загрузка видео ────────────────────────────────────────────────────
    def upload_as_private(self, video_path: str, title: str, description: str, thumbnail_path: str = "", schedule_time: str = "") -> str:
        """Загрузить видео как PRIVATE, вернуть video_id"""
        # Кнопка загрузки
        # Ждём загрузки страницы
        time.sleep(5)

        # Закрываем любые модальные окна (Welcome, подсказки и т.д.)
        close_selectors = [
            "[aria-label='Close']",
            "[aria-label='Закрыть']",
            ".ytcp-dialog paper-button[dialog-dismiss]",
            "#dismiss-button",
            "tp-yt-paper-button[dialog-dismiss]",
            ".ytd-button-renderer[aria-label='Continue']",
        ]
        for sel in close_selectors:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                btn.click()
                logger.info(f"  Закрыто модальное окно: {sel}")
                time.sleep(1)
            except Exception:
                pass

        # Также пробуем нажать Continue если есть
        try:
            continue_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Continue')]")
            continue_btn.click()
            time.sleep(2)
            logger.info("  Закрыто Welcome окно (Continue)")
        except Exception:
            pass

        time.sleep(2)

        # Пробуем разные селекторы кнопки загрузки
        upload_btn = None
        selectors = [
            "#upload-icon",
            "[aria-label='Upload videos']",
            "[aria-label='Загрузить видео']",
            "ytcp-button#upload-icon",
            "#upload-icon-button",
            ".ytcp-icon-button[id='upload-icon']",
        ]
        for sel in selectors:
            try:
                upload_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                logger.info(f"  Кнопка загрузки найдена: {sel}")
                break
            except Exception:
                continue

        if not upload_btn:
            raise Exception("Кнопка загрузки не найдена! Проверьте что открыт YouTube Studio")

        # Кликаем через JavaScript в обход модальных окон
        try:
            self.driver.execute_script("arguments[0].click();", upload_btn)
        except Exception:
            upload_btn.click()
        time.sleep(2)

        # Выбираем файл
        file_input = self.wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        file_input.send_keys(os.path.abspath(video_path))
        logger.info(f"  Файл отправлен")
        time.sleep(6)

        # Заголовок - даём YouTube время заполнить дефолтное название из имени файла
        time.sleep(5)
        try:
            # Пробуем разные селекторы
            title_field = None
            for sel in ["#title-textarea #textbox", "#title-textarea #child-input", "ytcp-mention-textbox#title #textbox"]:
                try:
                    title_field = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                    )
                    if title_field:
                        logger.info(f"  Поле заголовка найдено: {sel}")
                        break
                except Exception:
                    continue

            if title_field:
                time.sleep(2)
                self.driver.execute_script("arguments[0].scrollIntoView(true);", title_field)
                time.sleep(1)
                # Кликаем 3 раза подряд чтобы выделить весь текст
                self.driver.execute_script("arguments[0].click();", title_field)
                time.sleep(0.5)
                # Triple click для выделения всего содержимого
                from selenium.webdriver.common.action_chains import ActionChains
                actions = ActionChains(self.driver)
                actions.move_to_element(title_field).click().click().click().perform()
                time.sleep(0.5)
                # Несколько способов очистки подряд
                title_field.send_keys(Keys.CONTROL + "a")
                time.sleep(0.3)
                title_field.send_keys(Keys.BACKSPACE)
                time.sleep(0.3)
                # Очищаем через JS на всякий случай
                self.driver.execute_script("arguments[0].innerText = '';", title_field)
                time.sleep(0.5)
                # Вставляем заголовок
                title_field.click()
                title_field.send_keys(title)
                time.sleep(1)
                logger.info(f"  Заголовок вставлен: {title}")
            else:
                logger.warning("  Поле заголовка не найдено!")
        except Exception as e:
            logger.warning(f"  Заголовок: {e}")

        # Описание
        # Загружаем превью если есть
        if hasattr(self, '_thumbnail_path') and self._thumbnail_path:
            self.upload_thumbnail(self._thumbnail_path)

        if description:
            try:
                desc = None
                for sel in ["#description-textarea #textbox", "#description-textarea #child-input", "ytcp-mention-textbox#description #textbox"]:
                    try:
                        desc = self.driver.find_element(By.CSS_SELECTOR, sel)
                        if desc:
                            break
                    except Exception:
                        continue

                if desc:
                    time.sleep(1)
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", desc)
                    time.sleep(1)
                    self.driver.execute_script("arguments[0].click();", desc)
                    time.sleep(1)
                    desc.send_keys(description)
                    logger.info(f"  Описание вставлено")
            except Exception as e:
                logger.warning(f"  Описание: {e}")

        # 3x Next
        for i in range(3):
            try:
                next_btn = self.wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "#next-button"))
                )
                next_btn.click()
                time.sleep(2)
            except Exception as e:
                logger.warning(f"  Next {i+1}: {e}")

        # Schedule (если задано время) или PRIVATE
        if schedule_time:
            try:
                from datetime import datetime as dt
                schedule_dt = dt.strptime(schedule_time.strip(), "%d.%m.%Y %H:%M")

                # Скроллим к секции Schedule
                time.sleep(2)

                # Ищем Schedule label/radio - несколько вариантов
                schedule_clicked = False
                schedule_selectors = [
                    "//tp-yt-paper-radio-button[@name='SCHEDULE']",
                    "//div[contains(@class, 'second-container')]//tp-yt-paper-radio-button[2]",
                    "//ytcp-button[@id='second-container-expand-button']",
                    "//label[contains(., 'Schedule')]",
                    "//div[text()='Schedule']/..",
                ]
                for sel in schedule_selectors:
                    try:
                        elem = self.driver.find_element(By.XPATH, sel)
                        self.driver.execute_script("arguments[0].scrollIntoView(true);", elem)
                        time.sleep(1)
                        self.driver.execute_script("arguments[0].click();", elem)
                        time.sleep(2)
                        logger.info(f"  Schedule выбран через: {sel}")
                        schedule_clicked = True
                        break
                    except Exception:
                        continue

                if not schedule_clicked:
                    # Альтернатива - открыть выпадающее меню Schedule
                    try:
                        expand = self.driver.find_element(By.CSS_SELECTOR, "#second-container-expand-button")
                        self.driver.execute_script("arguments[0].click();", expand)
                        time.sleep(2)
                        logger.info("  Открыт раздел Schedule")
                        schedule_clicked = True
                    except Exception as e:
                        logger.warning(f"  Schedule не выбран: {e}")

                logger.info(f"  Время для Schedule: {schedule_time}")

                # Заполняем дату
                try:
                    date_field = self.wait.until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "ytcp-date-picker input, #datepicker-trigger input")
                        )
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", date_field)
                    time.sleep(1)
                    self.driver.execute_script("arguments[0].click();", date_field)
                    time.sleep(1)
                    date_field.send_keys(Keys.CONTROL + "a")
                    date_field.send_keys(Keys.DELETE)
                    date_str = schedule_dt.strftime("%b %d, %Y")
                    date_field.send_keys(date_str)
                    time.sleep(0.5)
                    date_field.send_keys(Keys.ENTER)
                    time.sleep(2)
                    logger.info(f"  ✅ Дата установлена: {date_str}")
                except Exception as e:
                    logger.warning(f"  Дата: {e}")

                # Заполняем время - адаптируется под 12h (AM/PM) и 24h формат
                try:
                    time.sleep(3)

                    # Несколько вариантов селекторов
                    inputs = []
                    for sel in [
                        "ytcp-form-input-container input.tp-yt-paper-input",
                        "ytcp-form-input-container input",
                        "tp-yt-paper-input input",
                        "input[aria-label*='time']",
                        "input[aria-label*='ремя']",
                    ]:
                        found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        if found:
                            inputs = found
                            logger.info(f"  Поля найдены через: {sel} ({len(found)} шт.)")
                            break

                    # Логируем все найденные поля
                    for idx, inp in enumerate(inputs):
                        val = inp.get_attribute("value") or ""
                        aria = inp.get_attribute("aria-label") or ""
                        logger.info(f"    [{idx}] value='{val}' aria-label='{aria}'")

                    # Ищем поле времени
                    time_field = None
                    is_12h_format = False
                    for inp in inputs:
                        val = inp.get_attribute("value") or ""
                        aria = (inp.get_attribute("aria-label") or "").lower()
                        # Время по значению или по aria-label
                        if ":" in val or "time" in aria or "ремя" in aria:
                            # Не дата
                            if not any(m in val.lower() for m in ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "янв", "фев"]):
                                time_field = inp
                                if "AM" in val.upper() or "PM" in val.upper():
                                    is_12h_format = True
                                break

                    if time_field:
                        from selenium.webdriver.common.action_chains import ActionChains
                        # Кликаем в поле
                        self.driver.execute_script("arguments[0].click();", time_field)
                        time.sleep(0.5)
                        # Triple click для выделения всего
                        actions = ActionChains(self.driver)
                        actions.move_to_element(time_field).click().click().click().perform()
                        time.sleep(0.5)
                        # Очищаем несколько раз для надёжности
                        time_field.send_keys(Keys.CONTROL + "a")
                        time.sleep(0.3)
                        time_field.send_keys(Keys.BACKSPACE)
                        time.sleep(0.3)
                        time_field.send_keys(Keys.CONTROL + "a")
                        time.sleep(0.3)
                        time_field.send_keys(Keys.DELETE)
                        time.sleep(0.3)
                        # Очищаем через JS
                        self.driver.execute_script("arguments[0].value = '';", time_field)
                        time.sleep(0.5)

                        # Подбираем формат под локаль YouTube
                        if is_12h_format:
                            # 12-часовой формат: "3:30 AM" или "11:30 PM"
                            time_str = schedule_dt.strftime("%I:%M %p").lstrip("0")
                            logger.info(f"  Формат 12h: {time_str}")
                        else:
                            # 24-часовой формат: "03:30" или "23:00"
                            time_str = schedule_dt.strftime("%H:%M")
                            logger.info(f"  Формат 24h: {time_str}")

                        time_field.send_keys(time_str)
                        time.sleep(1)
                        time_field.send_keys(Keys.TAB)
                        time.sleep(1)
                        logger.info(f"  Время установлено: {time_str}")

                        # Кликаем где-то вне поля чтобы зафиксировать
                        try:
                            self.driver.find_element(By.TAG_NAME, "body").click()
                            time.sleep(1)
                        except Exception:
                            pass
                    else:
                        logger.warning("  Поле времени не найдено!")
                except Exception as e:
                    logger.warning(f"  Время: {e}")

                # Выбираем часовой пояс по стране из колонки H
                try:
                    # FIX #5: убрана мёртвая ветка с локальной `task` (NameError при срабатывании).
                    # Используем self._country, который проставляется в upload_all.
                    country = ""
                    if hasattr(self, "_country") and self._country:
                        country = self._country.strip().upper()

                    if country:
                        time.sleep(1)
                        # Открываем выбор часового пояса
                        tz_btn = self.driver.find_element(By.XPATH, "//ytcp-button[contains(., 'Time zone')] | //tp-yt-paper-button[contains(., 'Time zone')]")
                        self.driver.execute_script("arguments[0].click();", tz_btn)
                        time.sleep(2)

                        # Маппинг страны на ключевое слово в выпадающем списке
                        tz_map = {
                            "RU": "Moscow",
                            "US": "New York",
                            "DE": "Berlin",
                            "PL": "Warsaw",
                            "GB": "London",
                            "UK": "London",
                            "FR": "Paris",
                            "ES": "Madrid",
                            "IT": "Rome",
                            "UA": "Kyiv",
                            "BY": "Minsk",
                            "KZ": "Almaty",
                        }
                        tz_search = tz_map.get(country, country)

                        # Ищем нужный пункт в списке часовых поясов
                        time.sleep(1)
                        items = self.driver.find_elements(By.CSS_SELECTOR, "tp-yt-paper-item, [role='option'], ytcp-text-menu-item")
                        logger.info(f"  Найдено {len(items)} часовых поясов в списке")

                        clicked = False
                        for item in items:
                            try:
                                text = item.text.strip()
                                if tz_search.lower() in text.lower():
                                    self.driver.execute_script("arguments[0].scrollIntoView(true);", item)
                                    time.sleep(0.5)
                                    self.driver.execute_script("arguments[0].click();", item)
                                    time.sleep(2)
                                    logger.info(f"  ✅ Часовой пояс выбран: {text}")
                                    clicked = True
                                    break
                            except Exception:
                                continue

                        if not clicked:
                            logger.warning(f"  Часовой пояс {tz_search} не найден в списке, закрываем меню")
                            try:
                                # Закрываем меню кликом вне
                                self.driver.find_element(By.TAG_NAME, "body").click()
                                time.sleep(1)
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"  Часовой пояс: {e}")

                # Проверяем что Schedule активен и галочка стоит
                try:
                    time.sleep(2)
                    schedule_radios = self.driver.find_elements(By.CSS_SELECTOR, "tp-yt-paper-radio-button[name='SCHEDULE']")
                    for r in schedule_radios:
                        checked = r.get_attribute("aria-checked")
                        if checked != "true":
                            logger.info("  Schedule не выбран, кликаем ещё раз...")
                            self.driver.execute_script("arguments[0].click();", r)
                            time.sleep(1)
                except Exception as e:
                    logger.warning(f"  Проверка Schedule: {e}")

                # Нажимаем кнопку подтверждения Schedule (Done/Set/Подтвердить)
                try:
                    time.sleep(1)
                    confirm_buttons = self.driver.find_elements(By.TAG_NAME, "ytcp-button")
                    for btn in confirm_buttons:
                        text = btn.text.strip().lower()
                        if text in ["done", "set", "confirm", "save", "подтвердить", "готово", "сохранить"]:
                            try:
                                self.driver.execute_script("arguments[0].click();", btn)
                                logger.info(f"  Нажата кнопка подтверждения: {text}")
                                time.sleep(2)
                                break
                            except Exception:
                                continue

                    # Также пробуем paper-button
                    paper_btns = self.driver.find_elements(By.TAG_NAME, "paper-button")
                    for btn in paper_btns:
                        text = btn.text.strip().lower()
                        if text in ["done", "set", "confirm", "подтвердить", "готово"]:
                            try:
                                self.driver.execute_script("arguments[0].click();", btn)
                                logger.info(f"  Нажата paper кнопка: {text}")
                                time.sleep(2)
                                break
                            except Exception:
                                continue
                except Exception as e:
                    logger.warning(f"  Подтверждение Schedule: {e}")

            except Exception as e:
                logger.warning(f"  Schedule: {e}, ставим PRIVATE")
                try:
                    radio = self.wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//tp-yt-paper-radio-button[@name='PRIVATE']")
                        )
                    )
                    self.driver.execute_script("arguments[0].click();", radio)
                    time.sleep(1)
                except Exception:
                    pass
        else:
            try:
                radio = self.wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//tp-yt-paper-radio-button[@name='PRIVATE']")
                    )
                )
                self.driver.execute_script("arguments[0].click();", radio)
                time.sleep(1)
                logger.info("  Видимость: PRIVATE ✅")
            except Exception as e:
                logger.warning(f"  Видимость: {e}")

        # Ждём пока видео загрузится (прогресс бар исчезнет)
        logger.info("  Ожидаем полную загрузку видео на YouTube (это может занять до 20 минут)...")
        last_progress = ""
        last_sheet_update = time.time()
        current_row = getattr(self, "current_row", None)
        current_sheets = getattr(self, "current_sheets", None)
        for attempt in range(240):  # макс 40 мин
            try:
                # Проверяем по тексту страницы
                page_text = self.driver.page_source

                # Ищем процент загрузки
                import re
                match = re.search(r'(\d+)%\s*(?:uploaded|загружено)', page_text, re.IGNORECASE)
                if match:
                    pct = int(match.group(1))
                    if str(pct) != last_progress:
                        logger.info(f"  Загрузка: {pct}%")
                        last_progress = str(pct)
                        # Обновляем Sheets каждые 20 секунд
                        if current_row and current_sheets and (time.time() - last_sheet_update) > 20:
                            try:
                                current_sheets.sheet.update_cell(current_row, 9, f"{pct}%")
                                last_sheet_update = time.time()
                            except Exception:
                                pass
                    if pct >= 95:
                        if current_row and current_sheets:
                            try:
                                current_sheets.sheet.update_cell(current_row, 9, "")
                            except Exception:
                                pass
                        logger.info(f"  ✅ Загрузка {pct}%! Ждём 60 секунд обработки YouTube...")
                        time.sleep(60)
                        break

                # Проверка кнопки done
                done_btns = self.driver.find_elements(By.CSS_SELECTOR, "#done-button")
                if done_btns:
                    is_disabled = done_btns[0].get_attribute("disabled")
                    aria_disabled = done_btns[0].get_attribute("aria-disabled")
                    if not is_disabled and aria_disabled != "true":
                        # Если кнопка активна и прогресс достиг 100% - выходим
                        if last_progress == "100":
                            logger.info("  ✅ Видео готово!")
                            break

                time.sleep(10)
            except Exception as e:
                logger.warning(f"  Проверка прогресса: {e}")
                time.sleep(10)

        # FIX #4: извлекаем реальный video_id из share-ссылки в диалоге.
        # YouTube показывает ссылку "https://youtu.be/XXXXX" в диалоге загрузки.
        video_id = ""
        share_selectors = [
            "a.from-input[href*='youtu.be']",
            ".ytcp-video-info a[href*='youtu.be']",
            "a[href*='youtu.be/']",
            "span.ytcp-video-info",
        ]
        for sel in share_selectors:
            try:
                elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                href = elem.get_attribute("href") or ""
                text = elem.text or ""
                source = href if "youtu.be/" in href else text
                if "youtu.be/" in source:
                    video_id = source.split("youtu.be/")[-1].split("?")[0].strip()
                    logger.info(f"  ✅ video_id извлечён ({sel}): {video_id}")
                    break
            except Exception:
                continue

        if not video_id:
            # Fallback: ищем по тексту страницы
            try:
                import re as _re
                match = _re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", self.driver.page_source)
                if match:
                    video_id = match.group(1)
                    logger.info(f"  ✅ video_id извлечён (regex): {video_id}")
            except Exception:
                pass

        if not video_id:
            logger.warning("  ⚠️ video_id не извлечён — публикация по расписанию не сработает")

        # Сохраняем
        try:
            save_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#done-button"))
            )
            save_btn.click()
            time.sleep(3)

            # Закрываем окно "We're still checking your content"
            try:
                btns = self.driver.find_elements(By.TAG_NAME, "paper-button")
                for btn in btns:
                    if btn.text.strip().lower() in ["got it", "continue", "ok"]:
                        btn.click()
                        logger.info("  Закрыто предупреждение YouTube")
                        time.sleep(1)
                        break
            except Exception:
                pass

            time.sleep(2)

            logger.info("  ✅ Сохранено!")
            # FIX #4: возвращаем настоящий video_id, а не литерал "ok"
            return video_id
        except Exception as e:
            logger.error(f"  Сохранение: {e}")
            return ""

    # ── Публикация ────────────────────────────────────────────────────────
    def publish_video(self, video_id: str, channel_id: str = ""):
        """Сменить PRIVATE → PUBLIC"""
        if channel_id:
            url = f"https://studio.youtube.com/channel/{channel_id}/videos/upload?filter=%5B%5D&sort=dd"
        else:
            url = f"https://studio.youtube.com/video/{video_id}/edit"

        self.driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
        time.sleep(4)

        try:
            visibility_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "[aria-label='Visibility']"))
            )
            visibility_btn.click()
            time.sleep(2)

            public_radio = self.wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//tp-yt-paper-radio-button[@name='PUBLIC']")
                )
            )
            public_radio.click()
            time.sleep(1)

            save_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#save-button"))
            )
            save_btn.click()
            time.sleep(3)
            logger.info(f"  ✅ Опубликовано: {video_id}")

        except Exception as e:
            logger.error(f"  Ошибка публикации: {e}")
            raise


# ─── ОСНОВНОЙ МЕНЕДЖЕР ───────────────────────────────────────────────────────
class MassUploadManager:
    def __init__(self):
        self.adspower = AdsPowerAPI()
        self.sheets   = SheetsManager()
        self.drive    = DriveManager()

    def _get_driver(self, profile_id: str) -> webdriver.Chrome:
        """Открыть профиль AdsPower и вернуть Selenium драйвер"""
        browser_data     = self.adspower.open_browser(profile_id)
        logger.info(f"  AdsPower ответ: {json.dumps(browser_data, ensure_ascii=False)}")
        selenium_address = browser_data.get("ws", {}).get("selenium", "")
        webdriver_path   = browser_data.get("webdriver", "")
        logger.info(f"  Selenium: {selenium_address}")
        logger.info(f"  Webdriver: {webdriver_path}")
        time.sleep(3)

        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.debugger_address = selenium_address

        service = Service(executable_path=webdriver_path)
        driver  = webdriver.Chrome(service=service, options=options)

        driver.minimize_window()
        return driver

    # ── Режим 1: Загрузка ─────────────────────────────────────────────────
    def upload_all(self):
        tasks = self.sheets.get_pending_tasks()
        logger.info(f"═══ ЗАГРУЗКА. Задач: {len(tasks)} ═══\n")

        for i, task in enumerate(tasks, 1):
            logger.info(f"[{i}/{len(tasks)}] {task['channel']}")
            video_path = None
            driver     = None

            try:
                # 0. Сбрасываем цвет I, текст в I, youtube_id (L) и internal_status (M)
                try:
                    row_num = task["row"]
                    self.sheets.sheet.format(f"I{row_num}", {
                        "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                    })
                    self.sheets.sheet.update_cell(row_num, COL_STATUS, "")
                    # FIX #3 + #4: чистим скрытые служебные колонки тоже
                    self.sheets.sheet.update_cell(row_num, COL_YOUTUBE_ID, "")
                    self.sheets.sheet.update_cell(row_num, COL_INTERNAL_STATUS, "")
                    logger.info(f"  Строка {row_num} сброшена")
                except Exception:
                    pass

                # 1. Берём видео из локальной папки
                video_url = task["video_url"].strip()
                # Если это путь — используем его, иначе ищем в VIDEOS_DIR
                if os.path.isabs(video_url):
                    video_path = video_url
                else:
                    video_path = os.path.join(VIDEOS_DIR, video_url)

                if not os.path.exists(video_path):
                    raise FileNotFoundError(f"Видео не найдено: {video_path}")
                logger.info(f"  ✅ Видео найдено: {video_path}")

                # 1.5 Берём превью из локальной папки
                thumbnail_path = ""
                if task.get("thumbnail"):
                    thumb_url = task["thumbnail"].strip()
                    if os.path.isabs(thumb_url):
                        thumbnail_path = thumb_url
                    else:
                        thumbnail_path = os.path.join(THUMBNAILS_DIR, thumb_url)

                    if not os.path.exists(thumbnail_path):
                        logger.warning(f"  Превью не найдено: {thumbnail_path}")
                        thumbnail_path = ""
                    else:
                        logger.info(f"  ✅ Превью найдено: {thumbnail_path}")

                # 2. Открываем профиль
                driver = self._get_driver(task["profile_id"])
                uploader = YouTubeUploader(driver)
                uploader._thumbnail_path = thumbnail_path
                uploader._country = task.get("country", "")
                uploader.current_row = task["row"]
                uploader.current_sheets = self.sheets

                # 3. Переключаем аккаунт (если нужно)
                if task["google_account"]:
                    uploader.switch_google_account(task["google_account"])

                # 4. Переключаем канал (если нужно)
                if task["channel_id"]:
                    uploader.switch_channel(task["channel_id"])
                else:
                    driver.get("https://studio.youtube.com")
                    time.sleep(4)

                # 5. Загружаем с расписанием
                video_id = uploader.upload_as_private(
                    video_path, task["title"], task["description"],
                    schedule_time=task.get("upload_time", "")
                )

                if video_id:
                    logger.info("  ✅ Видео полностью загружено! Закроем профиль через 5 секунд...")
                    time.sleep(5)
                    # FIX #4: сохраняем настоящий video_id в колонку L,
                    # чтобы потом планировщик его нашёл
                    try:
                        self.sheets.set_video_id(task["row"], video_id)
                    except Exception as e:
                        logger.warning(f"  set_video_id: {e}")
                    self.sheets.set_status(task["row"], "uploaded")
                    try:
                        row_num = task["row"]
                        self.sheets.sheet.format(f"I{row_num}", {
                            "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.4}
                        })
                    except Exception:
                        pass
                    logger.info(f"✅ Загружено: {video_id}\n")
                else:
                    self.sheets.set_status(task["row"], "")
                    logger.error(f"❌ video_id не получен\n")

            except Exception as e:
                import traceback
                logger.error(f"❌ Ошибка: {e}")
                logger.error(traceback.format_exc())
                self.sheets.set_status(task["row"], "")

            finally:
                if driver:
                    try: driver.quit()
                    except: pass
                self.adspower.close_browser(task["profile_id"])
                # Видео не удаляем - это файлы пользователя
                time.sleep(UPLOAD_DELAY)

        logger.info("═══ Загрузка завершена ═══")

    # ── Режим 2: Публикация по расписанию ─────────────────────────────────
    def publish_scheduled(self):
        logger.info("═══ ПЛАНИРОВЩИК запущен (проверка каждую минуту) ═══")

        while True:
            now   = datetime.now().strftime("%d.%m.%Y %H:%M")
            tasks = self.sheets.get_uploaded_tasks()

            for task in tasks:
                if task["upload_time"] == now:
                    logger.info(f"⏰ Публикуем: {task['channel']}")
                    driver = None
                    try:
                        driver   = self._get_driver(task["profile_id"])
                        uploader = YouTubeUploader(driver)

                        if task["google_account"]:
                            uploader.switch_google_account(task["google_account"])

                        uploader.publish_video(task["video_id"], task["channel_id"])
                        self.sheets.set_status(task["row"], "published")

                    except Exception as e:
                        logger.error(f"❌ Ошибка: {e}")
                        self.sheets.set_status(task["row"], "")

                    finally:
                        if driver:
                            try: driver.quit()
                            except: pass
                        self.adspower.close_browser(task["profile_id"])

            time.sleep(60)


# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "upload"

    manager = MassUploadManager()

    if mode == "upload":
        manager.upload_all()
    elif mode == "publish":
        manager.publish_scheduled()
    elif mode == "both":
        manager.upload_all()
        manager.publish_scheduled()
    else:
        print("Использование:")
        print("  python uploader.py upload   — загрузить все как PRIVATE")
        print("  python uploader.py publish  — публиковать по расписанию")
        print("  python uploader.py both     — загрузить + публиковать")
