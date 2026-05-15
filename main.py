#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          🎓 Moodle Lecture Parser v3.0 — Universal Content Grabber          ║
║                                                                              ║
║  Playwright → Network Interception → Auto-Detect → Slides PDF / Video MP4  ║
╚══════════════════════════════════════════════════════════════════════════════╝

Использование:
    python main.py                           — интерактивный режим
    python main.py --url <URL лекции>        — одна лекция
    python main.py --batch lectures.txt      — пакетный режим
    python main.py --url <URL> --name "Имя"  — с кастомным именем
    python main.py --debug                   — видимый браузер (headless=False)

Автоопределение контента:
    📄 Слайды (PNG/JPG) → скачивание aiohttp → сборка PDF
    🎬 Видео (m3u8/mpd/mp4) → yt-dlp / ffmpeg → MP4

Настройка:
    Скопируй .env.example → .env и заполни свои данные.
"""

import argparse
import asyncio
import shutil
import sys
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from playwright.async_api import async_playwright, BrowserContext, Page

from src.config import load_credentials, Credentials, ContentType, USER_AGENT, VIEWPORT
from src.auth import login_to_moodle
from src.interceptor import intercept_lecture_content, InterceptResult
from src.downloader import download_all_slides
from src.assembler import build_pdf
from src.video import download_video

console = Console()

# Директория для итоговых файлов (PDF + MP4)
OUTPUT_DIR = Path(__file__).parent / "output"


# ── CLI аргументы ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="🎓 Moodle Lecture Parser v3.0 — Universal Content Grabber",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Примеры:
  python main.py
  python main.py --url https://lms.uni.kz/mod/resource/view.php?id=12345
  python main.py --url https://lms.uni.kz/... --name "Лекция_01"
  python main.py --batch lectures.txt
  python main.py --debug
        """
    )
    parser.add_argument("--url", help="URL одной лекции")
    parser.add_argument("--name", help="Имя выходного файла (без расширения)")
    parser.add_argument(
        "--batch",
        help="Путь к файлу со списком URL (по одному на строку)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Открыть браузер в видимом режиме для отладки"
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Не удалять временные файлы"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Директория для сохранения (по умолчанию: {OUTPUT_DIR})"
    )
    return parser.parse_args()


# ── Извлечение заголовка лекции ──────────────────────────────────────────────

async def get_lecture_title(page: Page) -> str:
    """Пытается извлечь заголовок лекции из HTML страницы."""
    selectors = [
        '.page-header-headings h1',
        'h2.activity-header',
        '#page-header h1',
        'h2',
        'title',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            text = await el.text_content(timeout=2000)
            if text and len(text.strip()) > 2:
                clean = re.sub(r'[<>:"/\\|?*\n\r]', '_', text.strip())
                clean = re.sub(r'_+', '_', clean).strip('_')
                return clean[:80]
        except Exception:
            continue
    return "lecture"


# ── Обработка одной лекции ───────────────────────────────────────────────────

async def process_lecture(
    page: Page,
    context: BrowserContext,
    lecture_url: str,
    file_name: str | None,
    output_dir: Path,
    no_cleanup: bool,
) -> tuple[Path | None, str]:
    """
    Полный пайплайн обработки одной лекции.

    Автоматически определяет тип контента:
        SLIDES → download images → assemble PDF
        VIDEO  → yt-dlp / ffmpeg → save MP4

    Returns:
        tuple: (Path к файлу | None, тип контента строкой)
    """
    try:
        # 1. Универсальный перехват контента
        result: InterceptResult = await intercept_lecture_content(page, lecture_url)

        # 2. Определяем имя файла
        if not file_name:
            file_name = await get_lecture_title(page)
        file_name = file_name or "lecture"

        # 3. Извлекаем cookies
        pw_cookies = await context.cookies()
        aiohttp_cookies = {c["name"]: c["value"] for c in pw_cookies}

        # ═══════════════════════════════════════════════════════════════════
        # СЦЕНАРИЙ 1: СЛАЙДЫ → PDF
        # ═══════════════════════════════════════════════════════════════════
        if result.content_type == ContentType.SLIDES:
            console.print(
                "\n📄 [bold]Сценарий: СЛАЙДЫ → PDF[/bold]"
            )

            temp_dir = output_dir / "temp_slides" / file_name
            image_paths = await download_all_slides(
                template_url=result.template_url,
                cookies=aiohttp_cookies,
                download_dir=temp_dir,
                start_number=result.detected_number,
                known_urls=result.all_urls if len(result.all_urls) > 3 else None,
            )

            if not image_paths:
                console.print("[bold red]❌ Ни одного слайда не скачано[/bold red]")
                return None, "slides"

            pdf_path = output_dir / f"{file_name}.pdf"
            build_pdf(image_paths, pdf_path)

            if not no_cleanup:
                shutil.rmtree(temp_dir, ignore_errors=True)

            return pdf_path, "slides"

        # ═══════════════════════════════════════════════════════════════════
        # СЦЕНАРИЙ 2: ВИДЕО → MP4
        # ═══════════════════════════════════════════════════════════════════
        elif result.content_type == ContentType.VIDEO:
            console.print(
                "\n🎬 [bold]Сценарий: ВИДЕО → MP4[/bold]"
            )

            mp4_path = output_dir / f"{file_name}.mp4"

            video_path = await download_video(
                video_url=result.video_url,
                output_path=mp4_path,
                is_stream=result.video_is_stream,
                cookies=aiohttp_cookies,
                headers=result.video_headers,
            )

            return video_path, "video"

        else:
            console.print("[bold red]❌ Неизвестный тип контента[/bold red]")
            return None, "unknown"

    except Exception as e:
        console.print(f"[bold red]❌ Ошибка:[/bold red] {e}")
        return None, "error"


# ── Точка входа ──────────────────────────────────────────────────────────────

async def main() -> None:
    args = parse_args()

    # Баннер
    console.print(Panel.fit(
        "[bold cyan]🎓 Moodle Lecture Parser v3.0[/bold cyan]\n"
        "[dim]Universal Content Grabber — Slides & Video[/dim]\n"
        "[dim]Network Interception → Auto-Detect → PDF / MP4[/dim]",
        border_style="cyan",
    ))

    # 1. Учётные данные
    creds: Credentials = load_credentials()

    # 2. Определяем список URL для обработки
    lectures: list[tuple[str, str | None]] = []

    if args.batch:
        batch_file = Path(args.batch)
        if not batch_file.exists():
            console.print(f"[bold red]❌ Файл не найден: {batch_file}[/bold red]")
            sys.exit(1)
        for line in batch_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("|", 1)
                url = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else None
                lectures.append((url, name))
        console.print(f"📋 Пакетный режим: [bold]{len(lectures)}[/bold] лекций")

    elif args.url:
        lectures.append((args.url, args.name))

    else:
        url = input("\n📚 URL лекции (полная ссылка): ").strip()
        if not url.startswith("http"):
            console.print("[bold red]❌ Некорректный URL[/bold red]")
            sys.exit(1)
        name = input("📄 Имя файла (Enter = авто): ").strip() or None
        lectures.append((url, name))

    # 3. Запускаем Playwright
    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, Path | None, str]] = []  # (url, path, type)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.debug,
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
        )
        page = await context.new_page()

        try:
            # Авторизация (один раз)
            await login_to_moodle(page, creds)

            # Обрабатываем каждую лекцию
            for i, (url, name) in enumerate(lectures, 1):
                if len(lectures) > 1:
                    console.rule(f"[bold]Лекция {i}/{len(lectures)}[/bold]")

                file_path, content_type = await process_lecture(
                    page=page,
                    context=context,
                    lecture_url=url,
                    file_name=name,
                    output_dir=output_dir,
                    no_cleanup=args.no_cleanup,
                )
                results.append((url, file_path, content_type))

        finally:
            await browser.close()

    # 4. Итоговый отчёт
    console.print("\n")
    table = Table(title="📊 Итоги", show_lines=True)
    table.add_column("URL", style="dim", max_width=50)
    table.add_column("Тип", justify="center")
    table.add_column("Файл", style="green")
    table.add_column("Статус")

    for url, path, ctype in results:
        short_url = url[:50] + "..." if len(url) > 50 else url
        type_icon = "📄 PDF" if ctype == "slides" else "🎬 MP4" if ctype == "video" else "❓"
        if path:
            table.add_row(short_url, type_icon, path.name, "✅ Успех")
        else:
            table.add_row(short_url, type_icon, "—", "❌ Ошибка")

    console.print(table)

    ok = sum(1 for _, p, _ in results if p)
    fail = len(results) - ok
    slides_count = sum(1 for _, p, t in results if p and t == "slides")
    video_count = sum(1 for _, p, t in results if p and t == "video")

    console.print(
        f"\n🎉 Готово! "
        f"Успешно: [green]{ok}[/green] "
        f"(📄 {slides_count} PDF, 🎬 {video_count} видео) | "
        f"Ошибки: [red]{fail}[/red]"
    )
    if ok:
        console.print(f"📂 Файлы в: [bold]{output_dir.absolute()}[/bold]")


if __name__ == "__main__":
    asyncio.run(main())
