"""Minimal 5-field cron parser + next-fire computation (spec §5.8).

Supports the standard ``minute hour day-of-month month day-of-week``
format with the usual wildcards (``*``), step values (``*/5``), lists
(``0,15,30``), and ranges (``9-17``).  No support for ``@yearly`` /
``L`` / ``W`` / ``#`` — those are out of scope for the platform.

Why hand-rolled instead of ``croniter``?  croniter has a non-trivial
dependency footprint (pytz, python-dateutil) and pulls in features we
don't need.  The parser here is ~80 lines and tested end-to-end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


_MINUTE_MAX = 59
_HOUR_MAX = 23
_DOM_MAX = 31
_MONTH_MAX = 12
_DOW_MAX = 6  # 0 = Monday … 6 = Sunday


@dataclass(frozen=True)
class CronExpression:
    minutes: set[int]
    hours: set[int]
    days_of_month: set[int]
    months: set[int]
    days_of_week: set[int]


def _parse_field(token: str, *, lo: int, hi: int) -> set[int]:
    """Parse a single cron field (e.g. ``*/5``, ``0,15``, ``9-17``)."""
    out: set[int] = set()
    for piece in token.split(","):
        piece = piece.strip()
        if not piece:
            raise ValueError(f"empty cron field token: {token!r}")
        step = 1
        if "/" in piece:
            base, step_str = piece.split("/", 1)
            try:
                step = int(step_str)
            except ValueError as exc:
                raise ValueError(f"bad step in cron field: {piece!r}") from exc
            if step <= 0:
                raise ValueError(f"step must be > 0: {piece!r}")
        else:
            base = piece
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_str, end_str = base.split("-", 1)
            try:
                start = int(start_str)
                end = int(end_str)
            except ValueError as exc:
                raise ValueError(f"bad range in cron field: {base!r}") from exc
        else:
            try:
                value = int(base)
            except ValueError as exc:
                raise ValueError(f"bad value in cron field: {base!r}") from exc
            if step != 1:
                # ``N/STEP`` syntax means ``start at N, every STEP``.
                start, end = value, hi
            else:
                if value < lo or value > hi:
                    raise ValueError(
                        f"value {value} out of range [{lo}, {hi}] in field {token!r}",
                    )
                out.add(value)
                continue
        for v in range(start, end + 1, step):
            if v < lo or v > hi:
                raise ValueError(
                    f"value {v} out of range [{lo}, {hi}] in field {token!r}",
                )
            out.add(v)
    return out


def parse_cron(expr: str) -> CronExpression:
    """Parse a 5-field cron expression.  Raises ``ValueError`` on bad input."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"cron must have 5 fields (minute hour dom month dow), got {len(fields)}",
        )
    return CronExpression(
        minutes=_parse_field(fields[0], lo=0, hi=_MINUTE_MAX),
        hours=_parse_field(fields[1], lo=0, hi=_HOUR_MAX),
        days_of_month=_parse_field(fields[2], lo=1, hi=_DOM_MAX),
        months=_parse_field(fields[3], lo=1, hi=_MONTH_MAX),
        days_of_week=_parse_field(fields[4], lo=0, hi=_DOW_MAX),
    )


def next_fire_after(expr: CronExpression, start: datetime) -> datetime:
    """Return the next ``datetime >= start + 1 minute`` matching ``expr``.

    Iterates minute-by-minute up to ~4 years; cheap enough for the
    platform's scheduling cadence (most schedules fire every N minutes,
    not N seconds).
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    # Start from the next minute so ``start`` itself doesn't count as "due".
    candidate = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
    horizon = candidate + timedelta(days=365 * 4)
    while candidate <= horizon:
        # cron day-of-week: 0 = Sunday … 6 = Saturday.
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            candidate.minute in expr.minutes
            and candidate.hour in expr.hours
            and candidate.month in expr.months
            and candidate.day in expr.days_of_month
            and cron_weekday in expr.days_of_week
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("no fire time found within 4 years")


__all__ = [
    "CronExpression",
    "next_fire_after",
    "parse_cron",
]