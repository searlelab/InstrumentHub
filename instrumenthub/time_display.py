from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


MINUTES_PER_HOUR = Decimal("60")


def format_accounting_duration(minutes: int | Decimal) -> str:
    """Render instrument-time accounting without exposing minute units."""
    total = Decimal(str(minutes))
    sign = "-" if total < 0 else ""
    total = abs(total)
    if total == total.to_integral_value():
        whole_minutes = int(total)
        if whole_minutes % 60 == 0:
            hours = whole_minutes // 60
            days, remainder = divmod(hours, 24)
            parts: list[str] = []
            if days:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if remainder:
                parts.append(f"{remainder} hour{'s' if remainder != 1 else ''}")
            return sign + (" ".join(parts) if parts else "0 hours")
    hours = (total / MINUTES_PER_HOUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    value = format(hours, "f").rstrip("0").rstrip(".") or "0"
    return f"{sign}{value} hours"


def accounting_hours_value(minutes: int | Decimal) -> str:
    """Return a compact decimal-hour value suitable for an HTML input."""
    hours = (Decimal(str(minutes)) / MINUTES_PER_HOUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(hours, "f").rstrip("0").rstrip(".") or "0"


def parse_accounting_hours(value: str | int | Decimal, *, label: str = "Time") -> int:
    """Convert a positive decimal-hour form value to whole minutes."""
    try:
        hours = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a positive number of hours.") from None
    if not hours.is_finite() or hours <= 0:
        raise ValueError(f"{label} must be a positive number of hours.")
    minutes = (hours * MINUTES_PER_HOUR).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if minutes <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return int(minutes)
