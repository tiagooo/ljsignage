"""Time helpers: stored in UTC with tzinfo, shown in Europe/Lisbon (CLAUDE.md rule 7).

Wall-clock input from the panel is converted with local_to_utc(), which rejects
times skipped by the spring DST change and resolves the repeated hour of the
autumn change (e.g. 25/10/2026 01:30) to its first occurrence (summer time).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class NonexistentLocalTime(ValueError):
    """The wall-clock time does not exist in the timezone (DST gap)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: use timezone-aware UTC datetimes")
    return dt.astimezone(UTC)


def local_to_utc(naive: datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Convert a naive wall-clock time in ``tz`` to UTC.

    Returns ``(utc_datetime, ambiguous)``. Raises NonexistentLocalTime when the
    time falls in a DST gap.
    """
    if naive.tzinfo is not None:
        raise ValueError("expected a naive wall-clock datetime")
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        raise NonexistentLocalTime(naive.isoformat())
    ambiguous = first.utcoffset() != second.utcoffset()
    return first.astimezone(UTC), ambiguous


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return ensure_utc(dt).astimezone(tz)


def fmt_datetime(dt: datetime | None, tz: ZoneInfo) -> str:
    return "" if dt is None else to_local(dt, tz).strftime("%d/%m/%Y %H:%M")


def fmt_date(dt: datetime | None, tz: ZoneInfo) -> str:
    return "" if dt is None else to_local(dt, tz).strftime("%d/%m/%Y")


def fmt_time(dt: datetime | None, tz: ZoneInfo) -> str:
    return "" if dt is None else to_local(dt, tz).strftime("%H:%M")


def fmt_relative(dt: datetime | None, now: datetime, tz: ZoneInfo) -> str:
    """Short relative description in Portuguese ("há 5 min", "ontem às 14:05")."""
    if dt is None:
        return "nunca"
    delta = (ensure_utc(now) - ensure_utc(dt)).total_seconds()
    local, local_now = to_local(dt, tz), to_local(now, tz)
    days = (local_now.date() - local.date()).days
    if delta >= 0:
        if delta < 60:
            return "agora mesmo"
        if delta < 3600:
            return f"há {int(delta // 60)} min"
        if days == 0:
            return f"há {int(delta // 3600)} h"
        if days == 1:
            return f"ontem às {local:%H:%M}"
        return local.strftime("%d/%m/%Y %H:%M")
    ahead = -delta
    if ahead < 60:
        return "dentro de instantes"
    if ahead < 3600:
        return f"dentro de {int(ahead // 60)} min"
    if days == 0:
        return f"hoje às {local:%H:%M}"
    if days == -1:
        return f"amanhã às {local:%H:%M}"
    return local.strftime("%d/%m/%Y %H:%M")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {secs:02d} s" if secs else f"{minutes} min"


def fmt_bytes(size: int | None) -> str:
    """Decimal units with a Portuguese decimal comma (SD cards are sold in decimal GB)."""
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}".replace(".", ",")
        value /= 1000
    raise AssertionError("unreachable")


def fmt_number(value: float | None, decimals: int = 2) -> str:
    """Portuguese decimal comma, without useless trailing zeros (25 → "25", 29.97 → "29,97")."""
    if value is None:
        return "—"
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{value:.0f}"
    return text.replace(".", ",")
