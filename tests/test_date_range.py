"""Unit tests for the shared start/end resolver behind the Dashboard's
analytics endpoints (app/api/v1/_date_range.py). Pure function — no DB needed.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.api.v1._date_range import resolve_range


def test_defaults_to_default_days_ending_today():
    rng = resolve_range(None, None, max_span_days=366, default_days=7)
    assert rng.span_days == 7
    assert rng.end - rng.start == timedelta(days=6)


def test_explicit_start_and_end():
    rng = resolve_range(date(2026, 1, 1), date(2026, 1, 30), max_span_days=366)
    assert rng.start == date(2026, 1, 1)
    assert rng.end == date(2026, 1, 30)
    assert rng.span_days == 30
    # end_at is exclusive, one day past the local midnight of `end`.
    assert (rng.end_at - rng.start_at).days == 30


def test_missing_start_derives_from_end_and_default_days():
    rng = resolve_range(None, date(2026, 6, 10), max_span_days=366, default_days=90)
    assert rng.end == date(2026, 6, 10)
    assert rng.start == date(2026, 6, 10) - timedelta(days=89)
    assert rng.span_days == 90


def test_future_end_clamped_to_today():
    far_future = date.today() + timedelta(days=365)
    rng = resolve_range(None, far_future, max_span_days=366, default_days=7)
    assert rng.end == date.today()


def test_start_after_end_rejected():
    with pytest.raises(HTTPException) as exc_info:
        resolve_range(date(2026, 1, 10), date(2026, 1, 1), max_span_days=366)
    assert exc_info.value.status_code == 422


def test_span_wider_than_max_rejected():
    with pytest.raises(HTTPException) as exc_info:
        resolve_range(date(2020, 1, 1), date(2026, 1, 1), max_span_days=366)
    assert exc_info.value.status_code == 422


def test_span_exactly_at_max_allowed():
    rng = resolve_range(date(2025, 1, 1), date(2025, 12, 31), max_span_days=365)
    assert rng.span_days == 365
