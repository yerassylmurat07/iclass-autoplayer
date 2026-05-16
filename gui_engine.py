#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════╗
║  🎬 iClass AutoPlayer v2.0 — RPA для автопросмотра видеолекций  ║
║                                                                   ║
║  Автологин (.env) → Меню по неделям → Автопросмотр с Анти-АФК   ║
╚═══════════════════════════════════════════════════════════════════╝

Использование:
    python autoplayer.py --course "https://learn.inha.ac.kr/course/view.php?id="
    python autoplayer.py --course "..." --speed 2.0

Настройка:
    Логин и пароль берутся из .env (MOODLE_LOGIN, MOODLE_PASSWORD).
"""

import argparse
import os
import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, Dialog, Frame



# GUI Callbacks
import logging
import threading

log_callback = print
progress_callback = lambda title, cur, tot: None
selection_callback = None
selection_event = threading.Event()
selected_lectures = []
is_running = True

def set_callbacks(log_cb, prog_cb, sel_cb=None):
    global log_callback, progress_callback, selection_callback
    log_callback = log_cb
    progress_callback = prog_cb
    selection_callback = sel_cb

def check_running():
    global is_running
    if not is_running:
        raise Exception("Остановлено пользователем")

# Загружаем .env
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ══════════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

_BASE_URL = os.getenv("MOODLE_URL", "https://learn.inha.ac.kr").rstrip("/")
LOGIN_URL = f"{_BASE_URL}/login/index.php"
COURSE_URL = f"{_BASE_URL}/course/view.php?id=XXXXX"

LOGIN_TIMEOUT = 60
POLL_INTERVAL = 10
PLAYBACK_SPEED = 1.0  # Moodle требует x1.0 для первого просмотра, иначе поставит ❌
VIEWPORT = {"width": 1280, "height": 720}

# CSS-селекторы для полей логина (из src/auth.py)
LOGIN_SELECTORS = {
    "username": ['#username', 'input[name="username"]', 'input[name="id"]',
                 'input[type="text"]'],
    "password": ['#password', 'input[name="password"]', 'input[type="password"]'],
    "submit":   ['#loginbtn', 'button[type="submit"]', 'input[type="submit"]',
                 'button:has-text("Log in")', 'button:has-text("로그인")'],
}


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LectureInfo:
    title: str
    url: str
    week: str = ""
    is_done: bool = False

@dataclass
class WeekSection:
    name: str
    lectures: list[LectureInfo] = field(default_factory=list)

@dataclass
class PlaybackResult:
    title: str
    success: bool
    duration_watched: float = 0.0
    error: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# АНТИ-АФК
# ══════════════════════════════════════════════════════════════════════════════

def _handle_dialog(dialog: Dialog) -> None:
    log_callback(f"  🛡️  [yellow]Анти-АФК:[/yellow] «{dialog.message[:50]}» → [green]OK[/green]")
    dialog.accept()


# ══════════════════════════════════════════════════════════════════════════════
# АВТОЛОГИН ИЗ .ENV
# ══════════════════════════════════════════════════════════════════════════════

def auto_login(page: Page, login: str, password: str) -> bool:
    """Автоматический логин."""
    if not login or not password:
        log_callback("[bold red]❌ Логин и пароль не заданы[/bold red]")
        return False


    if not login or not password:
        log_callback("[bold red]❌ MOODLE_LOGIN и MOODLE_PASSWORD не заданы в .env[/bold red]")
        return False

    log_callback(f"  🔐 Автологин как [cyan]{login}[/cyan]...")

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        log_callback(f"  ❌ [red]Не удалось открыть {LOGIN_URL}: {e}[/red]")
        return False

    page.wait_for_timeout(2000)

    # Заполняем username
    filled_user = False
    for sel in LOGIN_SELECTORS["username"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.fill(login)
                filled_user = True
                break
        except Exception:
            continue
    if not filled_user:
        log_callback("  ❌ Поле логина не найдено")
        return False

    # Заполняем password
    filled_pw = False
    for sel in LOGIN_SELECTORS["password"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.fill(password)
                filled_pw = True
                break
        except Exception:
            continue
    if not filled_pw:
        log_callback("  ❌ Поле пароля не найдено")
        return False

    # Submit
    clicked = False
    for sel in LOGIN_SELECTORS["submit"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        page.keyboard.press("Enter")

    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(2000)

    # Проверяем URL после попытки входа
    url = page.url.lower()
    if "login" in url or "failed" in url or "error" in url:
        log_callback("  ❌ Ошибка входа! Убедитесь, что пароль правильный.")
        log_callback("  💡 Совет: отключите галочку 'Фоновый режим', чтобы увидеть браузер и понять проблему.")
        return False

    log_callback("  ✅ Авторизация успешна!")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# СКАНИРОВАНИЕ КУРСА ПО НЕДЕЛЯМ
# ══════════════════════════════════════════════════════════════════════════════

def scan_course(page: Page, course_url: str) -> list[WeekSection]:
    """
    Сканирует курс и возвращает лекции, сгруппированные по неделям/разделам.
    """
    log_callback(f"\n📡 Переходим по ссылке курса...")
    page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)
    
    log_callback(f"🔎 Открыта страница: {page.title()[:40]}...")
    log_callback(f"🔗 Текущий URL: {page.url}")

    weeks: list[WeekSection] = []

    # Moodle/Coursemos: секции = <li class="section">
    section_selectors = [
        'li.section.main',
        '.course-section',
        'ul.topics > li',
        'ul.weeks > li',
        '.section.main',
    ]

    sections = []
    for sel in section_selectors:
        sections = page.locator(sel).all()
        if sections:
            break

    if not sections:
        # Fallback: парсим всю страницу как одну секцию
        log_callback("  ⚠️  Секции не найдены, сканируем всю страницу...")
        week = _scan_section_for_videos(page, page.locator("body"), "Все лекции")
        if week.lectures:
            weeks.append(week)
        return weeks

    for section in sections:
        # Извлекаем название секции (недели)
        week_name = ""
        name_selectors = [
            '.sectionname', '.section-title', 'h3.sectionname',
            '.content > h3', '.course-section-header',
        ]
        for ns in name_selectors:
            try:
                name_el = section.locator(ns).first
                text = name_el.text_content(timeout=1000)
                if text and text.strip():
                    week_name = text.strip()[:60]
                    break
            except Exception:
                continue

        if not week_name:
            # Пытаемся достать aria-label
            try:
                week_name = section.get_attribute("aria-label", timeout=500) or ""
                week_name = week_name.strip()[:60]
            except Exception:
                pass

        if not week_name:
            week_name = f"Раздел {len(weeks) + 1}"

        week = _scan_section_for_videos(page, section, week_name)
        if week.lectures:
            weeks.append(week)

    return weeks


def _scan_section_for_videos(page: Page, container, week_name: str) -> WeekSection:
    """Ищет видео-активности внутри одной секции."""
    week = WeekSection(name=week_name)

    # Ищем активности внутри секции
    activity_selectors = [
        'li.activity a.aalink',
        'li.activity a.activityname',
        'li.activity a[href*="view.php"]',
        '.activity-item a[href*="view.php"]',
    ]

    seen = set()
    for sel in activity_selectors:
        try:
            elements = container.locator(sel).all()
        except Exception:
            continue

        for el in elements:
            try:
                href = el.get_attribute("href", timeout=1000)
                text = (el.text_content(timeout=1000) or "").strip()

                if not href or href in seen or not text:
                    continue
                seen.add(href)

                # Определяем статус завершения
                is_done = False
                try:
                    parent = el.locator("xpath=ancestor::li[contains(@class,'activity')]").first
                    done_sels = [
                        'img[src*="completion-auto-y"]',
                        'img[src*="completion-manual-y"]',
                        '.completion-icon.is-done',
                        'img[title*="완료"]',
                        'img[title*="Completed"]',
                        '.autocompletion-view img[alt*="완료"]',
                    ]
                    for ds in done_sels:
                        if parent.locator(ds).count() > 0:
                            is_done = True
                            break
                except Exception:
                    pass

                full_url = href if href.startswith("http") else urljoin(page.url, href)

                week.lectures.append(LectureInfo(
                    title=text[:70],
                    url=full_url,
                    week=week_name,
                    is_done=is_done,
                ))
            except Exception:
                continue

    return week


# ══════════════════════════════════════════════════════════════════════════════
# ИНТЕРАКТИВНОЕ МЕНЮ
# ══════════════════════════════════════════════════════════════════════════════

def show_menu(weeks: list[WeekSection]) -> list[LectureInfo]:
    """
    Показывает интерактивное меню с выбором по неделям.

    Returns:
        list: выбранные лекции для просмотра
    """
    # Общая статистика
    total = sum(len(w.lectures) for w in weeks)
    done = sum(1 for w in weeks for l in w.lectures if l.is_done)
    undone = total - done

    console.print(Panel.fit(
        f"📊 Всего: [bold]{total}[/bold] | "
        f"✅ Просмотрено: [green]{done}[/green] | "
        f"⏳ Осталось: [yellow]{undone}[/yellow]",
        border_style="blue",
    ))

    # Таблица недель
    table = Table(title="📚 Разделы курса", show_lines=True)
    table.add_column("#", justify="right", width=4)
    table.add_column("Раздел", max_width=50)
    table.add_column("Всего", justify="center", width=6)
    table.add_column("⏳", justify="center", width=6)
    table.add_column("✅", justify="center", width=6)

    for i, w in enumerate(weeks, 1):
        w_done = sum(1 for l in w.lectures if l.is_done)
        w_undone = len(w.lectures) - w_done
        style = "dim" if w_undone == 0 else ""
        table.add_row(str(i), w.name, str(len(w.lectures)),
                       str(w_undone), str(w_done), style=style)

    log_callback(table)

    # Меню выбора
    log_callback("\n[bold]Выбери действие:[/bold]")
    log_callback("  [cyan]0[/cyan] — 🚀 Смотреть ВСЕ непросмотренные")
    for i, w in enumerate(weeks, 1):
        w_undone = sum(1 for l in w.lectures if not l.is_done)
        marker = "✅" if w_undone == 0 else f"⏳{w_undone}"
        log_callback(f"  [cyan]{i}[/cyan] — {w.name} [{marker}]")
    log_callback(f"  [cyan]q[/cyan] — Выход")

    choice = Prompt.ask("\n👉 Твой выбор", default="0")

    if choice.lower() == "q":
        log_callback("[yellow]👋 Выход[/yellow]")
        sys.exit(0)

    if choice == "0":
        # Все непросмотренные
        selected = [l for w in weeks for l in w.lectures if not l.is_done]
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(weeks):
                week = weeks[idx]
                # Показываем лекции в выбранной неделе
                selected = _show_week_detail(week)
            else:
                log_callback("[red]Неверный номер[/red]")
                return show_menu(weeks)
        except ValueError:
            log_callback("[red]Введи номер[/red]")
            return show_menu(weeks)

    if not selected:
        log_callback("[yellow]Нет лекций для просмотра[/yellow]")
        return []

    log_callback(f"\n🎯 Выбрано для просмотра: [bold cyan]{len(selected)}[/bold cyan] видео")
    return selected


def _show_week_detail(week: WeekSection) -> list[LectureInfo]:
    """Показывает лекции внутри одной недели с возможностью выбора."""
    log_callback(f"\n📂 [bold]{week.name}[/bold]")

    table = Table(show_lines=True)
    table.add_column("#", justify="right", width=4)
    table.add_column("Лекция", max_width=55)
    table.add_column("Статус", justify="center", width=10)

    for i, lec in enumerate(week.lectures, 1):
        status = "✅" if lec.is_done else "⏳"
        style = "dim" if lec.is_done else ""
        table.add_row(str(i), lec.title, status, style=style)

    log_callback(table)

    log_callback("\n[bold]Выбери:[/bold]")
    log_callback("  [cyan]0[/cyan] — Все непросмотренные из этого раздела")
    log_callback("  [cyan]a[/cyan] — Все (включая просмотренные)")
    log_callback("  [cyan]1,3,5[/cyan] — Конкретные номера через запятую")
    log_callback("  [cyan]b[/cyan] — Назад")

    choice = Prompt.ask("👉", default="0")

    if choice.lower() == "b":
        return []
    if choice.lower() == "a":
        return week.lectures[:]
    if choice == "0":
        return [l for l in week.lectures if not l.is_done]

    # Конкретные номера
    try:
        indices = [int(x.strip()) - 1 for x in choice.split(",")]
        return [week.lectures[i] for i in indices if 0 <= i < len(week.lectures)]
    except (ValueError, IndexError):
        log_callback("[red]Неверный ввод[/red]")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# ПРОСМОТР ВИДЕО
# ══════════════════════════════════════════════════════════════════════════════

def _find_video_frame(page: Page, debug: bool = False) -> Frame | None:
    """
    Агрессивный поиск фрейма с <video>.
    Coursemos прячет видео в глубоко вложенные iframe.
    """
    # 1. Проверяем главную страницу
    if page.locator("video").count() > 0:
        if debug:
            log_callback("     🔍 <video> найден в main frame")
        return page.main_frame

    # 2. Проверяем ВСЕ фреймы (включая вложенные)
    for frame in page.frames:
        try:
            name = frame.name or frame.url[:60]
            if frame.locator("video").count() > 0:
                if debug:
                    log_callback(f"     🔍 <video> найден в frame: [dim]{name}[/dim]")
                return frame
        except Exception:
            continue

    if debug:
        log_callback(f"     🔍 Всего фреймов: {len(page.frames)}")
        for i, f in enumerate(page.frames):
            try:
                url = f.url[:80] if f.url else "(empty)"
                log_callback(f"        frame[{i}]: {url}")
            except Exception:
                pass

    return None


def _wait_for_video(page: Page, timeout: int = 30) -> Frame | None:
    """
    Ждёт появления <video> элемента до timeout секунд.
    Использует Playwright native wait_for_selector — он отслеживает
    динамически создаваемые элементы (как VideoJS).
    """
    log_callback(f"     ⏳ Ищем видеоплеер (до {timeout}с)...")

    # 1. Пробуем Playwright native wait (лучше для динамических элементов)
    for frame in [page.main_frame] + page.frames:
        try:
            frame.wait_for_selector("video", timeout=timeout * 1000)
            log_callback(f"     ✅ <video> появился в DOM!")
            return frame
        except Exception:
            continue

    # 2. Fallback: ручной поиск
    frame = _find_video_frame(page, debug=True)
    if frame:
        return frame

    return None


def _try_activate_player(page: Page) -> None:
    """
    Пытается активировать плеер — кликает по кнопкам запуска,
    открытия viewer, и другим элементам Coursemos/Moodle.
    """
    # Coursemos / iClass специфичные кнопки
    buttons = [
        # Кнопки открытия/запуска
        'a:has-text("열기")',             # "Открыть" (корейский)
        'a:has-text("강의시작")',          # "Начать лекцию"
        'a:has-text("학습하기")',          # "Учиться"
        'a:has-text("Launch")',
        'a:has-text("View")',
        'a:has-text("Enter")',
        # Moodle стандарт
        '.resourceworkaround a',
        '#resourceobject',
        # Кнопки play внутри плеера
        '.vjs-big-play-button',
        '.ytp-large-play-button',
        'button.play-button',
        'button[aria-label="Play"]',
        'button[aria-label="재생"]',       # "Воспроизведение"
        # Общие
        '.video-play-btn',
        '.btn-play',
        '[class*="play"]',
    ]

    for sel in buttons:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                log_callback(f"     🖱️  Кликаем: [dim]{sel}[/dim]")
                loc.click()
                page.wait_for_timeout(3000)
                return
        except Exception:
            continue

    # Пробуем кликнуть в iframe-ах тоже
    for frame in page.frames:
        for sel in ['.vjs-big-play-button', 'button[aria-label="Play"]',
                    'button[aria-label="재생"]', '.play-button', 'video']:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=500):
                    log_callback(f"     🖱️  Кликаем в iframe: [dim]{sel}[/dim]")
                    loc.click()
                    page.wait_for_timeout(3000)
                    return
            except Exception:
                continue


def play_video(page: Page, lecture: LectureInfo, speed: float) -> PlaybackResult:
    """Открывает, запускает и ждёт окончания одного видео."""
    log_callback(f"\n  📖 [bold]{lecture.title}[/bold]")
    
    # ── 1. Определяем правильный URL ──────────────────────────────────────
    # Если ссылка ведет на view.php, нам на самом деле нужно на viewer.php
    target_url = lecture.url
    if "mod/vod/view.php" in target_url:
        target_url = target_url.replace("view.php", "viewer.php")
        log_callback(f"     🔄 Перенаправление: [dim]view.php → viewer.php[/dim]")

    try:
        # Переходим на страницу лекции
        page.goto(target_url, wait_until="networkidle", timeout=60_000)
        log_callback("     📄 Страница загружена")

        # Проверяем, не остались ли мы на странице с кнопкой "Watch VOD"
        if page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').count() > 0:
            log_callback("     🖱️ Найдена кнопка запуска, нажимаем...")
            try:
                # Пытаемся нажать, но если это window.open, ловим новую страницу
                with page.expect_popup(timeout=5000) as popup_info:
                    page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').first.click()
                page = popup_info.value
                page.wait_for_load_state("networkidle")
                log_callback("     ✨ Переключились на окно плеера")
            except Exception:
                # Если попапа нет, просто идем по ссылке
                btn_href = page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').first.get_attribute("href")
                if btn_href:
                    page.goto(urljoin(page.url, btn_href), wait_until="networkidle")

        # ── 2. Ищем видео ────────────────────────────────────────────────
        video_frame = _wait_for_video(page, timeout=20)

        if not video_frame:
            log_callback("     🔄 Пробуем активировать плеер...")
            _try_activate_player(page)
            video_frame = _wait_for_video(page, timeout=15)

        # ── Попытка 3: может быть popup / новое окно ─────────────────────
        if not video_frame:
            # Проверяем, не открылась ли новая вкладка
            all_pages = page.context.pages
            if len(all_pages) > 1:
                new_page = all_pages[-1]
                log_callback("     🔄 Обнаружена новая вкладка, переключаемся...")
                new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                new_page.on("dialog", _handle_dialog)
                video_frame = _wait_for_video(new_page, timeout=15)
                if video_frame:
                    # Переключаемся на новую страницу для дальнейшей работы
                    page = new_page

        # ── Не нашли — показываем отладку ─────────────────────────────────
        if not video_frame:
            log_callback("     ❌ [red]Видео не найдено[/red]")
            log_callback("     🔍 [bold]Отладка — что на странице:[/bold]")
            log_callback(f"        URL: {page.url}")
            log_callback(f"        Фреймов: {len(page.frames)}")

            # Дамп всех ссылок, кнопок и ключевых элементов
            try:
                dump = page.evaluate("""(() => {
                    const info = {};

                    // Все ссылки
                    info.links = [...document.querySelectorAll('a[href]')].slice(0, 15).map(a => ({
                        text: a.textContent.trim().substring(0, 50),
                        href: a.href.substring(0, 100),
                        onclick: a.getAttribute('onclick')?.substring(0, 80) || ''
                    }));

                    // Все кнопки
                    info.buttons = [...document.querySelectorAll('button, input[type="button"], input[type="submit"]')].map(b => ({
                        text: (b.textContent || b.value || '').trim().substring(0, 50),
                        class: b.className?.substring(0, 60) || '',
                        onclick: b.getAttribute('onclick')?.substring(0, 80) || ''
                    }));

                    // Все iframe
                    info.iframes = [...document.querySelectorAll('iframe')].map(f => ({
                        src: f.src?.substring(0, 120) || '(empty)',
                        id: f.id || '',
                        class: f.className || ''
                    }));

                    // Все object/embed
                    info.objects = [...document.querySelectorAll('object, embed')].map(o => ({
                        data: o.data?.substring(0, 100) || o.src?.substring(0, 100) || '',
                        type: o.type || ''
                    }));

                    // Ключевые div-ы (player, video, vod)
                    info.playerDivs = [...document.querySelectorAll('[class*="player"], [class*="video"], [class*="vod"], [id*="player"], [id*="video"]')].map(d => ({
                        tag: d.tagName,
                        id: d.id || '',
                        class: d.className?.substring(0, 80) || ''
                    }));

                    // window.open или popup скрипты — ПОЛНЫЙ текст
                    const scripts = [...document.querySelectorAll('script')];
                    info.scriptHints = scripts
                        .map(s => s.textContent)
                        .filter(t => t && (t.includes('window.open') || t.includes('player') || t.includes('video') || t.includes('popup') || t.includes('vod')))
                        .map(t => t.substring(0, 800));

                    // VOD info содержимое
                    info.vodInfo = [...document.querySelectorAll('.vod_info, .vod_info_value, [class*="vod"]')]
                        .map(d => ({tag: d.tagName, class: d.className, text: d.innerText?.substring(0, 100) || '', html: d.innerHTML?.substring(0, 200) || ''}));

                    // Основной контент
                    const mainContent = document.querySelector('#region-main, .course-content, [role="main"], #maincontent, .generalbox');
                    info.mainHTML = mainContent ? mainContent.innerHTML.substring(0, 2000) : document.body.innerHTML.substring(0, 2000);

                    // Текст страницы
                    info.bodyText = document.body?.innerText?.substring(0, 500) || '';

                    return info;
                })()""")

                if dump.get("links"):
                    log_callback("\n        [cyan]🔗 Ссылки:[/cyan]")
                    for l in dump["links"]:
                        extra = f" onclick={l['onclick']}" if l.get('onclick') else ""
                        log_callback(f"           {l['text'][:40]} → [dim]{l['href'][:70]}{extra}[/dim]")

                if dump.get("buttons"):
                    log_callback("\n        [cyan]🔘 Кнопки:[/cyan]")
                    for b in dump["buttons"]:
                        log_callback(f"           «{b['text'][:40]}» class=[dim]{b['class'][:50]}[/dim]")

                if dump.get("iframes"):
                    log_callback("\n        [cyan]📦 iframes:[/cyan]")
                    for f in dump["iframes"]:
                        log_callback(f"           src=[dim]{f['src']}[/dim]")

                if dump.get("objects"):
                    log_callback("\n        [cyan]🎞️ object/embed:[/cyan]")
                    for o in dump["objects"]:
                        log_callback(f"           {o['type']} → [dim]{o['data']}[/dim]")

                if dump.get("playerDivs"):
                    log_callback("\n        [cyan]🎬 Player divs:[/cyan]")
                    for d in dump["playerDivs"]:
                        log_callback(f"           <{d['tag']}> id={d['id']} class=[dim]{d['class']}[/dim]")

                if dump.get("scriptHints"):
                    log_callback("\n        [cyan]📜 Scripts (full):[/cyan]")
                    for s in dump["scriptHints"][:5]:
                        log_callback(f"           [dim]{s[:500]}[/dim]")

                if dump.get("vodInfo"):
                    log_callback("\n        [cyan]📹 VOD Info divs:[/cyan]")
                    for v in dump["vodInfo"]:
                        log_callback(f"           <{v['tag']}> .{v['class']} text=«{v['text'][:60]}»")
                        if v.get("html"):
                            log_callback(f"              html=[dim]{v['html'][:150]}[/dim]")

                if dump.get("mainHTML"):
                    log_callback(f"\n        [cyan]📄 Main region HTML (first 1500 chars):[/cyan]")
                    log_callback(f"           [dim]{dump['mainHTML'][:1500]}[/dim]")

            except Exception as e:
                log_callback(f"        Ошибка дампа: {e}")

            return PlaybackResult(lecture.title, False, error="No <video> found")

        log_callback("     🎥 [green]Видео найдено![/green]")

        # ── Запуск воспроизведения ────────────────────────────────────────
        video_frame.evaluate(f"""(() => {{
            const v = document.querySelector('video');
            if (v) {{
                v.muted = false;
                v.volume = 0.1;
                v.playbackRate = {speed};
                v.play().catch(() => {{}});
            }}
        }})()""")
        page.wait_for_timeout(2000)

        # Проверяем играет ли
        is_playing = video_frame.evaluate("""(() => {
            const v = document.querySelector('video');
            return v && !v.paused && !v.ended && v.readyState > 2;
        })()""")

        if not is_playing:
            log_callback("     ⏯️  Не играет, пробуем кликнуть по video...")
            try:
                video_frame.locator("video").first.click(timeout=3000)
                page.wait_for_timeout(2000)
                video_frame.evaluate(f"""(() => {{
                    const v = document.querySelector('video');
                    if (v) {{ v.playbackRate = {speed}; v.play().catch(()=>{{}}); }}
                }})()""")
            except Exception:
                pass

        # Получаем длительность
        duration = video_frame.evaluate("""(() => {
            const v = document.querySelector('video');
            return v ? v.duration : 0;
        })()""") or 0

        if duration <= 0 or duration != duration:  # NaN check
            log_callback("     ⏳ Ждём загрузки метаданных видео...")
            for _ in range(10):
                page.wait_for_timeout(2000)
                duration = video_frame.evaluate("""(() => {
                    const v = document.querySelector('video');
                    return v && v.duration && isFinite(v.duration) ? v.duration : 0;
                })()""") or 0
                if duration > 0:
                    break

        dur_min = duration / 60
        eff_min = dur_min / speed if speed > 0 else dur_min
        log_callback(f"     ▶️  Длительность: {dur_min:.1f} мин (x{speed} ≈ {eff_min:.1f} мин)")

        # ── Smart Polling: ждём окончания ─────────────────────────────────
        start = time.time()
        # Безопасный лимит ожидания: оригинальная длительность + 30 минут запаса.
        # Это спасет от раннего выхода, если Moodle сбросил скорость до x1.0.
        max_wait = (duration + 1800) if duration > 0 else 7200  
        stall = 0
        last_t = 0.0

        while time.time() - start < max_wait:
                check_running()
                try:
                    st = video_frame.evaluate("""(() => {
                        const v = document.querySelector('video');
                        if (!v) return {ended:true, current:0, duration:0, paused:true, rate:1.0};
                        return {ended:v.ended, current:v.currentTime,
                                duration:v.duration, paused:v.paused, rate:v.playbackRate};
                    })()""")
                except Exception:
                    video_frame = _find_video_frame(page)
                    if not video_frame:
                        break
                    page.wait_for_timeout(3000)
                    continue

                cur = st.get("current", 0)

                # Если видео закончилось или осталось меньше секунды
                if st.get("ended") or (duration > 0 and cur >= duration - 1.0):
                    progress_callback(lecture.title, duration, duration)
                    log_callback("\n     ⏳ Досмотр: ожидание 15 секунд для сохранения статистики в LMS...")
                    page.wait_for_timeout(15000)
                    break

                progress_callback(lecture.title, cur, duration)

                # Постоянный контроль скорости (некоторые LMS сбрасывают её на x1.0)
                if abs(st.get("rate", 1.0) - speed) > 0.1 and not st.get("ended"):
                    try:
                        video_frame.evaluate(f"""(() => {{
                            const v = document.querySelector('video');
                            if(v){{ v.playbackRate={speed}; }}
                        }})()""")
                    except Exception:
                        pass

                # Авто-unpause
                if st.get("paused"):
                    try:
                        video_frame.evaluate(f"""(() => {{
                            const v = document.querySelector('video');
                            if(v){{ v.play().catch(()=>{{}}); v.playbackRate={speed}; }}
                        }})()""")
                    except Exception:
                        pass

                # Anti-stall (КРИТИЧНО: без пропуска секунд, иначе Moodle ставит крестик)
                if abs(cur - last_t) < 0.5:
                    stall += 1
                    if stall > 5:
                        try:
                            video_frame.evaluate(f"""(() => {{
                                const v = document.querySelector('video');
                                if(v){{ v.play().catch(()=>{{}}); v.playbackRate={speed}; }}
                            }})()""")
                        except Exception:
                            pass
                        stall = 0
                else:
                    stall = 0
                last_t = cur

                page.wait_for_timeout(POLL_INTERVAL * 1000)

        elapsed = time.time() - start
        log_callback(f"     ✅ [green]Готово за {elapsed/60:.1f} мин[/green]")
        return PlaybackResult(lecture.title, True, elapsed)

    except Exception as e:
        log_callback(f"     ❌ [red]{e}[/red]")
        return PlaybackResult(lecture.title, False, error=str(e)[:50])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="🎬 iClass AutoPlayer v2.0")
    p.add_argument("--course", default=COURSE_URL, help="URL курса")
    p.add_argument("--speed", type=float, default=PLAYBACK_SPEED, help="Скорость (default: 2.0)")
    return p.parse_args()



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
            
            # --- ИНТЕРАКТИВНЫЙ ВЫБОР ---
            if selection_callback:
                selection_event.clear()
                selection_callback(weeks)
                log_callback("⏳ Ожидание выбора пользователя в интерфейсе...")
                
                # Ждем пока пользователь нажмет кнопку в GUI
                selection_event.wait()
                
                if not is_running:
                    raise Exception("Остановлено пользователем")
                
                # Фильтруем только выбранные заголовки
                to_watch_titles = selected_lectures
            else:
                to_watch_titles = [l.title for w in weeks for l in w.lectures if not l.is_done]
            
            total_watched = 0
            for w in weeks:
                for lec in w.lectures:
                    if lec.title in to_watch_titles:
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
