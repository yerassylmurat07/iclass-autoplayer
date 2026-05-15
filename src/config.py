"""
Конфигурация и загрузка учётных данных.

Приоритет:
    1. Переменные окружения / .env
    2. Интерактивный ввод в консоли
"""

import os
import getpass
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum, auto

from dotenv import load_dotenv


# ── Типы контента ────────────────────────────────────────────────────────────

class ContentType(Enum):
    """Тип обнаруженного контента на странице лекции."""
    SLIDES = auto()   # Изображения слайдов → PDF
    VIDEO  = auto()   # Потоковое видео (m3u8/mpd) → MP4
    UNKNOWN = auto()  # Не удалось определить


# ── Константы ────────────────────────────────────────────────────────────────

# Верхний предел: сколько слайдов пытаться скачать
MAX_SLIDES: int = 200

# Таймаут ожидания перехвата первого ресурса (секунды)
INTERCEPT_TIMEOUT: float = 30.0

# Параллельных загрузок одновременно
CONCURRENT_DOWNLOADS: int = 12

# Повторных попыток при ошибке загрузки
DOWNLOAD_RETRIES: int = 3

# Задержка между повторами (секунды)
RETRY_DELAY: float = 1.0

# DPI для итогового PDF
PDF_DPI: int = 200

# Таймаут для ffmpeg/yt-dlp видео скачивания (секунды)
VIDEO_DOWNLOAD_TIMEOUT: int = 1800  # 30 минут

# User-Agent для маскировки под обычный браузер
USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Viewport
VIEWPORT: dict = {"width": 1920, "height": 1080}


# ── Dataclass для учётных данных ─────────────────────────────────────────────

@dataclass
class Credentials:
    """Учётные данные для портала Moodle."""
    portal_url: str
    login: str
    password: str


def load_credentials() -> Credentials:
    """
    Загружает учётные данные.

    Порядок:
        1. .env файл из корня проекта
        2. Переменные окружения
        3. Ввод в консоли (пароль скрыт)

    Returns:
        Credentials: dataclass с portal_url, login, password
    """
    # Ищем .env в корне проекта
    project_root = Path(__file__).parent.parent
    env_path = project_root / ".env"
    load_dotenv(dotenv_path=env_path)

    portal_url = os.getenv("MOODLE_URL", "").strip()
    login = os.getenv("MOODLE_LOGIN", "").strip()
    password = os.getenv("MOODLE_PASSWORD", "").strip()

    if not portal_url:
        portal_url = input("🌐 URL портала (напр. https://lms.university.kz): ").strip()
    if not login:
        login = input("👤 Логин: ").strip()
    if not password:
        password = getpass.getpass("🔑 Пароль: ")

    # Нормализация URL
    portal_url = portal_url.rstrip("/")
    if not portal_url.startswith("http"):
        portal_url = f"https://{portal_url}"

    return Credentials(portal_url=portal_url, login=login, password=password)
