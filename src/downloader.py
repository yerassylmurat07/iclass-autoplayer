"""
Асинхронный загрузчик слайдов через aiohttp.

Возможности:
    - Параллельная загрузка с семафором
    - Retry-логика с экспоненциальным backoff
    - Автоопределение конца презентации (по 404)
    - Rich прогресс-бар
"""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiohttp
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from .config import (
    MAX_SLIDES,
    CONCURRENT_DOWNLOADS,
    DOWNLOAD_RETRIES,
    RETRY_DELAY,
)

console = Console()


# ── Генерация URL ────────────────────────────────────────────────────────────

def build_slide_url(template: str, target_number: int, original_number: int = 1) -> str:
    """
    Подставляет номер слайда в URL-шаблон.

    Умно заменяет число из original_number на target_number,
    сохраняя ведущие нули если они были.

    Args:
        template: URL перехваченного слайда (шаблон)
        target_number: Номер слайда для подстановки
        original_number: Номер который был в оригинальном URL

    Returns:
        str: URL для конкретного слайда

    Examples:
        >>> build_slide_url(".../slides/1.png", 5, 1)
        '.../slides/5.png'
        >>> build_slide_url(".../img/001.jpg", 42, 1)
        '.../img/042.jpg'
    """
    orig_str = str(original_number)

    # Ищем число перед расширением файла
    match = re.search(r'(\d+)(\.\w+)$', template)
    if match:
        found_num = match.group(1)
        # Определяем ширину (ведущие нули)
        width = len(found_num)
        replacement = str(target_number).zfill(width)
        # Заменяем только это конкретное вхождение
        start, end = match.start(1), match.end(1)
        return template[:start] + replacement + template[end:]

    # Fallback: заменяем первое число в пути
    parsed = urlparse(template)
    new_path = re.sub(
        re.escape(orig_str),
        str(target_number),
        parsed.path,
        count=1
    )
    return urlunparse(parsed._replace(path=new_path))


# ── Загрузка одного слайда ───────────────────────────────────────────────────

async def _download_one(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    semaphore: asyncio.Semaphore,
) -> bool:
    """
    Скачивает один слайд с retry-логикой.

    Returns:
        True — успешно скачан
        False — 404 (конец) или необратимая ошибка
    """
    async with semaphore:
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 404:
                        return False
                    if resp.status == 403:
                        console.print(f"  ⛔ 403 Forbidden: {url[:80]}")
                        return False
                    if resp.status != 200:
                        if attempt < DOWNLOAD_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        console.print(f"  ⚠️  HTTP {resp.status}: {url[:60]}")
                        return False

                    data = await resp.read()
                    # Проверяем что это действительно изображение (не HTML-редирект)
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        return False

                    dest.write_bytes(data)
                    return True

            except asyncio.TimeoutError:
                if attempt < DOWNLOAD_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                return False

            except aiohttp.ClientError as e:
                if attempt < DOWNLOAD_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                console.print(f"  ⚠️  Ошибка: {e}")
                return False

    return False


# ── Массовая загрузка ────────────────────────────────────────────────────────

async def download_all_slides(
    template_url: str,
    cookies: dict[str, str],
    download_dir: Path,
    start_number: int = 1,
    known_urls: list[str] | None = None,
) -> list[Path]:
    """
    Параллельно скачивает все слайды презентации.

    Алгоритм:
        1. Если known_urls переданы — качаем только их
        2. Иначе: генерируем URL от start_number до MAX_SLIDES
        3. Скачиваем батчами по CONCURRENT_DOWNLOADS
        4. Если весь батч → 404: СТОП (конец презентации)

    Args:
        template_url: URL шаблон (первый пойманный слайд)
        cookies: Cookies из Playwright (авторизация)
        download_dir: Папка для PNG файлов
        start_number: С какого номера начать (обычно 1)
        known_urls: Если уже перехвачены конкретные URL — качать их

    Returns:
        list[Path]: Отсортированный список путей к скачанным файлам
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

    # Определяем расширение
    ext_match = re.search(r'\.\w+$', urlparse(template_url).path)
    ext = ext_match.group(0) if ext_match else ".png"

    downloaded: list[tuple[int, Path]] = []

    connector = aiohttp.TCPConnector(limit=CONCURRENT_DOWNLOADS, ssl=False)
    async with aiohttp.ClientSession(cookies=cookies, connector=connector) as session:

        if known_urls and len(known_urls) > 3:
            # ── Режим 1: качаем только перехваченные URL ──────────────────
            console.print(
                f"\n⬇️  Скачиваем [bold]{len(known_urls)}[/bold] перехваченных слайдов..."
            )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Загрузка", total=len(known_urls))

                tasks_list = []
                paths_list = []
                for i, url in enumerate(known_urls, start=1):
                    dest = download_dir / f"slide_{i:03d}{ext}"
                    tasks_list.append(_download_one(session, url, dest, semaphore))
                    paths_list.append((i, dest))

                results = await asyncio.gather(*tasks_list)
                for (num, path), ok in zip(paths_list, results):
                    if ok:
                        downloaded.append((num, path))
                    progress.advance(task)

        else:
            # ── Режим 2: генерируем URL по шаблону ────────────────────────
            console.print(
                f"\n⬇️  Параллельное скачивание (по шаблону, макс. {MAX_SLIDES})..."
            )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Поиск слайдов", total=MAX_SLIDES)
                consecutive_failures = 0

                for batch_start in range(start_number, start_number + MAX_SLIDES, CONCURRENT_DOWNLOADS):
                    batch_end = min(batch_start + CONCURRENT_DOWNLOADS, start_number + MAX_SLIDES)
                    batch_nums = range(batch_start, batch_end)

                    tasks_list = []
                    paths_list = []
                    for num in batch_nums:
                        url = build_slide_url(template_url, num, start_number)
                        dest = download_dir / f"slide_{num:03d}{ext}"
                        tasks_list.append(_download_one(session, url, dest, semaphore))
                        paths_list.append((num, dest))

                    results = await asyncio.gather(*tasks_list)

                    batch_ok = 0
                    for (num, path), ok in zip(paths_list, results):
                        if ok:
                            downloaded.append((num, path))
                            batch_ok += 1
                        progress.advance(task)

                    if batch_ok == 0:
                        consecutive_failures += 1
                        if consecutive_failures >= 2:
                            # Два пустых батча подряд → точно конец
                            progress.update(task, total=progress.tasks[0].completed)
                            break
                    else:
                        consecutive_failures = 0

    # Сортируем по номеру
    downloaded.sort(key=lambda x: x[0])

    if downloaded:
        console.print(
            f"\n📊 Скачано: [bold green]{len(downloaded)}[/bold green] слайдов "
            f"(#{downloaded[0][0]} — #{downloaded[-1][0]})"
        )
    else:
        console.print("[bold red]❌ Ни одного слайда не скачано[/bold red]")

    return [path for _, path in downloaded]
