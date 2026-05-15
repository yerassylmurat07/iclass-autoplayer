"""
Универсальный Network Interceptor — перехват слайдов И видео через Playwright.

Стратегия:
    1. Подписываемся на все сетевые запросы (page + iframes)
    2. Параллельно ищем ДВА типа контента:
       a) Изображения слайдов (числовые имена: 1.png, 2.png)
       b) Потоковое видео (плейлисты: .m3u8, .mpd)
    3. Автоскролл + клик по viewer-элементам
    4. Возвращаем результат с определённым типом контента
"""

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import Page, Frame, Request
from rich.console import Console

from .config import INTERCEPT_TIMEOUT, ContentType

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# ПАТТЕРНЫ
# ══════════════════════════════════════════════════════════════════════════════

# ── Паттерны для слайдов ─────────────────────────────────────────────────────

SLIDE_PATTERNS: list[re.Pattern] = [
    re.compile(r'/(\d+)\.(png|jpe?g|webp|gif|bmp)', re.IGNORECASE),
    re.compile(r'/(slide|page|img|image)[_\-]?(\d+)\.(png|jpe?g|webp)', re.IGNORECASE),
    re.compile(r'/pages?/(\d+)/(render|image|thumb)', re.IGNORECASE),
]

IGNORE_PATTERNS: list[re.Pattern] = [
    re.compile(r'(favicon|icon|logo|avatar|banner|thumb(nail)?|badge)', re.IGNORECASE),
    re.compile(r'(pluginfile\.php.*/(u|f)\d)', re.IGNORECASE),
    re.compile(r'\d+x\d+', re.IGNORECASE),
    re.compile(r'(sprite|emoji|flag)', re.IGNORECASE),
]

# ── Паттерны для видео-потоков ───────────────────────────────────────────────

VIDEO_STREAM_PATTERNS: list[re.Pattern] = [
    # HLS плейлисты
    re.compile(r'\.m3u8(\?|$)', re.IGNORECASE),
    # MPEG-DASH манифесты
    re.compile(r'\.mpd(\?|$)', re.IGNORECASE),
]

# Паттерны для прямых видео-ссылок (MP4, WebM)
VIDEO_DIRECT_PATTERNS: list[re.Pattern] = [
    re.compile(r'\.(mp4|webm|mkv)(\?|$)', re.IGNORECASE),
]

# URL видео, которые нужно игнорировать (рекламные трекеры, мини-превью)
VIDEO_IGNORE_PATTERNS: list[re.Pattern] = [
    re.compile(r'(analytics|tracking|beacon|pixel|ads)', re.IGNORECASE),
    re.compile(r'(thumbnail|poster|preview)', re.IGNORECASE),
]


# ══════════════════════════════════════════════════════════════════════════════
# РЕЗУЛЬТАТ ПЕРЕХВАТА
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class InterceptResult:
    """
    Универсальный результат перехвата.

    Attributes:
        content_type: Что именно обнаружено (SLIDES / VIDEO)
        # — для слайдов —
        template_url: URL первого пойманного слайда (шаблон)
        detected_number: Номер пойманного слайда
        all_urls: Все перехваченные URL слайдов
        # — для видео —
        video_url: URL потокового плейлиста (.m3u8 / .mpd) или прямая ссылка
        video_is_stream: True если m3u8/mpd (нужен ffmpeg), False если прямой mp4
        video_headers: Заголовки, необходимые для скачивания (Referer, Cookie и пр.)
    """
    content_type: ContentType

    # Slides
    template_url: str = ""
    detected_number: int = 1
    all_urls: list[str] = field(default_factory=list)

    # Video
    video_url: str = ""
    video_is_stream: bool = False
    video_headers: dict[str, str] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# ДЕТЕКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def _matches_slide(url: str) -> int | None:
    """Проверяет, является ли URL слайдом. Возвращает номер или None."""
    for ignore in IGNORE_PATTERNS:
        if ignore.search(url):
            return None
    for pattern in SLIDE_PATTERNS:
        match = pattern.search(url)
        if match:
            groups = match.groups()
            for g in reversed(groups):
                if g and g.isdigit():
                    return int(g)
    return None


def _matches_video_stream(url: str) -> bool:
    """Проверяет, является ли URL потоковым плейлистом (m3u8/mpd)."""
    for ignore in VIDEO_IGNORE_PATTERNS:
        if ignore.search(url):
            return False
    for pattern in VIDEO_STREAM_PATTERNS:
        if pattern.search(url):
            return True
    return False


def _matches_video_direct(url: str) -> bool:
    """Проверяет, является ли URL прямой видео-ссылкой (mp4/webm)."""
    for ignore in VIDEO_IGNORE_PATTERNS:
        if ignore.search(url):
            return False
    for pattern in VIDEO_DIRECT_PATTERNS:
        if pattern.search(url):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# TRIGGER VIEWER
# ══════════════════════════════════════════════════════════════════════════════

async def _trigger_viewer(page: Page) -> None:
    """
    Пытается активировать просмотрщик (слайды или видео) на странице.

    Стратегии:
        - Скролл для lazy-load
        - Клик по кнопкам «Открыть» / «Launch» / «Play»
        - Клик по элементам видеоплеера
    """
    # Скролл для lazy-load
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.5)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)

    # Попытка кликнуть на launch/play кнопки
    launch_selectors = [
        # Moodle / Coursemos
        'a:has-text("열기")',
        'a:has-text("Launch")',
        'a:has-text("Open")',
        'a:has-text("View")',
        'button:has-text("Start")',
        '.resourceworkaround a',
        '#resourceobject',
        '.mediaplugin a',
        # Видеоплеер
        'video',
        '.vjs-big-play-button',
        '.video-js .vjs-play-control',
        'button[aria-label="Play"]',
        '.ytp-large-play-button',
        '.mejs-overlay-play',
        '.mediaplugin_videojs video',
    ]
    for sel in launch_selectors:
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=500):
                await locator.click()
                console.print(f"  🖱️  Кликнули по viewer: [dim]{sel}[/dim]")
                await asyncio.sleep(2)
                break
        except Exception:
            continue


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ ПЕРЕХВАТА
# ══════════════════════════════════════════════════════════════════════════════

async def intercept_lecture_content(page: Page, lecture_url: str) -> InterceptResult:
    """
    Универсальный перехват контента лекции.

    Параллельно слушает запросы на:
        1. Изображения слайдов (→ PDF pipeline)
        2. Потоковые плейлисты m3u8/mpd (→ Video pipeline)
        3. Прямые видео-ссылки mp4/webm (→ Video pipeline)

    Приоритет:
        - Если найден видео-поток (m3u8/mpd) → VIDEO
        - Если найдено прямое видео (mp4) → VIDEO
        - Если найдены слайды → SLIDES
        - Если ничего → RuntimeError

    Args:
        page: Playwright Page
        lecture_url: Полный URL лекции

    Returns:
        InterceptResult: универсальный результат с определённым content_type
    """
    # Аккумуляторы
    captured_slides: list[tuple[int, str]] = []
    captured_streams: list[str] = []  # m3u8 / mpd
    captured_direct_video: list[str] = []  # mp4 / webm
    captured_headers: dict[str, str] = {}

    def _on_request(request: Request) -> None:
        """Универсальный колбэк: анализирует каждый запрос."""
        url = request.url
        resource = request.resource_type

        # ── Проверяем видео-потоки (высший приоритет) ─────────────────────
        if _matches_video_stream(url):
            if url not in captured_streams:
                captured_streams.append(url)
                # Сохраняем заголовки запроса (нужны для ffmpeg)
                headers = request.headers
                if "referer" in headers:
                    captured_headers["Referer"] = headers["referer"]
                if "cookie" in headers:
                    captured_headers["Cookie"] = headers["cookie"]
                console.print(
                    f"  🎬 [bold magenta]ВИДЕО-ПОТОК[/bold magenta] перехвачен: "
                    f"[dim]{url[:90]}...[/dim]"
                )
            return

        # ── Проверяем прямые видео-ссылки ─────────────────────────────────
        if resource in ("media", "video", "fetch", "xhr", "other"):
            if _matches_video_direct(url):
                if url not in captured_direct_video:
                    captured_direct_video.append(url)
                    headers = request.headers
                    if "referer" in headers:
                        captured_headers["Referer"] = headers["referer"]
                    if "cookie" in headers:
                        captured_headers["Cookie"] = headers["cookie"]
                    console.print(
                        f"  🎥 [bold yellow]ПРЯМОЕ ВИДЕО[/bold yellow] перехвачено: "
                        f"[dim]{url[:90]}...[/dim]"
                    )
                return

        # ── Проверяем слайды ──────────────────────────────────────────────
        if resource in ("image", "fetch", "xhr", "document", "other"):
            slide_num = _matches_slide(url)
            if slide_num is not None:
                if url not in [u for _, u in captured_slides]:
                    captured_slides.append((slide_num, url))

    # ── Подписываемся на запросы ──────────────────────────────────────────────
    page.on("request", _on_request)

    with console.status("[bold cyan]Открываем лекцию..."):
        await page.goto(lecture_url, wait_until="domcontentloaded", timeout=30_000)

    console.print(f"\n📖 Лекция: [link={lecture_url}]{lecture_url}[/link]")
    console.print("  🔍 Анализируем трафик: ищем слайды и видео...")

    # Активируем viewer
    await _trigger_viewer(page)

    # Подписываемся на iframe-ы
    for frame in page.frames:
        if frame != page.main_frame:
            frame.on("request", _on_request)

    # Ещё раз скроллим
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1)

    # ── Ждём результат с таймаутом ────────────────────────────────────────────
    elapsed = 0.0
    while elapsed < INTERCEPT_TIMEOUT:
        # Если уже нашли поток — можно заканчивать раньше (даём ещё 2 сек)
        if captured_streams and elapsed > 3.0:
            break
        if not captured_slides and not captured_streams and not captured_direct_video:
            await asyncio.sleep(0.5)
            elapsed += 0.5
        else:
            # Что-то нашли — ждём ещё немного для сбора остальных URL
            await asyncio.sleep(2)
            break

    # Убираем листенер
    page.remove_listener("request", _on_request)

    # ── Определяем тип контента ───────────────────────────────────────────────

    # Приоритет 1: потоковое видео (m3u8/mpd)
    if captured_streams:
        # Предпочитаем master-плейлист (обычно первый)
        video_url = captured_streams[0]
        console.print(f"\n🎬 [bold green]Тип контента: ВИДЕО (поток)[/bold green]")
        console.print(f"   Плейлист: [dim]{video_url[:100]}[/dim]")
        console.print(f"   Всего перехвачено потоков: {len(captured_streams)}")
        return InterceptResult(
            content_type=ContentType.VIDEO,
            video_url=video_url,
            video_is_stream=True,
            video_headers=captured_headers,
        )

    # Приоритет 2: прямое видео (mp4/webm)
    if captured_direct_video:
        video_url = captured_direct_video[0]
        console.print(f"\n🎥 [bold green]Тип контента: ВИДЕО (прямая ссылка)[/bold green]")
        console.print(f"   URL: [dim]{video_url[:100]}[/dim]")
        return InterceptResult(
            content_type=ContentType.VIDEO,
            video_url=video_url,
            video_is_stream=False,
            video_headers=captured_headers,
        )

    # Приоритет 3: слайды
    if captured_slides:
        captured_slides.sort(key=lambda x: x[0])
        first_num, first_url = captured_slides[0]
        console.print(f"\n🖼️  [bold green]Тип контента: СЛАЙДЫ[/bold green]")
        console.print(f"   Перехвачено: {len(captured_slides)} URL")
        console.print(f"   Первый слайд (#{first_num}): [dim]{first_url[:100]}[/dim]")
        return InterceptResult(
            content_type=ContentType.SLIDES,
            template_url=first_url,
            detected_number=first_num,
            all_urls=[url for _, url in captured_slides],
        )

    # Ничего не нашли
    raise RuntimeError(
        f"❌ За {INTERCEPT_TIMEOUT}с не удалось обнаружить контент.\n\n"
        "  Скрипт искал:\n"
        "  • Слайды (изображения с цифрами: 1.png, slide_2.jpg)\n"
        "  • Видео-потоки (.m3u8, .mpd плейлисты)\n"
        "  • Прямые видео (.mp4, .webm)\n\n"
        "  Возможные причины:\n"
        "  1. Контент в защищённом iframe → --debug для визуальной отладки\n"
        "  2. Нестандартный формат URL → обновите паттерны в src/interceptor.py\n"
        "  3. Нужно нажать кнопку «Начать» → добавьте селектор в _trigger_viewer()\n"
    )
