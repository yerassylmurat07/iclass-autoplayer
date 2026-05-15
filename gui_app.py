import customtkinter as ctk
import threading
import json
import os
import sys
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Импортируем наш движок (созданный на базе autoplayer)
import gui_engine

# Загружаем переменные окружения (.env)
load_dotenv()

CONFIG_FILE = "gui_config.json"

class MoodleAutoplayerGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("iClass AutoPlayer Pro")
        self.geometry("950x700")
        self.minsize(800, 600)
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.config = self.load_config()
        self.video_bars = {}  
        self.checkbox_vars = {} 
        self.worker_thread = None

        # Жесткий фикс копипасты для macOS
        self.bind_all("<Command-c>", self.copy_text)
        self.bind_all("<Command-v>", self.paste_text)
        self.bind_all("<Command-a>", self.select_all_text)
        self.bind_all("<Command-x>", self.cut_text)
        self.bind_all("<Meta-c>", self.copy_text)
        self.bind_all("<Meta-v>", self.paste_text)
        self.bind_all("<Meta-a>", self.select_all_text)
        self.bind_all("<Meta-x>", self.cut_text)

        self.create_widgets()

    def copy_text(self, event=None):
        try:
            widget = self.focus_get()
            if hasattr(widget, "selection_get"):
                self.clipboard_clear()
                self.clipboard_append(widget.selection_get())
            elif isinstance(widget, ctk.CTkEntry):
                self.clipboard_clear()
                self.clipboard_append(widget.get())
            return "break"
        except: pass

    def paste_text(self, event=None):
        try:
            text = self.clipboard_get()
            widget = self.focus_get()
            if hasattr(widget, "insert"):
                try:
                    if hasattr(widget, "delete") and widget.select_present():
                        widget.delete("sel.first", "sel.last")
                except: pass
                widget.insert("insert", text)
            return "break"
        except: pass

    def cut_text(self, event=None):
        try:
            self.copy_text()
            widget = self.focus_get()
            if hasattr(widget, "delete"):
                try:
                    if widget.select_present():
                        widget.delete("sel.first", "sel.last")
                except: pass
            return "break"
        except: pass

    def select_all_text(self, event=None):
        try:
            widget = self.focus_get()
            if hasattr(widget, "select_range"):
                widget.select_range(0, "end")
            return "break"
        except: pass

    def load_config(self):
        default_config = {"course_history": [], "speed": 1.0, "headless": True}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default_config.update(loaded)
            except:
                pass
        return default_config

    def save_config(self):
        course = self.course_combo.get().strip()
        history = self.config.get("course_history", [])
        if course:
            if course in history:
                history.remove(course)
            history.insert(0, course)
            self.config["course_history"] = history[:10]

        self.config["speed"] = float(self.speed_slider.get())
        self.config["headless"] = self.headless_var.get()

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f)

    def create_widgets(self):
        # Настройка сетки окна
        self.grid_columnconfigure(0, weight=1)  # Левая панель
        self.grid_columnconfigure(1, weight=2)  # Центральная панель
        self.grid_rowconfigure(0, weight=3)     # Верх (левая + центр)
        self.grid_rowconfigure(1, weight=1)     # Низ (мониторинг)

        # ==========================================
        # 1. ЛЕВАЯ ПАНЕЛЬ (Настройки)
        # ==========================================
        self.left_panel = ctk.CTkFrame(self)
        self.left_panel.grid(row=0, column=0, padx=(20, 10), pady=(20, 10), sticky="nsew")
        
        ctk.CTkLabel(self.left_panel, text="⚙️ Настройки", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(15, 15))

        # Логин
        env_login = os.getenv("MOODLE_LOGIN", "")
        ctk.CTkLabel(self.left_panel, text="Логин (из .env):", anchor="w").pack(fill="x", padx=20, pady=(5, 0))
        self.login_entry = ctk.CTkEntry(self.left_panel, placeholder_text="ID Студента")
        self.login_entry.pack(fill="x", padx=20, pady=(0, 10))
        self.login_entry.insert(0, env_login)

        # Пароль
        env_pw = os.getenv("MOODLE_PASSWORD", "")
        ctk.CTkLabel(self.left_panel, text="Пароль (из .env):", anchor="w").pack(fill="x", padx=20, pady=(5, 0))
        self.password_entry = ctk.CTkEntry(self.left_panel, show="*", placeholder_text="Пароль")
        self.password_entry.pack(fill="x", padx=20, pady=(0, 15))
        self.password_entry.insert(0, env_pw)

        # Курс
        ctk.CTkLabel(self.left_panel, text="Ссылка на курс:", anchor="w").pack(fill="x", padx=20, pady=(5, 0))
        history = self.config.get("course_history", [])
        self.course_combo = ctk.CTkComboBox(self.left_panel, values=history if history else [""])
        self.course_combo.pack(fill="x", padx=20, pady=(0, 15))
        if history:
            self.course_combo.set(history[0])

        # Скорость (Слайдер)
        speed_val = float(self.config.get("speed", 1.0))
        self.speed_label = ctk.CTkLabel(self.left_panel, text=f"Скорость: {speed_val:.1f}x", anchor="w")
        self.speed_label.pack(fill="x", padx=20, pady=(5, 0))
        self.speed_slider = ctk.CTkSlider(self.left_panel, from_=1.0, to=2.0, number_of_steps=10, command=self.update_speed_label)
        self.speed_slider.pack(fill="x", padx=20, pady=(0, 10))
        self.speed_slider.set(speed_val)

        # Фоновый режим
        self.headless_var = ctk.BooleanVar(value=self.config.get("headless", True))
        self.headless_check = ctk.CTkCheckBox(self.left_panel, text="Скрытый браузер (Фон)", variable=self.headless_var)
        self.headless_check.pack(fill="x", padx=20, pady=10)

        # Кнопки
        self.action_btn = ctk.CTkButton(self.left_panel, text="🔍 СКАНИРОВАТЬ КУРС", font=ctk.CTkFont(weight="bold"),
                                       command=self.start_scan, fg_color="#1f6aa5", hover_color="#144870", height=40)
        self.action_btn.pack(fill="x", padx=20, pady=(15, 5))

        self.stop_btn = ctk.CTkButton(self.left_panel, text="⏹ ОСТАНОВИТЬ", font=ctk.CTkFont(weight="bold"),
                                      command=self.stop_player, fg_color="#c23b22", hover_color="#8f2b19", state="disabled", height=40)
        self.stop_btn.pack(fill="x", padx=20, pady=5)


        # ==========================================
        # 2. ЦЕНТРАЛЬНАЯ ПАНЕЛЬ (Выбор лекций)
        # ==========================================
        self.center_panel = ctk.CTkFrame(self)
        self.center_panel.grid(row=0, column=1, padx=(10, 20), pady=(20, 10), sticky="nsew")
        
        ctk.CTkLabel(self.center_panel, text="📚 Выбор Лекций", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(15, 5))

        self.scroll_frame = ctk.CTkScrollableFrame(self.center_panel)
        self.scroll_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        self.empty_label = ctk.CTkLabel(self.scroll_frame, text="Введите ссылку и нажмите «Сканировать курс»...", text_color="gray")
        self.empty_label.pack(pady=50)


        # ==========================================
        # 3. НИЖНЯЯ ПАНЕЛЬ (Мониторинг)
        # ==========================================
        self.bottom_panel = ctk.CTkFrame(self)
        self.bottom_panel.grid(row=1, column=0, columnspan=2, padx=20, pady=(10, 20), sticky="nsew")
        self.bottom_panel.grid_columnconfigure(0, weight=1)
        self.bottom_panel.grid_rowconfigure(1, weight=1)

        # Прогресс бар текущего видео
        self.progress_label = ctk.CTkLabel(self.bottom_panel, text="Прогресс видео: (Ожидание)", anchor="w")
        self.progress_label.grid(row=0, column=0, padx=15, pady=(10, 0), sticky="w")
        
        self.progress_bar = ctk.CTkProgressBar(self.bottom_panel)
        self.progress_bar.grid(row=0, column=0, padx=15, pady=(10, 0), sticky="e")
        self.progress_bar.set(0)

        # Логи
        self.log_textbox = ctk.CTkTextbox(self.bottom_panel, state="disabled", fg_color="#1e1e1e", font=ctk.CTkFont(family="Courier", size=12))
        self.log_textbox.grid(row=1, column=0, padx=15, pady=(10, 15), sticky="nsew")

    def update_speed_label(self, val):
        self.speed_label.configure(text=f"Скорость: {val:.1f}x")

    def log(self, text):
        def update_log():
            import re
            clean_text = re.sub(r'\[.*?\]', '', text)
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", clean_text + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        self.after(0, update_log)

    def clear_scroll_frame(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

    def start_scan(self):
        login = self.login_entry.get().strip()
        pw = self.password_entry.get().strip()
        course = self.course_combo.get().strip()
        
        if not login or not pw or not course:
            self.log("❌ Заполните все поля (Логин, Пароль, Ссылка)!")
            return

        self.save_config()
        self.course_combo.configure(values=self.config["course_history"])

        self.action_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")
        
        self.clear_scroll_frame()
        self.empty_label = ctk.CTkLabel(self.scroll_frame, text="⏳ Запускаю браузер и сканирую курс...", text_color="cyan")
        self.empty_label.pack(pady=50)

        # Настраиваем коллбэки
        gui_engine.set_callbacks(self.log, self.update_progress, self.on_course_scanned)
        
        # Поток для Playwright
        self.worker_thread = threading.Thread(target=self.run_background_task, args=(login, pw, course))
        self.worker_thread.start()

    def run_background_task(self, login, pw, course):
        speed = float(self.speed_slider.get())
        headless = self.headless_var.get()
        
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p: pass
        except Exception:
            self.log("Скачиваем внутренний браузер Chromium (один раз)... Пожалуйста, подождите!")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)

        try:
            gui_engine.run_autoplayer(login, pw, course, headless=headless, speed=speed)
        except Exception as e:
            if "Остановлено" in str(e):
                self.log("⏹ Остановлено пользователем.")
            else:
                self.log(f"❌ Ошибка: {e}")
                
        self.after(0, self.reset_buttons)

    def on_course_scanned(self, weeks):
        # Коллбэк из фонового потока
        def build_ui():
            self.clear_scroll_frame()
            self.checkbox_vars.clear()

            if not weeks:
                lbl = ctk.CTkLabel(self.scroll_frame, text="❌ В курсе не найдено видео.", text_color="red")
                lbl.pack(pady=20)
                gui_engine.selected_lectures = []
                gui_engine.selection_event.set()
                return

            for w in weeks:
                week_lbl = ctk.CTkLabel(self.scroll_frame, text=w.name, font=ctk.CTkFont(weight="bold", size=14))
                week_lbl.pack(anchor="w", pady=(10, 5), padx=5)

                for lec in w.lectures:
                    var = ctk.BooleanVar(value=False) # По умолчанию галочки сняты
                    self.checkbox_vars[lec.title] = var
                    status = "✅" if lec.is_done else "⏳"
                    color = "gray" if lec.is_done else ("#ffffff" if ctk.get_appearance_mode() == "Dark" else "black")
                    
                    cb = ctk.CTkCheckBox(self.scroll_frame, text=f"{lec.title[:50]}... [{status}]", variable=var, text_color=color)
                    cb.pack(anchor="w", padx=20, pady=2)

            # Меняем кнопку на "Начать просмотр"
            self.action_btn.configure(
                text="▶ НАЧАТЬ ПРОСМОТР", 
                command=self.start_playing_selected,
                state="normal",
                fg_color="green", hover_color="darkgreen"
            )

        self.after(0, build_ui)

    def start_playing_selected(self):
        selected = [title for title, var in self.checkbox_vars.items() if var.get()]
        if not selected:
            self.log("⚠️ Выберите хотя бы одно видео!")
            return
            
        self.log(f"▶ Начинаем просмотр {len(selected)} видео...")
        self.action_btn.configure(text="⏳ В ПРОЦЕССЕ...", state="disabled", fg_color="#1f6aa5")
        
        gui_engine.selected_lectures = selected
        gui_engine.selection_event.set()

    def update_progress(self, title, current, total):
        def update():
            # Обновление бара в нижней панели
            self.progress_label.configure(text=f"Смотрим: {title[:40]}...")
            if total > 0:
                self.progress_bar.set(min(current / total, 1.0))
            else:
                self.progress_bar.set(0)
        self.after(0, update)

    def stop_player(self):
        self.log("⚠️ Остановка... (Браузер закроется в фоне)")
        gui_engine.is_running = False
        gui_engine.selection_event.set() 

    def reset_buttons(self):
        self.action_btn.configure(text="🔍 СКАНИРОВАТЬ КУРС", command=self.start_scan, state="normal", fg_color="#1f6aa5", hover_color="#144870")
        self.stop_btn.configure(state="disabled")
        self.progress_label.configure(text="Прогресс видео: (Ожидание)")
        self.progress_bar.set(0)

if __name__ == "__main__":
    app = MoodleAutoplayerGUI()
    app.mainloop()
