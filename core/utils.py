import asyncio
import random
from typing import Any


async def random_delay(min_sec: float, max_sec: float):
    """Случайная задержка в заданном диапазоне."""
    delay = random.uniform(min_sec, max_sec)
    await asyncio.sleep(delay)


def random_choice(options: list) -> Any:
    return random.choice(options)


def random_float(min_val: float, max_val: float, decimals: int = 2) -> float:
    return round(random.uniform(min_val, max_val), decimals)


def random_int(min_val: int, max_val: int) -> int:
    return random.randint(min_val, max_val)
