"""
Space Media Archiver — GUI
Простой интерфейс на tkinter:
- Поле для ключевых слов (через запятую)
- Поле для количества материалов
- Выбор: видео / картинки / и то и другое
- Кнопки: Запуск / Пауза / Продолжить / Стоп / Выход
"""

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import media_archiver as ma


class TextHandler(logging.Handler):
    """Отправляет лог в очередь, GUI читает её и пишет в Text-виджет."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class ArchiverGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Space Media Archiver")
        self.root.geometry("780x620")
        self.root.minsize(680, 540)

        self.control: ma.Control | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.total_saved = 0

        self._build_ui()
        self._wire_logging()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

    # ─── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Параметры
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
        media_frame = ttk.Frame(params)
        media_frame.grid(row=1, column=3, sticky="w", padx=6, pady=6)
        ttk.Radiobutton(media_frame, text="Видео", variable=self.media_var, value="video").pack(side="left")
        ttk.Radiobutton(media_frame, text="Картинки", variable=self.media_var, value="image").pack(side="left")
        ttk.Radiobutton(media_frame, text="И то, и другое", variable=self.media_var, value="both").pack(side="left")

        ttk.Label(params, text="Папка для сохранения:").grid(row=2, column=0, sticky="w", padx=6, pady=6)
        self.out_var = tk.StringVar(value=os.path.abspath("archive_db"))
        ttk.Entry(params, textvariable=self.out_var, width=50).grid(row=2, column=1, columnspan=2, sticky="we", padx=6, pady=6)
        ttk.Button(params, text="Обзор...", command=self._browse_dir).grid(row=2, column=3, sticky="w", padx=6, pady=6)

        params.columnconfigure(1, weight=1)

        # Кнопки управления
        controls = ttk.Frame(self.root)
        controls.pack(fill="x", **pad)

        self.btn_start = ttk.Button(controls, text="▶ Запуск", command=self.on_start)
        self.btn_pause = ttk.Button(controls, text="⏸ Пауза", command=self.on_pause, state="disabled")
        self.btn_resume = ttk.Button(controls, text="⏵ Продолжить", command=self.on_resume, state="disabled")
        self.btn_stop = ttk.Button(controls, text="⏹ Стоп", command=self.on_stop, state="disabled")
        self.btn_exit = ttk.Button(controls, text="✕ Выход", command=self.on_exit)

        for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop):
            b.pack(side="left", padx=4, pady=4)
        self.btn_exit.pack(side="right", padx=4, pady=4)

        # Статус
        status = ttk.Frame(self.root)
        status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="Готов к запуску")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.counter_var = tk.StringVar(value="Скачано: 0")
        ttk.Label(status, textvariable=self.counter_var, anchor="e").pack(side="right")

        # Лог
        log_frame = ttk.LabelFrame(self.root, text="Лог работы")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled", bg="#111", fg="#ddd", insertbackground="#ddd")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if path:
            self.out_var.set(path)

    # ─── Logging plumbing ────────────────────────────────────────────────────
    def _wire_logging(self):
        handler = TextHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        ma.logger.addHandler(handler)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log_queue)

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ─── Actions ─────────────────────────────────────────────────────────────
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

        self.control = ma.Control()
        self.total_saved = 0
        self.counter_var.set("Скачано: 0")
        self.status_var.set("Работает...")
        self._set_buttons(running=True, paused=False)
        self._append_log(f"=== Старт: темы={topics}, лимит={limit}, типы={media}, папка={out} ===")

        def progress(total):
            self.total_saved = total
            self.root.after(0, lambda: self.counter_var.set(f"Скачано: {total}"))

        def worker():
            try:
                ma.run(
                    topics=topics,
                    media_types=media,
                    sources=list(ma.SOURCES.keys()),
                    limit=limit,
                    root=out,
                    workers=8,
                    control=self.control,
                    on_progress=progress,
                )
            except Exception as e:
                ma.logger.error("Сбой: %s", e)
            finally:
                self.root.after(0, self._on_worker_done)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def on_pause(self):
        if not self.control:
            return
        self.control.pause()
        self.status_var.set("Пауза")
        self._set_buttons(running=True, paused=True)
        self._append_log("=== Пауза ===")

    def on_resume(self):
        if not self.control:
            return
        self.control.resume()
        self.status_var.set("Работает...")
        self._set_buttons(running=True, paused=False)
        self._append_log("=== Продолжаем ===")

    def on_stop(self):
        if not self.control:
            return
        self.control.stop()
        self.status_var.set("Останавливается...")
        self._append_log("=== Остановка по запросу ===")

    def on_exit(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Выход", "Загрузка идёт. Прервать и выйти?"):
                return
            if self.control:
                self.control.stop()
        self.root.destroy()
        os._exit(0)  # экстренное завершение всех потоков

    def _on_worker_done(self):
        self.status_var.set(f"Готово. Всего скачано: {self.total_saved}")
        self._set_buttons(running=False, paused=False)
        self._append_log(f"=== Завершено. Скачано: {self.total_saved} ===")

    def _set_buttons(self, running: bool, paused: bool):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_pause.configure(state="normal" if running and not paused else "disabled")
        self.btn_resume.configure(state="normal" if running and paused else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    ArchiverGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
