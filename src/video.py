"""
Модуль скачивания видео-лекций.

Поддерживает два бэкенда:
    1. yt-dlp (приоритетный) — лучше справляется с авторизацией,
       выбором качества, и сложными плейлистами.
    2. ffmpeg (fallback) — прямая загрузка потоков m3u8/mpd.

Также поддерживает прямые ссылки на mp4/webm через aiohttp.

Пост-обработка:
    Все скачанные видео проходят через ffmpeg re-mux для гарантии
    совместимости с QuickTime (macOS), iOS, Android.
"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from .config import VIDEO_DOWNLOAD_TIMEOUT, USER_AGENT

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# ПРОВЕРКА СИСТЕМНЫХ ЗАВИСИМОСТЕЙ
# ══════════════════════════════════════════════════════════════════════════════

def _check_ffmpeg() -> str | None:
    """Проверяет наличие ffmpeg. Возвращает путь или None."""
    return shutil.which("ffmpeg")


def _check_ytdlp() -> str | None:
    """Проверяет наличие yt-dlp. Возвращает путь или None."""
    return shutil.which("yt-dlp")


def check_video_tools() -> dict[str, str | None]:
    """
    Проверяет доступность утилит для скачивания видео.

    Returns:
        dict: {"ffmpeg": path|None, "yt-dlp": path|None}
    """
    tools = {
        "ffmpeg": _check_ffmpeg(),
        "yt-dlp": _check_ytdlp(),
    }

    if tools["yt-dlp"]:
        console.print(f"  ✅ yt-dlp: [dim]{tools['yt-dlp']}[/dim]")
    else:
        console.print("  ⚠️  yt-dlp: не найден (pip install yt-dlp)")

    if tools["ffmpeg"]:
        console.print(f"  ✅ ffmpeg: [dim]{tools['ffmpeg']}[/dim]")
    else:
        console.print("  ⚠️  ffmpeg: не найден (brew install ffmpeg)")

    return tools


# ══════════════════════════════════════════════════════════════════════════════
# ПОСТ-ОБРАБОТКА: re-mux для совместимости
# ══════════════════════════════════════════════════════════════════════════════

async def _remux_for_compatibility(raw_path: Path, final_path: Path) -> bool:
    """
    Re-mux видео через ffmpeg для 100% совместимости с Apple/Android.

    Проблема: HLS/DASH потоки из Moodle часто имеют:
        - Битые timestamps (PTS/DTS) из MPEG-TS сегментов
        - Отсутствующий moov atom в начале файла
        - Неправильный container metadata
    Всё это приводит к тому, что файл "технически MP4", но
    QuickTime/iOS/Android не могут его открыть.

    Решение: полный re-mux (без перекодировки кодеков) с фиксом:
        ffmpeg -i raw.mp4 -c copy -movflags +faststart -fflags +genpts fixed.mp4

    Args:
        raw_path: Путь к "сырому" скачанному файлу
        final_path: Путь к финальному исправленному файлу

    Returns:
        bool: True если re-mux успешен
    """
    if not _check_ffmpeg():
        console.print("  ⚠️  ffmpeg не найден — пропускаем re-mux")
        # Просто переименовываем
        if raw_path != final_path:
            raw_path.rename(final_path)
        return True

    console.print("\n  🔧 Пост-обработка: re-mux для совместимости с macOS/iOS/Android...")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(raw_path),
        # Копируем кодеки без перекодировки (быстро)
        "-c:v", "copy",
        "-c:a", "aac",                 # Перекодируем аудио в AAC (гарантия совместимости)
        "-b:a", "128k",                # Нормальный битрейт аудио
        "-movflags", "+faststart",     # moov atom в начало (критично для стриминга)
        "-fflags", "+genpts",          # Пересчитать timestamps
        "-avoid_negative_ts", "make_zero",  # Убрать отрицательные timestamps
        "-map", "0:v:0",              # Взять первый видео-поток
        "-map", "0:a:0?",             # Взять первый аудио-поток (? = не падать если нет)
        str(final_path),
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=600  # 10 мин на re-mux
            )
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if "time=" in decoded:
                # Показываем прогресс re-mux
                console.print(f"     [dim]{decoded[:100]}[/dim]", end="\r")

        await process.wait()

        if process.returncode == 0 and final_path.exists() and final_path.stat().st_size > 1024:
            # Удаляем сырой файл
            if raw_path != final_path and raw_path.exists():
                raw_path.unlink()
            console.print("  ✅ Re-mux завершён — файл совместим с macOS/iOS/Android")
            return True
        else:
            console.print(f"  ⚠️  Re-mux не удался (код {process.returncode}), используем оригинал")
            if raw_path != final_path:
                raw_path.rename(final_path)
            return True  # Отдаём что есть

    except Exception as e:
        console.print(f"  ⚠️  Ошибка re-mux: {e}, используем оригинал")
        if raw_path != final_path and raw_path.exists():
            raw_path.rename(final_path)
        return True


# ══════════════════════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ ЧЕРЕЗ YT-DLP
# ══════════════════════════════════════════════════════════════════════════════

async def _download_with_ytdlp(
    video_url: str,
    output_path: Path,
    cookies: dict[str, str],
    headers: dict[str, str],
) -> bool:
    """
    Скачивает видео через yt-dlp.

    Формат: берём лучшее качество (best) — без попытки разделить
    video+audio, т.к. Moodle HLS обычно отдаёт один мультиплексный поток.
    """
    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "--user-agent", USER_AGENT,
        # ── Формат: best (единый поток, без мерджа) ───────────────────────
        # Moodle HLS обычно отдаёт один поток с video+audio вместе.
        # "bestvideo+bestaudio" ломает такие потоки.
        "-f", "best",
        # Не создавать .part файлы
        "--no-part",
        # Перезаписать если файл уже есть
        "--force-overwrites",
        # Вывод прогресса
        "--progress",
        "--newline",
        # Выходной файл
        "-o", str(output_path),
    ]

    # Передаём Referer
    if "Referer" in headers:
        cmd.extend(["--referer", headers["Referer"]])

    # Передаём cookies как header строку
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        cmd.extend(["--add-header", f"Cookie: {cookie_str}"])

    # URL в конце
    cmd.append(video_url)

    console.print(f"\n  🚀 Запускаем [bold cyan]yt-dlp[/bold cyan]...")
    console.print(f"     [dim]$ yt-dlp -f best ... {video_url[:60]}[/dim]")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=VIDEO_DOWNLOAD_TIMEOUT
            )
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                if any(kw in decoded.lower() for kw in ["%", "download", "merge", "error", "warning", "destination"]):
                    console.print(f"     [dim]{decoded}[/dim]")

        await process.wait()

        # Поиск финального файла
        if output_path.exists() and output_path.stat().st_size > 1024:
            return True

        # yt-dlp мог сохранить с другим расширением
        parent = output_path.parent
        stem = output_path.stem
        for ext in [".mp4", ".mkv", ".webm", ".ts"]:
            candidate = parent / f"{stem}{ext}"
            if candidate.exists() and candidate.stat().st_size > 1024:
                if candidate != output_path:
                    candidate.rename(output_path)
                return True

        # Поиск по маске
        for f in parent.glob(f"*{stem}*"):
            if f.suffix in (".mp4", ".mkv", ".webm", ".ts") and f.stat().st_size > 1024:
                f.rename(output_path)
                return True

        # Очистка .part
        for f in parent.glob("*.part"):
            f.unlink(missing_ok=True)

        console.print(f"  ⚠️  yt-dlp код {process.returncode}, файл не найден")
        return False

    except asyncio.TimeoutError:
        console.print(f"  ⚠️  yt-dlp таймаут ({VIDEO_DOWNLOAD_TIMEOUT}с)")
        for f in output_path.parent.glob("*.part"):
            f.unlink(missing_ok=True)
        try:
            process.kill()
        except Exception:
            pass
        return False
    except FileNotFoundError:
        console.print("  ❌ yt-dlp не найден")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ ЧЕРЕЗ FFMPEG
# ══════════════════════════════════════════════════════════════════════════════

async def _download_with_ffmpeg(
    video_url: str,
    output_path: Path,
    cookies: dict[str, str],
    headers: dict[str, str],
) -> bool:
    """
    Скачивает видео через ffmpeg (потоковый m3u8/mpd → mp4).

    Важно: НЕ используем -c copy для HLS, потому что MPEG-TS сегменты
    после склейки часто дают битый контейнер. Вместо этого делаем
    лёгкий re-mux с фиксом timestamps.
    """
    cmd = ["ffmpeg", "-y"]  # -y = перезаписать без вопросов

    # HTTP-заголовки для ffmpeg
    header_parts = [f"User-Agent: {USER_AGENT}"]
    if "Referer" in headers:
        header_parts.append(f"Referer: {headers['Referer']}")
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        header_parts.append(f"Cookie: {cookie_str}")

    headers_str = "\r\n".join(header_parts)
    cmd.extend(["-headers", headers_str])

    # Входной поток
    cmd.extend(["-i", video_url])

    # Re-mux с фиксом timestamps (не просто copy!)
    cmd.extend([
        "-c:v", "copy",                    # Видео без перекодировки
        "-c:a", "aac", "-b:a", "128k",     # Аудио перекодировать в AAC
        "-bsf:a", "aac_adtstoasc",         # Фикс AAC в MP4
        "-movflags", "+faststart",          # moov atom в начало
        "-fflags", "+genpts+discardcorrupt",  # Фикс timestamps + отброс битых пакетов
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        str(output_path),
    ])

    console.print(f"\n  🚀 Запускаем [bold cyan]ffmpeg[/bold cyan]...")
    console.print(f"     [dim]$ ffmpeg -i \"{video_url[:50]}...\" → {output_path.name}[/dim]")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_progress = ""
        while True:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=VIDEO_DOWNLOAD_TIMEOUT
            )
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                if "time=" in decoded or "error" in decoded.lower():
                    if decoded != last_progress:
                        console.print(f"     [dim]{decoded[:120]}[/dim]")
                        last_progress = decoded

        await process.wait()

        if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
            return True
        else:
            console.print(f"  ⚠️  ffmpeg код {process.returncode}")
            return False

    except asyncio.TimeoutError:
        console.print(f"  ⚠️  ffmpeg таймаут ({VIDEO_DOWNLOAD_TIMEOUT}с)")
        try:
            process.kill()
        except Exception:
            pass
        return False
    except FileNotFoundError:
        console.print("  ❌ ffmpeg не найден")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ ПРЯМОГО ВИДЕО ЧЕРЕЗ AIOHTTP
# ══════════════════════════════════════════════════════════════════════════════

async def _download_direct_video(
    video_url: str,
    output_path: Path,
    cookies: dict[str, str],
    headers: dict[str, str],
) -> bool:
    """
    Скачивает прямую видео-ссылку (mp4/webm) через aiohttp.
    """
    aiohttp_headers = {"User-Agent": USER_AGENT}
    aiohttp_headers.update(headers)

    console.print(f"\n  ⬇️  Прямая загрузка видео через [bold cyan]aiohttp[/bold cyan]...")

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            cookies=cookies,
            headers=aiohttp_headers,
            connector=connector,
        ) as session:
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=VIDEO_DOWNLOAD_TIMEOUT)) as resp:
                if resp.status != 200:
                    console.print(f"  ⚠️  HTTP {resp.status}")
                    return False

                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.percentage:>3.0f}%"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task(
                        "Загрузка видео",
                        total=total if total > 0 else None
                    )

                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(output_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress.update(task, completed=downloaded)

                if output_path.exists() and output_path.stat().st_size > 1024:
                    return True
                return False

    except Exception as e:
        console.print(f"  ⚠️  Ошибка aiohttp: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def download_video(
    video_url: str,
    output_path: Path,
    is_stream: bool,
    cookies: dict[str, str],
    headers: dict[str, str],
) -> Path | None:
    """
    Скачивает видео-лекцию и гарантирует совместимость файла.

    Pipeline:
        1. Скачиваем "сырой" файл (ffmpeg / yt-dlp / aiohttp)
        2. Re-mux через ffmpeg для фикса контейнера
        3. Проверяем финальный файл
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Проверяем доступные инструменты
    console.print("\n🔧 Проверка инструментов для видео:")
    tools = check_video_tools()

    if not tools["ffmpeg"] and not tools["yt-dlp"]:
        if is_stream:
            console.print(
                "\n[bold red]❌ Для потокового видео нужен ffmpeg или yt-dlp![/bold red]\n"
                "  brew install ffmpeg && pip install yt-dlp\n"
            )
            return None

    # Путь для "сырого" файла — рядом с финальным, с префиксом _raw_
    raw_path = output_path.parent / f"_raw_{output_path.stem}.mp4"
    # Чистим старые raw файлы
    raw_path.unlink(missing_ok=True)

    success = False

    console.print(f"\n📍 raw  → [dim]{raw_path.name}[/dim]")
    console.print(f"📍 final → [dim]{output_path.name}[/dim]")

    if is_stream:
        console.print(f"\n📡 Скачивание потокового видео...")

        if tools["ffmpeg"] and not success:
            console.print("  [cyan]▶ Попытка 1: ffmpeg[/cyan]")
            success = await _download_with_ffmpeg(video_url, raw_path, cookies, headers)
            console.print(f"  {'✅' if success else '❌'} ffmpeg: {success}")

        if tools["yt-dlp"] and not success:
            console.print("  [cyan]▶ Попытка 2: yt-dlp[/cyan]")
            success = await _download_with_ytdlp(video_url, raw_path, cookies, headers)
            console.print(f"  {'✅' if success else '❌'} yt-dlp: {success}")
    else:
        console.print(f"\n📥 Скачивание прямого видео...")

        console.print("  [cyan]▶ Попытка 1: aiohttp[/cyan]")
        success = await _download_direct_video(video_url, raw_path, cookies, headers)
        console.print(f"  {'✅' if success else '❌'} aiohttp: {success}")

        if not success and tools["yt-dlp"]:
            console.print("  [cyan]▶ Попытка 2: yt-dlp[/cyan]")
            success = await _download_with_ytdlp(video_url, raw_path, cookies, headers)
            console.print(f"  {'✅' if success else '❌'} yt-dlp: {success}")

        if not success and tools["ffmpeg"]:
            console.print("  [cyan]▶ Попытка 3: ffmpeg[/cyan]")
            success = await _download_with_ffmpeg(video_url, raw_path, cookies, headers)
            console.print(f"  {'✅' if success else '❌'} ffmpeg: {success}")

    # ── Поиск raw файла ──────────────────────────────────────────────────
    if not raw_path.exists():
        console.print(f"\n  🔍 raw файл не на месте, ищем в output/...")
        parent = output_path.parent
        found = None
        for f in sorted(parent.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f != output_path and f.name != ".DS_Store":
                if f.stat().st_size > 1024:
                    console.print(f"  🔍 Нашли: [bold]{f.name}[/bold] ({f.stat().st_size / 1024 / 1024:.1f} МБ)")
                    found = f
                    break

        if found:
            found.rename(raw_path)
        else:
            console.print("[bold red]❌ Не удалось скачать видео — файл не создан[/bold red]")
            console.print("  📂 Содержимое output/:")
            for f in parent.iterdir():
                console.print(f"     {f.name} ({f.stat().st_size} bytes)")
            return None

    raw_size = raw_path.stat().st_size
    console.print(f"\n📦 Raw файл: {raw_size / 1024 / 1024:.1f} МБ")

    if raw_size < 1024:
        console.print("[bold red]❌ Файл слишком маленький (< 1 КБ)[/bold red]")
        raw_path.unlink(missing_ok=True)
        return None

    # ── Пост-обработка: re-mux для совместимости ─────────────────────────
    remux_ok = await _remux_for_compatibility(raw_path, output_path)

    if remux_ok and output_path.exists():
        size_mb = output_path.stat().st_size / (1024 * 1024)
        console.print(f"\n✅ Видео сохранено: [bold green]{output_path}[/bold green]")
        console.print(f"   📦 Размер: {size_mb:.1f} МБ")
        return output_path
    else:
        console.print("[bold red]❌ Не удалось обработать видео[/bold red]")
        return None

