"""Персистентное хранилище: кэш file_id и per-user история скачиваний."""

import json

from .config import CACHE_FILE, HISTORY_FILE, HISTORY_LIMIT, logger

# ─────────────────────────────────────────────────────────────────────────────
# Кэш file_id (track_id → telegram file_id)
# ─────────────────────────────────────────────────────────────────────────────

_file_id_cache: dict[str, str] = {}


def load_cache() -> None:
    """Подгружаем cache.json при старте."""
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _file_id_cache.update(data)
            logger.info(f"[CACHE] загружено {len(_file_id_cache)} записей")
    except Exception as e:
        logger.warning(f"[CACHE] не смог прочитать cache.json: {e}")


def save_cache() -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps(_file_id_cache, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[CACHE] не смог сохранить cache.json: {e}")


def cache_get(track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return _file_id_cache.get(str(track_id))


def cache_set(track_id: int, file_id: str) -> None:
    _file_id_cache[str(track_id)] = file_id
    save_cache()


# ─────────────────────────────────────────────────────────────────────────────
# История скачиваний (per-user, персистентная)
# ─────────────────────────────────────────────────────────────────────────────

_user_history: dict[str, list[dict]] = {}


def load_history() -> None:
    """Подгружаем history.json при старте."""
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _user_history.update(data)
            total = sum(len(v) for v in _user_history.values())
            logger.info(f"[HISTORY] загружено {total} записей для {len(_user_history)} юзеров")
    except Exception as e:
        logger.warning(f"[HISTORY] не смог прочитать history.json: {e}")


def save_history() -> None:
    try:
        HISTORY_FILE.write_text(
            json.dumps(_user_history, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[HISTORY] не смог сохранить history.json: {e}")


def history_get(user_id: int) -> list[dict]:
    """Получить историю конкретного пользователя."""
    return _user_history.get(str(user_id), [])


def history_push(user_id: int, item: dict) -> None:
    """Добавить трек в историю пользователя и сохранить."""
    key = str(user_id)
    if key not in _user_history:
        _user_history[key] = []

    # Дедупликация — перемещаем в конец
    track_id = (item.get("track") or {}).get("id")
    if track_id:
        _user_history[key] = [
            h for h in _user_history[key]
            if (h.get("track") or {}).get("id") != track_id
        ]

    _user_history[key].append(item)
    if len(_user_history[key]) > HISTORY_LIMIT:
        _user_history[key] = _user_history[key][-HISTORY_LIMIT:]

    save_history()
