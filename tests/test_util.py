"""Basecamp timestamp parsing."""
from datetime import datetime, timezone

from app.util import parse_bc_datetime


def test_parses_zulu_with_millis():
    dt = parse_bc_datetime("2024-01-02T15:04:05.000Z")
    assert dt == datetime(2024, 1, 2, 15, 4, 5, tzinfo=timezone.utc)


def test_parses_offset_form():
    dt = parse_bc_datetime("2024-01-02T15:04:05+02:00")
    assert dt.utcoffset().total_seconds() == 2 * 3600


def test_naive_is_assumed_utc():
    dt = parse_bc_datetime("2024-01-02T15:04:05")
    assert dt.tzinfo == timezone.utc


def test_none_and_empty():
    assert parse_bc_datetime(None) is None
    assert parse_bc_datetime("") is None
