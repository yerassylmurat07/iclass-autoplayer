import re

with open('gui_engine.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Remove rich imports
code = re.sub(r'from rich.*?import.*?\n', '', code)
code = re.sub(r'console = Console\(\)\n', '', code)

# 2. Add callback globals
callbacks = """
# GUI Callbacks
import logging
log_callback = print
progress_callback = lambda title, cur, tot: None
is_running = True

def set_callbacks(log_cb, prog_cb):
    global log_callback, progress_callback
    log_callback = log_cb
    progress_callback = prog_cb

def check_running():
    global is_running
    if not is_running:
        raise Exception("Остановлено пользователем")
"""
code = re.sub(r'# Загружаем \.env', callbacks + '\n# Загружаем .env', code, count=1)

# 3. Replace console.print
code = re.sub(r'console\.print\((.*?)\)', r'log_callback(\1)', code)

# 4. Remove rich Progress context manager in play_video
# Find the play_video progress block
prog_pattern = r'with Progress\(.*?\n\s+console=console\) as prog:\n\s+task = prog\.add_task.*?total=max\(duration, 1\)\)\n\n\s+while time\.time\(\) - start < max_wait:'
code = re.sub(prog_pattern, r'while time.time() - start < max_wait:\n                check_running()', code, flags=re.DOTALL)

# Replace prog.update with progress_callback
code = re.sub(r'prog\.update\(task, completed=(.*?)\)', r'progress_callback(lecture.title, \1, duration)', code)

# 5. Remove interactive menu, just return all undone
menu_pattern = r'def interactive_menu.*?return \[\]\n\n'
new_menu = """def interactive_menu(week):
    return [l for l in week.lectures if not l.is_done]
"""
code = re.sub(r'def interactive_menu.*?return \[\]\n\n', new_menu, code, flags=re.DOTALL)

# 6. Make auto_login accept credentials
login_func_pattern = r'def auto_login\(page: Page\) -> bool:\n.*?"""\n.*?login = os\.getenv\("MOODLE_LOGIN", ""\)\.strip\(\)\n.*?password = os\.getenv\("MOODLE_PASSWORD", ""\)\.strip\(\)'
new_login_func = """def auto_login(page: Page, login: str, password: str) -> bool:
    \"\"\"Автоматический логин.\"\"\"
    if not login or not password:
        log_callback("[bold red]❌ Логин и пароль не заданы[/bold red]")
        return False
"""
code = re.sub(login_func_pattern, new_login_func, code, flags=re.DOTALL)

# 7. Modify main to run from GUI
main_pattern = r'def main\(\):.*'
new_main = """
def run_autoplayer(login, password, course_url, headless=True, speed=1.0):
    global is_running
    is_running = True
    
    log_callback(f"🚀 Запуск движка... (Курс: {course_url})")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless, args=["--mute-audio"])
            context = browser.new_context(viewport=VIEWPORT)
            page = context.new_page()
            page.on("dialog", _handle_dialog)

            if not auto_login(page, login, password):
                return False

            page.goto(course_url, wait_until="domcontentloaded")
            weeks = scan_course(page, course_url)
            
            total_watched = 0
            for w in weeks:
                to_watch = [l for l in w.lectures if not l.is_done]
                for lec in to_watch:
                    check_running()
                    res = play_video(page, lec, speed=speed)
                    if res.success:
                        total_watched += 1
                    try:
                        page.goto(course_url, wait_until="domcontentloaded")
                    except:
                        pass
            
            log_callback(f"🎉 Все видео проверены! Посмотрено: {total_watched}")
            return True
    except Exception as e:
        log_callback(f"❌ Ошибка движка: {e}")
        return False
"""
code = re.sub(main_pattern, new_main, code, flags=re.DOTALL)

with open('gui_engine.py', 'w', encoding='utf-8') as f:
    f.write(code)

