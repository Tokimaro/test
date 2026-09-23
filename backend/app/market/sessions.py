"""Торговые сессии фондового рынка (раздел 7.1 плана: часы торгов, выходные, праздники)."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


@dataclass
class SessionCalendar:
    """Интервалы регулярных сессий [open_ms, close_ms). Если для даты нет данных
    (календарь брокера не загружен), используется стандартное расписание NYSE по будням."""

    sessions: dict[date, tuple[int, int]] = field(default_factory=dict)

    def load(self, days: list[tuple[date, time, time]]) -> None:
        """days — (дата, открытие, закрытие) по времени Нью-Йорка (как в календаре брокера)."""
        for d, o, c in days:
            self.sessions[d] = (_ny_ms(d, o), _ny_ms(d, c))

    def session_for(self, ts_ms: int) -> tuple[int, int] | None:
        d = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(NY).date()
        if d in self.sessions:
            return self.sessions[d]
        if self.sessions and min(self.sessions) <= d <= max(self.sessions):
            return None  # внутри загруженного диапазона нет записи — праздник/выходной
        if d.weekday() >= 5:
            return None
        return _ny_ms(d, REGULAR_OPEN), _ny_ms(d, REGULAR_CLOSE)

    def is_open(self, ts_ms: int, buffer_minutes: int = 0) -> bool:
        s = self.session_for(ts_ms)
        if s is None:
            return False
        buf = buffer_minutes * 60_000
        return s[0] + buf <= ts_ms < s[1] - buf


def _ny_ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=NY).timestamp() * 1000)


def parse_day(d: str, o: str, c: str) -> tuple[date, time, time]:
    """Формат календаря Alpaca: date=YYYY-MM-DD, open/close=HH:MM."""
    return date.fromisoformat(d), time.fromisoformat(o), time.fromisoformat(c)


def days_ahead(start: date, n: int) -> tuple[date, date]:
    return start, start + timedelta(days=n)
