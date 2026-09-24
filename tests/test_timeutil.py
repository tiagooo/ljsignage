"""Dates are stored in UTC and shown in Europe/Lisbon, including across DST changes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lj_signage.reconciler import planner as pl
from lj_signage.timeutil import (
    NonexistentLocalTime,
    fmt_bytes,
    fmt_datetime,
    fmt_duration,
    fmt_number,
    fmt_relative,
    fmt_time,
    local_to_utc,
)
from tests.planner_helpers import fs_with, kinds, make_input, make_video, run

LISBON = ZoneInfo("Europe/Lisbon")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- 25/10/2026: 02:00 WEST becomes 01:00 WET ------------------------------------------


def test_repeated_hour_resolves_to_the_first_occurrence():
    value, ambiguous = local_to_utc(datetime(2026, 10, 25, 1, 30), LISBON)  # noqa: DTZ001
    assert ambiguous
    assert value == utc(2026, 10, 25, 0, 30)  # 01:30 summer time (UTC+1)


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 10, 25, 0, 30), utc(2026, 10, 24, 23, 30)),  # noqa: DTZ001
        (datetime(2026, 10, 25, 2, 30), utc(2026, 10, 25, 2, 30)),  # noqa: DTZ001
        (datetime(2026, 7, 1, 12, 0), utc(2026, 7, 1, 11, 0)),  # noqa: DTZ001
        (datetime(2026, 12, 1, 12, 0), utc(2026, 12, 1, 12, 0)),  # noqa: DTZ001
    ],
)
def test_unambiguous_local_times(local, expected):
    assert local_to_utc(local, LISBON) == (expected, False)


def test_spring_gap_is_rejected():
    with pytest.raises(NonexistentLocalTime):
        local_to_utc(datetime(2026, 3, 29, 1, 30), LISBON)  # noqa: DTZ001


def test_a_day_over_the_autumn_change_lasts_25_hours():
    start, _ = local_to_utc(datetime(2026, 10, 25, 0, 0), LISBON)  # noqa: DTZ001
    end, _ = local_to_utc(datetime(2026, 10, 26, 0, 0), LISBON)  # noqa: DTZ001
    assert end - start == timedelta(hours=25)


def test_display_follows_the_wall_clock_across_the_change():
    before = utc(2026, 10, 25, 0, 58)
    after = utc(2026, 10, 25, 1, 3)
    assert fmt_time(before, LISBON) == "01:58"  # summer time
    assert fmt_time(after, LISBON) == "01:03"  # winter time, five minutes later
    assert fmt_datetime(after, LISBON) == "25/10/2026 01:03"


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        fmt_datetime(datetime(2026, 10, 25, 1, 0), LISBON)  # noqa: DTZ001


def test_campaign_window_over_the_change_is_evaluated_in_utc():
    video, fallback = make_video(1), make_video(9)
    start, _ = local_to_utc(datetime(2026, 10, 25, 0, 0), LISBON)  # noqa: DTZ001
    end, _ = local_to_utc(datetime(2026, 10, 25, 3, 0), LISBON)  # noqa: DTZ001
    assert (start, end) == (utc(2026, 10, 24, 23, 0), utc(2026, 10, 25, 3, 0))
    campaign = pl.Want(1, video, start, end, 10)
    reserve = pl.Want(2, fallback, utc(2026, 1, 1), None, 10, True)
    library = (video, fallback)

    fs = fs_with(active=[(fallback, 10)])
    timeline = [
        (utc(2026, 10, 24, 22, 59), False),  # 23:59 WEST
        (utc(2026, 10, 24, 23, 0), True),  # 00:00 WEST: starts
        (utc(2026, 10, 25, 0, 30), True),  # 01:30 WEST
        (utc(2026, 10, 25, 1, 30), True),  # 01:30 WET: the same wall-clock time again
        (utc(2026, 10, 25, 2, 59), True),  # 02:59 WET
        (utc(2026, 10, 25, 3, 0), False),  # 03:00 WET: ends
    ]
    for now, should_play in timeline:
        assert campaign.is_active(now) is should_play
        run(fs, make_input(fs, [campaign, reserve], library, now=now))
        playing = [name.split("_", 1)[1][:7] for name in fs.visible()]
        assert playing == (["video-1"] if should_play else ["video-9"]), now


def test_activation_happens_once_over_the_repeated_hour():
    video, fallback = make_video(1), make_video(9)
    start, _ = local_to_utc(datetime(2026, 10, 25, 1, 30), LISBON)  # noqa: DTZ001
    campaign = pl.Want(1, video, start, None, 10)
    reserve = pl.Want(2, fallback, utc(2026, 1, 1), None, 10, True)
    fs = fs_with(active=[(fallback, 10)], staged=[video])
    first = run(fs, make_input(fs, [campaign, reserve], (video, fallback), now=start))
    assert kinds(first) == ["activate", "deactivate"]
    second_occurrence = start + timedelta(hours=1)  # 01:30 WET
    again = run(fs, make_input(fs, [campaign, reserve], (video, fallback), now=second_occurrence))
    assert again.actions == ()


# --- formatting --------------------------------------------------------------------------


def test_relative_times_in_portuguese():
    now = utc(2026, 9, 24, 12, 0)
    assert fmt_relative(now - timedelta(seconds=20), now, LISBON) == "agora mesmo"
    assert fmt_relative(now - timedelta(minutes=5), now, LISBON) == "há 5 min"
    assert fmt_relative(now - timedelta(hours=3), now, LISBON) == "há 3 h"
    assert fmt_relative(utc(2026, 9, 23, 13, 5), now, LISBON) == "ontem às 14:05"
    assert fmt_relative(now + timedelta(minutes=10), now, LISBON) == "dentro de 10 min"
    assert fmt_relative(None, now, LISBON) == "nunca"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "—"), (42, "42 s"), (60, "1 min"), (200, "3 min 20 s"), (3900, "1 h 05 min")],
)
def test_durations(seconds, text):
    assert fmt_duration(seconds) == text


@pytest.mark.parametrize(
    ("size", "text"),
    [(None, "—"), (512, "512 B"), (12_345_678, "12,3 MB"), (4_000_000_000, "4,0 GB")],
)
def test_sizes_use_a_decimal_comma(size, text):
    assert fmt_bytes(size) == text


@pytest.mark.parametrize(
    ("value", "text"), [(None, "—"), (25.0, "25"), (29.97002997, "29,97"), (12.5, "12,5")]
)
def test_numbers_use_a_decimal_comma(value, text):
    assert fmt_number(value) == text
