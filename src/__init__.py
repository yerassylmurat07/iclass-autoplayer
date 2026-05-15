"""
Moodle Lecture Parser v3.0 — пакет модулей.

Модули:
    config      — загрузка .env, константы
    auth        — Playwright авторизация на Moodle
    interceptor — Универсальный Network Interception (слайды + видео)
    downloader  — aiohttp параллельное скачивание слайдов
    video       — скачивание видео-лекций (m3u8/mpd → mp4)
    assembler   — сборка изображений в PDF
"""
