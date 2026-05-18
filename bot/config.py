"""Конфигурация и константы проекта."""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Токены и ID
BOT_TOKEN = os.getenv("BOT_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")

# Пути
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
CACHE_FILE = Path("cache.json")
HISTORY_FILE = Path("history.json")

# Лимиты Telegram
TG_MESSAGE_LIMIT = 4096
TG_CAPTION_LIMIT = 1024

# Инлайн-выдача
INLINE_RESULTS_LIMIT = 10
LYRICS_RESULTS_LIMIT = 5

# Pending-кэш (между inline_query и chosen_inline_result)
PENDING_CACHE_MAX = 500
PENDING_CACHE_TRIM = 100

# Сколько треков хранить в истории каждого пользователя
HISTORY_LIMIT = 50

# Браузерный UA — без него SoundCloud иногда отдаёт 403
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {"User-Agent": BROWSER_UA}

# Триггеры для переключения в режим поиска текстов
LYRICS_SUFFIXES = (" text", " текст")
LYRICS_SHORT_SUFFIXES = (" t", " т")

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pliserload")
