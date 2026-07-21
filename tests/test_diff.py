"""
Tests for the change-detection core. Run standalone:

    python tests/test_diff.py

or with pytest:

    pytest -q
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# make the top-level module importable when run from repo root or tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skylrk_monitor as m  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
LOG = logging.getLogger("test")
LOG.addHandler(logging.NullHandler())
CFG = m.load_config(None)  # defaults: target XS/S, console channel


def _snap(name):
    return m.build_snapshot(m.load_fixture(os.path.join(FIX, name)), CFG)


def _seed(snapshot, dt):
    """Return a state as if we had seeded from `snapshot` (no alerts)."""
    empty = {"schema_version": 1, "last_check": {}, "products": {}, "ledger": {}}
    return m.update_state(empty, snapshot, [], set(), dt)


def test_clothing_filter_excludes_accessories():
    snap = _snap("run1_baseline.json")
    # hoodie + t-shirt kept; sunglasses dropped
    assert set(snap.keys()) == {"1001", "1002"}, snap.keys()
    print("PASS clothing filter excludes sunglasses")


def test_first_run_seeds_without_alerts():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    snap = _snap("run1_baseline.json")
    empty = {"schema_version": 1, "last_check": {}, "products": {}, "ledger": {}}
    events = m.detect_events(empty, snap, CFG, dt, LOG)
    assert events == [], events
    print("PASS first run produces no alerts")


def test_no_change_no_events():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    snap = _snap("run1_baseline.json")
    prev = _seed(snap, dt)
    events = m.detect_events(prev, snap, CFG, dt + timedelta(minutes=15), LOG)
    assert events == [], events
    print("PASS identical snapshot produces no alerts")


def test_xs_restock_fires_once():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    prev = _seed(_snap("run1_baseline.json"), dt)
    snap2 = _snap("run2_restock.json")
    events = m.detect_events(prev, snap2, CFG, dt + timedelta(minutes=15), LOG)
    assert len(events) == 1, [e["type"] for e in events]
    e = events[0]
    assert e["type"] == "XS_S_RESTOCK"
    assert e["product_id"] == "1001"
    assert e["sizes"] == ["XS"]
    assert e["color"] == "THISTLE"
    print("PASS XS restock fires exactly one correct alert")


def test_new_product_fires_once():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    prev = _seed(_snap("run2_restock.json"), dt)
    snap3 = _snap("run3_newproduct.json")
    events = m.detect_events(prev, snap3, CFG, dt + timedelta(minutes=15), LOG)
    assert len(events) == 1, [e["type"] for e in events]
    e = events[0]
    assert e["type"] == "NEW_LAUNCH"
    assert e["product_id"] == "1004"
    assert "S" in e["sizes"]
    print("PASS new product fires exactly one launch alert")


def test_price_change_fires_update_only():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    prev = _seed(_snap("run1_baseline.json"), dt)
    snap4 = _snap("run4_pricechange.json")
    events = m.detect_events(prev, snap4, CFG, dt + timedelta(minutes=15), LOG)
    assert len(events) == 1, [e["type"] for e in events]
    e = events[0]
    assert e["type"] == "PRODUCT_UPDATE"
    assert e["product_id"] == "1001"
    assert "120.00" in e["changed"] and "140.00" in e["changed"]
    print("PASS price change fires exactly one update alert")


def test_cooldown_suppresses_duplicate():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    prev = _seed(_snap("run1_baseline.json"), dt)
    snap2 = _snap("run2_restock.json")
    later = dt + timedelta(minutes=15)
    # Inject the restock signature into the ledger as if just alerted
    sig = "restock:1001:THISTLE:XS"
    prev["ledger"][sig] = later.isoformat(timespec="seconds")
    suppressed = m.detect_events(prev, snap2, CFG, later + timedelta(hours=1), LOG)
    assert suppressed == [], suppressed
    # After the cooldown window (default 6h) it may alert again
    prev["ledger"][sig] = dt.isoformat(timespec="seconds")
    allowed = m.detect_events(prev, snap2, CFG, dt + timedelta(hours=7), LOG)
    assert len(allowed) == 1, allowed
    print("PASS cooldown suppresses duplicate then allows after window")


def test_at_least_once_on_failed_send():
    """If a send fails, the product keeps its old state so it retries next run."""
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    prev = _seed(_snap("run1_baseline.json"), dt)
    snap2 = _snap("run2_restock.json")
    # simulate: event detected but send failed for product 1001
    state = m.update_state(prev, snap2, sent_events=[], failed_pids={"1001"}, dt=dt)
    # 1001 kept OLD (XS still unavailable) so next run re-detects the restock
    assert state["products"]["1001"]["variants"]["20011"]["available"] is False
    events_next = m.detect_events(state, snap2, CFG, dt + timedelta(minutes=15), LOG)
    assert any(e["type"] == "XS_S_RESTOCK" for e in events_next)
    print("PASS failed send retries on next run (at-least-once)")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(ALL) - failures}/{len(ALL)} tests passed")
    sys.exit(1 if failures else 0)
