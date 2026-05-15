#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════╗
║  🎬 iClass AutoPlayer v2.0 — RPA для автопросмотра видеолекций  ║
║                                                                   ║
║  Автологин (.env) → Меню по неделям → Автопросмотр с Анти-АФК   ║
╚═══════════════════════════════════════════════════════════════════╝

Использование:
    python autoplayer.py --course "https://learn.inha.ac.kr/course/view.php?id=70982"
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
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

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
    console.print(f"  🛡️  [yellow]Анти-АФК:[/yellow] «{dialog.message[:50]}» → [green]OK[/green]")
    dialog.accept()


# ══════════════════════════════════════════════════════════════════════════════
# АВТОЛОГИН ИЗ .ENV
# ══════════════════════════════════════════════════════════════════════════════

def auto_login(page: Page) -> bool:
    """Автоматический логин используя MOODLE_LOGIN и MOODLE_PASSWORD из .env."""
    login = os.getenv("MOODLE_LOGIN", "").strip()
    password = os.getenv("MOODLE_PASSWORD", "").strip()

    if not login or not password:
        console.print("[bold red]❌ MOODLE_LOGIN и MOODLE_PASSWORD не заданы в .env[/bold red]")
        return False

    console.print(f"  🔐 Автологин как [cyan]{login}[/cyan]...")

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        console.print(f"  ❌ [red]Не удалось открыть {LOGIN_URL}: {e}[/red]")
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
        console.print("  ❌ Поле логина не найдено")
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
        console.print("  ❌ Поле пароля не найдено")
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

    # Проверяем
    url = page.url.lower()
    if "/login" in url and ("index.php" in url or "failed" in url):
        console.print("  ❌ [red]Логин или пароль неверные![/red]")
        return False

    console.print("  ✅ [green]Авторизация успешна![/green]")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# СКАНИРОВАНИЕ КУРСА ПО НЕДЕЛЯМ
# ══════════════════════════════════════════════════════════════════════════════

def scan_course(page: Page, course_url: str) -> list[WeekSection]:
    """
    Сканирует курс и возвращает лекции, сгруппированные по неделям/разделам.
    """
    console.print(f"\n📡 Сканируем курс...")
    page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)

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
        console.print("  ⚠️  Секции не найдены, сканируем всю страницу...")
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

    console.print(table)

    # Меню выбора
    console.print("\n[bold]Выбери действие:[/bold]")
    console.print("  [cyan]0[/cyan] — 🚀 Смотреть ВСЕ непросмотренные")
    for i, w in enumerate(weeks, 1):
        w_undone = sum(1 for l in w.lectures if not l.is_done)
        marker = "✅" if w_undone == 0 else f"⏳{w_undone}"
        console.print(f"  [cyan]{i}[/cyan] — {w.name} [{marker}]")
    console.print(f"  [cyan]q[/cyan] — Выход")

    choice = Prompt.ask("\n👉 Твой выбор", default="0")

    if choice.lower() == "q":
        console.print("[yellow]👋 Выход[/yellow]")
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
                console.print("[red]Неверный номер[/red]")
                return show_menu(weeks)
        except ValueError:
            console.print("[red]Введи номер[/red]")
            return show_menu(weeks)

    if not selected:
        console.print("[yellow]Нет лекций для просмотра[/yellow]")
        return []

    console.print(f"\n🎯 Выбрано для просмотра: [bold cyan]{len(selected)}[/bold cyan] видео")
    return selected


def _show_week_detail(week: WeekSection) -> list[LectureInfo]:
    """Показывает лекции внутри одной недели с возможностью выбора."""
    console.print(f"\n📂 [bold]{week.name}[/bold]")

    table = Table(show_lines=True)
    table.add_column("#", justify="right", width=4)
    table.add_column("Лекция", max_width=55)
    table.add_column("Статус", justify="center", width=10)

    for i, lec in enumerate(week.lectures, 1):
        status = "✅" if lec.is_done else "⏳"
        style = "dim" if lec.is_done else ""
        table.add_row(str(i), lec.title, status, style=style)

    console.print(table)

    console.print("\n[bold]Выбери:[/bold]")
    console.print("  [cyan]0[/cyan] — Все непросмотренные из этого раздела")
    console.print("  [cyan]a[/cyan] — Все (включая просмотренные)")
    console.print("  [cyan]1,3,5[/cyan] — Конкретные номера через запятую")
    console.print("  [cyan]b[/cyan] — Назад")

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
        console.print("[red]Неверный ввод[/red]")
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
            console.print("     🔍 <video> найден в main frame")
        return page.main_frame

    # 2. Проверяем ВСЕ фреймы (включая вложенные)
    for frame in page.frames:
        try:
            name = frame.name or frame.url[:60]
            if frame.locator("video").count() > 0:
                if debug:
                    console.print(f"     🔍 <video> найден в frame: [dim]{name}[/dim]")
                return frame
        except Exception:
            continue

    if debug:
        console.print(f"     🔍 Всего фреймов: {len(page.frames)}")
        for i, f in enumerate(page.frames):
            try:
                url = f.url[:80] if f.url else "(empty)"
                console.print(f"        frame[{i}]: {url}")
            except Exception:
                pass

    return None


def _wait_for_video(page: Page, timeout: int = 30) -> Frame | None:
    """
    Ждёт появления <video> элемента до timeout секунд.
    Использует Playwright native wait_for_selector — он отслеживает
    динамически создаваемые элементы (как VideoJS).
    """
    console.print(f"     ⏳ Ищем видеоплеер (до {timeout}с)...")

    # 1. Пробуем Playwright native wait (лучше для динамических элементов)
    for frame in [page.main_frame] + page.frames:
        try:
            frame.wait_for_selector("video", timeout=timeout * 1000)
            console.print(f"     ✅ <video> появился в DOM!")
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
                console.print(f"     🖱️  Кликаем: [dim]{sel}[/dim]")
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
                    console.print(f"     🖱️  Кликаем в iframe: [dim]{sel}[/dim]")
                    loc.click()
                    page.wait_for_timeout(3000)
                    return
            except Exception:
                continue


def play_video(page: Page, lecture: LectureInfo, speed: float) -> PlaybackResult:
    """Открывает, запускает и ждёт окончания одного видео."""
    console.print(f"\n  📖 [bold]{lecture.title}[/bold]")
    
    # ── 1. Определяем правильный URL ──────────────────────────────────────
    # Если ссылка ведет на view.php, нам на самом деле нужно на viewer.php
    target_url = lecture.url
    if "mod/vod/view.php" in target_url:
        target_url = target_url.replace("view.php", "viewer.php")
        console.print(f"     🔄 Перенаправление: [dim]view.php → viewer.php[/dim]")

    try:
        # Переходим на страницу лекции
        page.goto(target_url, wait_until="networkidle", timeout=60_000)
        console.print("     📄 Страница загружена")

        # Проверяем, не остались ли мы на странице с кнопкой "Watch VOD"
        if page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').count() > 0:
            console.print("     🖱️ Найдена кнопка запуска, нажимаем...")
            try:
                # Пытаемся нажать, но если это window.open, ловим новую страницу
                with page.expect_popup(timeout=5000) as popup_info:
                    page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').first.click()
                page = popup_info.value
                page.wait_for_load_state("networkidle")
                console.print("     ✨ Переключились на окно плеера")
            except Exception:
                # Если попапа нет, просто идем по ссылке
                btn_href = page.locator('a:has-text("Watch VOD"), a:has-text("Watch"), .buttons a').first.get_attribute("href")
                if btn_href:
                    page.goto(urljoin(page.url, btn_href), wait_until="networkidle")

        # ── 2. Ищем видео ────────────────────────────────────────────────
        video_frame = _wait_for_video(page, timeout=20)

        if not video_frame:
            console.print("     🔄 Пробуем активировать плеер...")
            _try_activate_player(page)
            video_frame = _wait_for_video(page, timeout=15)

        # ── Попытка 3: может быть popup / новое окно ─────────────────────
        if not video_frame:
            # Проверяем, не открылась ли новая вкладка
            all_pages = page.context.pages
            if len(all_pages) > 1:
                new_page = all_pages[-1]
                console.print("     🔄 Обнаружена новая вкладка, переключаемся...")
                new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                new_page.on("dialog", _handle_dialog)
                video_frame = _wait_for_video(new_page, timeout=15)
                if video_frame:
                    # Переключаемся на новую страницу для дальнейшей работы
                    page = new_page

        # ── Не нашли — показываем отладку ─────────────────────────────────
        if not video_frame:
            console.print("     ❌ [red]Видео не найдено[/red]")
            console.print("     🔍 [bold]Отладка — что на странице:[/bold]")
            console.print(f"        URL: {page.url}")
            console.print(f"        Фреймов: {len(page.frames)}")

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
                    console.print("\n        [cyan]🔗 Ссылки:[/cyan]")
                    for l in dump["links"]:
                        extra = f" onclick={l['onclick']}" if l.get('onclick') else ""
                        console.print(f"           {l['text'][:40]} → [dim]{l['href'][:70]}{extra}[/dim]")

                if dump.get("buttons"):
                    console.print("\n        [cyan]🔘 Кнопки:[/cyan]")
                    for b in dump["buttons"]:
                        console.print(f"           «{b['text'][:40]}» class=[dim]{b['class'][:50]}[/dim]")

                if dump.get("iframes"):
                    console.print("\n        [cyan]📦 iframes:[/cyan]")
                    for f in dump["iframes"]:
                        console.print(f"           src=[dim]{f['src']}[/dim]")

                if dump.get("objects"):
                    console.print("\n        [cyan]🎞️ object/embed:[/cyan]")
                    for o in dump["objects"]:
                        console.print(f"           {o['type']} → [dim]{o['data']}[/dim]")

                if dump.get("playerDivs"):
                    console.print("\n        [cyan]🎬 Player divs:[/cyan]")
                    for d in dump["playerDivs"]:
                        console.print(f"           <{d['tag']}> id={d['id']} class=[dim]{d['class']}[/dim]")

                if dump.get("scriptHints"):
                    console.print("\n        [cyan]📜 Scripts (full):[/cyan]")
                    for s in dump["scriptHints"][:5]:
                        console.print(f"           [dim]{s[:500]}[/dim]")

                if dump.get("vodInfo"):
                    console.print("\n        [cyan]📹 VOD Info divs:[/cyan]")
                    for v in dump["vodInfo"]:
                        console.print(f"           <{v['tag']}> .{v['class']} text=«{v['text'][:60]}»")
                        if v.get("html"):
                            console.print(f"              html=[dim]{v['html'][:150]}[/dim]")

                if dump.get("mainHTML"):
                    console.print(f"\n        [cyan]📄 Main region HTML (first 1500 chars):[/cyan]")
                    console.print(f"           [dim]{dump['mainHTML'][:1500]}[/dim]")

            except Exception as e:
                console.print(f"        Ошибка дампа: {e}")

            return PlaybackResult(lecture.title, False, error="No <video> found")

        console.print("     🎥 [green]Видео найдено![/green]")

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
            console.print("     ⏯️  Не играет, пробуем кликнуть по video...")
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
            console.print("     ⏳ Ждём загрузки метаданных видео...")
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
        console.print(f"     ▶️  Длительность: {dur_min:.1f} мин (x{speed} ≈ {eff_min:.1f} мин)")

        # ── Smart Polling: ждём окончания ─────────────────────────────────
        start = time.time()
        # Безопасный лимит ожидания: оригинальная длительность + 30 минут запаса.
        # Это спасет от раннего выхода, если Moodle сбросил скорость до x1.0.
        max_wait = (duration + 1800) if duration > 0 else 7200  
        stall = 0
        last_t = 0.0

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                       BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                       console=console) as prog:
            task = prog.add_task(f"  {lecture.title[:40]}", total=max(duration, 1))

            while time.time() - start < max_wait:
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
                    prog.update(task, completed=duration)
                    console.print("\n     ⏳ Досмотр: ожидание 15 секунд для сохранения статистики в LMS...")
                    page.wait_for_timeout(15000)
                    break

                prog.update(task, completed=cur)

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
        console.print(f"     ✅ [green]Готово за {elapsed/60:.1f} мин[/green]")
        return PlaybackResult(lecture.title, True, elapsed)

    except Exception as e:
        console.print(f"     ❌ [red]{e}[/red]")
        return PlaybackResult(lecture.title, False, error=str(e)[:50])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="🎬 iClass AutoPlayer v2.0")
    p.add_argument("--course", default=COURSE_URL, help="URL курса")
    p.add_argument("--speed", type=float, default=PLAYBACK_SPEED, help="Скорость (default: 2.0)")
    return p.parse_args()


def main():
    args = parse_args()

    console.print(Panel.fit(
        "[bold cyan]🎬 iClass AutoPlayer v2.0[/bold cyan]\n"
        f"[dim]Автологин → Меню по неделям → Просмотр x{args.speed}[/dim]",
        border_style="cyan",
    ))

    with sync_playwright() as pw:
        # headless=True скрывает окно браузера, --mute-audio полностью отключает звук
        browser = pw.chromium.launch(
            headless=True,
            args=["--mute-audio"]
        )
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()
        page.on("dialog", _handle_dialog)

        try:
            # 1. Автологин
            if not auto_login(page):
                return

            # 2. Сканирование
            weeks = scan_course(page, args.course)
            if not weeks:
                console.print("[red]❌ Лекции не найдены[/red]")
                return

            # 3. Интерактивное меню
            queue = show_menu(weeks)
            if not queue:
                return

            # 4. Просмотр
            console.print(f"\n🚀 Начинаем: [bold]{len(queue)}[/bold] видео\n")
            results = []
            for i, lec in enumerate(queue, 1):
                console.rule(f"[bold]{i}/{len(queue)}[/bold]")
                r = play_video(page, lec, args.speed)
                results.append(r)
                if i < len(queue):
                    page.wait_for_timeout(3000)

            # 5. Итоги
            console.print("\n")
            table = Table(title="📊 Итоги", show_lines=True)
            table.add_column("#", width=4)
            table.add_column("Лекция", max_width=45)
            table.add_column("Статус", width=8)

            for i, r in enumerate(results, 1):
                st = "✅" if r.success else "❌"
                table.add_row(str(i), r.title[:45], st)
            console.print(table)

            ok = sum(1 for r in results if r.success)
            console.print(f"\n🎉 Просмотрено: [green]{ok}[/green]/{len(results)}")

        except KeyboardInterrupt:
            console.print("\n[yellow]⚠️  Ctrl+C[/yellow]")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
