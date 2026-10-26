"""Bounded exponential backoff for *ordinary* connection failures.

FloodWait is never handled with this class: Telegram prescribes the exact wait.
"""
from __future__ import annotations

import random
from typing import Callable


class Backoff:
    def __init__(self, base: float = 2.0, factor: float = 2.0, cap: float = 300.0,
                 jitter: float = 0.1, rng: Callable[[], float] = random.random) -> None:
        self.base = base
        self.factor = factor
        self.cap = cap
        self.jitter = jitter
        self._rng = rng
        self.attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (self.factor ** self.attempt))
        self.attempt += 1
        # jitter only de-synchronises reconnects; it never shortens the delay below the base value
        return min(self.cap, delay * (1 + self.jitter * self._rng()))

    def reset(self) -> None:
        self.attempt = 0
