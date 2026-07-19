"""Verify get_conn is usable across threads without check_same_thread errors."""

import threading

from app.db import repo
from app.db.connection import get_conn


def test_get_conn_per_thread(db):
    ids = {}

    def worker(name):
        ids[name] = id(get_conn())

    ids["main"] = id(get_conn())
    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Each thread got its own connection object.
    assert len(set(ids.values())) == len(ids)


def test_concurrent_writes_from_threads(db):
    errors = []

    def writer(ticker):
        try:
            repo.insert_trade(ticker, "buy", 1, 100.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(f"TK{i}",)) for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(repo.get_trades()) == 10
