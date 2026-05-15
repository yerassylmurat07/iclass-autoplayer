"""
Модуль авторизации на Moodle/Coursemos через Playwright.

Поддерживает:
    - Стандартную форму Moodle (/login/index.php)
    - Кастомные селекторы (Coursemos, другие LMS)
    - Автоопределение успешности логина
"""

import asyncio
from playwright.async_api import Page
from rich.console import Console

from .config import Credentials

console = Console()


# ── CSS-селекторы для разных LMS платформ ────────────────────────────────────

LOGIN_SELECTORS = {
    "username": [
        '#username',
        'input[name="username"]',
        'input[name="id"]',
        'input[name="user"]',
        '#login-username',
        'input[type="text"][autocomplete="username"]',
        'input[type="email"]',
        'input[type="text"]',
    ],
    "password": [
        '#password',
        'input[name="password"]',
        'input[name="pw"]',
        '#login-password',
        'input[type="password"]',
    ],
    "submit": [
        '#loginbtn',
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Войти")',
        'button:has-text("로그인")',        # корейский
        'button:has-text("Sign in")',
        '#login-submit',
    ],
}


async def _fill_field(page: Page, selectors: list[str], value: str, name: str) -> bool:
    """Заполняет поле, перебирая селекторы по приоритету."""
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=1000):
                await locator.fill(value)
                console.print(f"  ✓ Поле [cyan]{name}[/cyan] найдено: {sel}")
                return True
        except Exception:
            continue
    return False


async def _click_submit(page: Page, selectors: list[str]) -> bool:
    """Кликает кнопку submit, перебирая селекторы."""
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=1000):
                await locator.click()
                console.print(f"  ✓ Кнопка логина найдена: {sel}")
                return True
        except Exception:
            continue
    return False


async def login_to_moodle(page: Page, creds: Credentials) -> None:
    """
    Авторизуется на Moodle/Coursemos.

    Стратегия:
        1. Переходим на /login/index.php
        2. Перебираем CSS-селекторы для username, password, submit
        3. Проверяем: URL после логина НЕ содержит /login/ → успех

    Args:
        page: Экземпляр Playwright Page
        creds: Учётные данные (Credentials dataclass)

    Raises:
        RuntimeError: если авторизация не прошла
    """
    login_url = f"{creds.portal_url}/login/index.php"

    with console.status("[bold cyan]Открываем страницу авторизации..."):
        await page.goto(login_url, wait_until="networkidle", timeout=30_000)

    console.print(f"\n🔐 Страница авторизации: [link={login_url}]{login_url}[/link]")

    # Заполняем username
    if not await _fill_field(page, LOGIN_SELECTORS["username"], creds.login, "username"):
        raise RuntimeError(
            "❌ Не удалось найти поле username. "
            "Обновите селекторы в src/auth.py → LOGIN_SELECTORS['username']"
        )

    # Заполняем password
    if not await _fill_field(page, LOGIN_SELECTORS["password"], creds.password, "password"):
        raise RuntimeError(
            "❌ Не удалось найти поле password. "
            "Обновите селекторы в src/auth.py → LOGIN_SELECTORS['password']"
        )

    # Submit
    if not await _click_submit(page, LOGIN_SELECTORS["submit"]):
        # Fallback: Enter
        await page.keyboard.press("Enter")
        console.print("  ⚠ Кнопка submit не найдена, отправлен Enter")

    # Ждём навигации после логина
    await page.wait_for_load_state("networkidle", timeout=15_000)
    await asyncio.sleep(1)

    # Проверяем успешность
    current_url = page.url.lower()
    if "login" in current_url and ("index.php" in current_url or "failed" in current_url):
        raise RuntimeError(
            "❌ Авторизация не прошла!\n"
            "   Проверьте:\n"
            "   1. Правильность логина и пароля\n"
            "   2. URL портала\n"
            "   3. Селекторы в src/auth.py (если платформа кастомная)"
        )

    console.print("[bold green]✅ Авторизация успешна![/bold green]")
