"""user_profile repository tests."""

from __future__ import annotations

import pytest

from app import db


def test_default_cash_balance(temp_db: str) -> None:
    assert db.profile.get_cash_balance() == 10_000.0


def test_set_and_get_cash_balance(temp_db: str) -> None:
    db.profile.set_cash_balance(12_345.67)
    assert db.profile.get_cash_balance() == 12_345.67


def test_unknown_user_raises(temp_db: str) -> None:
    with pytest.raises(ValueError):
        db.profile.get_cash_balance(user_id="nope")
