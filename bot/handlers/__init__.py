"""Telegram-хэндлеры."""

from .commands import start_command
from .inline import inline_query, chosen_inline_result
from .callbacks import noop_callback

__all__ = [
    "start_command",
    "inline_query",
    "chosen_inline_result",
    "noop_callback",
]
