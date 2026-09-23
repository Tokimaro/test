"""Circuit breaker (раздел 6.4 плана): слишком много ошибок API за окно → пауза торговли."""

from collections import deque


class CircuitBreaker:
    def __init__(self, max_errors: int = 5, window_ms: int = 10 * 60_000) -> None:
        self.max_errors = max_errors
        self.window_ms = window_ms
        self._errors: deque[int] = deque()
        self.tripped = False

    def record(self, ts: int) -> bool:
        """Регистрирует ошибку. True — выключатель сработал именно сейчас."""
        self._errors.append(ts)
        while self._errors and ts - self._errors[0] > self.window_ms:
            self._errors.popleft()
        if not self.tripped and len(self._errors) >= self.max_errors:
            self.tripped = True
            return True
        return False

    def reset(self) -> None:
        self._errors.clear()
        self.tripped = False
