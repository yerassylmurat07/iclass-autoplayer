# 🎓 Moodle Lecture Parser v3.0

**Универсальный парсер лекций** для Moodle / Coursemos.  
Автоматически определяет тип контента и скачивает **слайды → PDF** или **видео → MP4**.

```
Playwright → Network Interception → Auto-Detect
    ├── 📄 Слайды → aiohttp параллельная загрузка → PDF
    └── 🎬 Видео  → yt-dlp / ffmpeg → MP4
```

---

## ⚡ Быстрый старт

```bash
# 1. Создаём виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# 2. Устанавливаем зависимости
pip install -r requirements.txt
playwright install chromium

# 3. (Опционально) Для скачивания видео-лекций:
pip install yt-dlp          # рекомендуется
brew install ffmpeg          # альтернатива / дополнение

# 4. Конфигурация
cp .env.example .env
# отредактируй .env своими данными

# 5. Запуск
python main.py
```

> ⚠️ **Каждый раз перед запуском** активируй окружение: `source .venv/bin/activate`

---

## 📖 Режимы использования

### Интерактивный (по умолчанию)

```bash
source .venv/bin/activate
python main.py
```

Скрипт сам определит тип контента (слайды или видео) и скачает в нужном формате.

### Одна лекция (CLI)

```bash
python main.py --url "https://lms.uni.kz/mod/resource/view.php?id=12345"
python main.py --url "https://lms.uni.kz/..." --name "Лекция_01"
```

### Пакетный режим

Создай файл `lectures.txt`:

```text
# Формат: URL | Имя файла (имя опционально)
# Строки с # — комментарии
# Скрипт сам определит PDF или MP4 для каждого URL

https://lms.uni.kz/mod/resource/view.php?id=111 | Лекция 01 Слайды
https://lms.uni.kz/mod/resource/view.php?id=222 | Лекция 02 Видео
https://lms.uni.kz/mod/resource/view.php?id=333
```

```bash
python main.py --batch lectures.txt
```

### Режим отладки

```bash
python main.py --debug
```

### Все флаги

| Флаг | Описание |
|------|----------|
| `--url URL` | URL одной лекции |
| `--name ИМЯ` | Кастомное имя файла (без расширения) |
| `--batch ФАЙЛ` | Файл со списком лекций |
| `--debug` | Видимый браузер |
| `--no-cleanup` | Не удалять временные файлы |
| `--output DIR` | Папка для файлов (по умолчанию `./output`) |

---

## 📁 Структура проекта

```
корсмос/
├── main.py              ← Точка входа (CLI + авто-роутинг)
├── requirements.txt     ← Зависимости pip
├── .env.example         ← Шаблон конфигурации
├── .gitignore
├── README.md            ← Этот файл
├── src/
│   ├── __init__.py
│   ├── config.py        ← Константы, ContentType enum, Credentials
│   ├── auth.py          ← Playwright авторизация (мультиселекторы)
│   ├── interceptor.py   ← Универсальный Network Interception
│   ├── downloader.py    ← aiohttp параллельная загрузка слайдов
│   ├── video.py         ← Скачивание видео (yt-dlp / ffmpeg / aiohttp)
│   └── assembler.py     ← Pillow → PDF сборка
└── output/              ← PDF + MP4 файлы
```

---

## 🧠 Как это работает

### 1. Авторизация (`src/auth.py`)

Playwright открывает `/login/index.php` и заполняет форму. Поддерживает **8 CSS-селекторов** для username, password, submit.

### 2. Универсальный Network Interception (`src/interceptor.py`)

**Ключевое обновление v3.0**: Один интерцептор слушает ВСЕ запросы и параллельно ищет два типа контента:

```
page.on("request") → каждый запрос проверяется на:
    ├── 🖼️  Слайды:  /1.png, /slide_3.jpg, /pages/2/render
    ├── 📡 Поток:   .m3u8, .mpd (HLS / DASH)
    └── 🎥 Видео:   .mp4, .webm (прямые ссылки)
```

**Приоритет автодетекции**:
1. Если найден `m3u8`/`mpd` → **VIDEO** (потоковое)
2. Если найден `mp4`/`webm` → **VIDEO** (прямое)
3. Если найдены изображения → **SLIDES**

Также:
- Автоподключение к **iframe** (Coursemos)
- Клик по кнопкам viewer и видеоплеера
- Перехват **HTTP-заголовков** (Referer, Cookie) для авторизованного скачивания
- Фильтрация ложных срабатываний (аватарки, иконки, трекеры)

### 3. Слайды → PDF (`src/downloader.py` + `src/assembler.py`)

Без изменений — параллельная загрузка через aiohttp с retry и прогресс-баром, сборка в PDF через Pillow.

### 4. Видео → MP4 (`src/video.py`) — НОВОЕ

Три бэкенда с автоматическим fallback:

| Бэкенд | Когда используется | Плюсы |
|--------|-------------------|-------|
| **yt-dlp** | Приоритет для m3u8/mpd | Умный выбор качества, поддержка cookies |
| **ffmpeg** | Fallback для потоков | `-c copy` без перекодировки (быстро) |
| **aiohttp** | Приоритет для прямых mp4 | Прогресс-бар, максимальная скорость |

Логика fallback:
```
Поток (m3u8/mpd):  yt-dlp → ffmpeg
Прямое видео:      aiohttp → yt-dlp → ffmpeg
```

Фичи:
- Передача **cookies сессии** из Playwright → в yt-dlp/ffmpeg
- Передача **Referer** заголовка (анти-hotlink защита)
- Реальный **стриминг лога ffmpeg/yt-dlp** в консоль
- Таймаут 30 минут для длинных видео

---

## 🐛 Troubleshooting

### ❌ Авторизация не прошла

Запусти с `--debug`, проверь селекторы в `src/auth.py`.

### ❌ Не определяет тип контента

1. `--debug` → открой DevTools → Network
2. Посмотри, какие запросы идут (фильтр: Media/Img)
3. Обнови паттерны в `src/interceptor.py`

### ❌ "ffmpeg/yt-dlp не найден"

```bash
# macOS
brew install ffmpeg
pip install yt-dlp

# Linux
sudo apt install ffmpeg
pip install yt-dlp
```

### ❌ Видео скачивается, но пустое / битое

Попробуй `--debug` и убедись, что видеоплеер реально запустился. Возможно, нужно нажать Play:

```python
# Добавь в src/interceptor.py → _trigger_viewer():
'button.play-button',
```

### ❌ "403 Forbidden" при скачивании видео

Обычно означает, что cookies или Referer не передались. Скрипт автоматически захватывает их из Playwright, но для сложных случаев может понадобиться `--debug` для отладки.

---

## ⚙️ Настройки (`src/config.py`)

| Константа | По умолчанию | Описание |
|-----------|-------------|----------|
| `MAX_SLIDES` | 200 | Верхний предел поиска слайдов |
| `CONCURRENT_DOWNLOADS` | 12 | Параллельных загрузок изображений |
| `INTERCEPT_TIMEOUT` | 30 сек | Ожидание перехвата контента |
| `DOWNLOAD_RETRIES` | 3 | Повторов при ошибке |
| `PDF_DPI` | 200 | Качество PDF |
| `VIDEO_DOWNLOAD_TIMEOUT` | 1800 сек | Таймаут для видео (30 мин) |

---

## 🛡️ Безопасность

- `.env` в `.gitignore`
- `getpass.getpass()` — пароль скрыт
- Cookies передаются только в рамках сессии
- Временные файлы удаляются после обработки

---

## 🏗️ Технологии

| Библиотека | Зачем |
|-----------|-------|
| [Playwright](https://playwright.dev/python/) | Авторизация + Network Interception |
| [aiohttp](https://docs.aiohttp.org/) | Параллельная загрузка слайдов / прямого видео |
| [Pillow](https://python-pillow.org/) | Обработка изображений + PDF |
| [Rich](https://rich.readthedocs.io/) | Красивый CLI |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Скачивание потокового видео (опционально) |
| [ffmpeg](https://ffmpeg.org/) | Fallback для видео-потоков (опционально) |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | Загрузка `.env` |
