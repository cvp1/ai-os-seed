#!/usr/bin/env python3
"""Event Bus — local SQLite append-only event log for the agent framework.

Every agent publishes and subscribes through this bus. Design:

  - SQLite-backed, single file, no external deps (stdlib)
  - Append-only: events are never deleted, only marked processed
  - Subscribers track their cursor via since_id (last event they read)
  - Lightweight: ~1KB per 1000 events at our volume

Usage:
    bus = EventBus()

    bus.publish("watchdog", "motion_detected",
                {"camera": "driveway", "label": "person"})

    for event in bus.subscribe(since_id=0, type="motion_detected"):
        print(event["payload"])
        bus.ack(event["id"])
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class EventBus:
    """Thread-safe SQLite event bus."""

    def __init__(self, db_path=None):
        if db_path is None:
            CC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "event-bus", "events.db")
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self):
        """Per-thread connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts TEXT NOT NULL,"
            "  source TEXT NOT NULL,"
            "  type TEXT NOT NULL,"
            "  payload TEXT NOT NULL DEFAULT '{}',"
            "  processed INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed)"
        )
        conn.commit()
        conn.close()

    def publish(self, source, type, payload=None):
        """Publish one event. Returns the event id."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO events (ts, source, type, payload) VALUES (?, ?, ?, ?)",
            (ts, source, type, payload_json),
        )
        self._conn.commit()
        return cur.lastrowid

    def subscribe(self, since_id=0, source=None, type=None, limit=100):
        """Yield events newer than ``since_id``, optionally filtered.

        Each event is a dict with keys: id, ts, source, type, payload.
        The caller should ``ack(event_id)`` after processing.
        """
        query = "SELECT id, ts, source, type, payload FROM events WHERE id > ?"
        params = [since_id]
        if source:
            query += " AND source = ?"
            params.append(source)
        if type:
            query += " AND type = ?"
            params.append(type)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        cur = self._conn.execute(query, params)
        for row in cur.fetchall():
            yield {
                "id": row[0],
                "ts": row[1],
                "source": row[2],
                "type": row[3],
                "payload": json.loads(row[4]) if row[4] else {},
            }

    def subscribe_blocking(self, since_id=0, source=None, type=None,
                           poll_interval=0.5, timeout=None):
        """Blocking generator: yields events as they arrive (polls).

        ``poll_interval`` — seconds between polls (default 0.5).
        ``timeout`` — max seconds to wait total (None = forever).
        """
        start = time.monotonic()
        cursor = since_id
        while True:
            if timeout and (time.monotonic() - start) > timeout:
                return
            for event in self.subscribe(since_id=cursor, source=source, type=type):
                cursor = event["id"]
                yield event
            time.sleep(poll_interval)

    def ack(self, event_id):
        """Mark an event processed."""
        self._conn.execute("UPDATE events SET processed=1 WHERE id=?", (event_id,))
        self._conn.commit()

    def ack_many(self, event_ids):
        """Mark multiple events processed."""
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self._conn.execute(
            "UPDATE events SET processed=1 WHERE id IN (%s)" % placeholders,
            event_ids,
        )
        self._conn.commit()

    def last_id(self):
        """Return the highest event id (for initial subscribe)."""
        cur = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
        return cur.fetchone()[0]

    def stats(self):
        """Return summary dict for health checks."""
        cur = self._conn.execute(
            "SELECT COUNT(*), SUM(processed), MAX(id) FROM events"
        )
        row = cur.fetchone()
        total, processed, max_id = (row[0] or 0), (row[1] or 0), (row[2] or 0)
        # Count by type
        cur = self._conn.execute(
            "SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY COUNT(*) DESC LIMIT 10"
        )
        by_type = {row[0]: row[1] for row in cur.fetchall()}
        return {
            "total_events": total,
            "processed": processed,
            "unprocessed": total - processed,
            "max_id": max_id,
            "by_type": by_type,
            "db_path": self._db_path,
        }

    def vacuum(self):
        """Reclaim space (run weekly)."""
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("VACUUM")
        self._conn.execute("PRAGMA journal_mode=WAL")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _selftest():
    import tempfile
    tmp = tempfile.mktemp(suffix=".db")
    bus = EventBus(tmp)
    failures, ran = [], [0]

    def check(name, cond):
        ran[0] += 1
        print("  %-60s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            failures.append(name)

    eid = bus.publish("watchdog", "motion_detected", {"cam": "doorbell"})
    check("publish returns event_id", eid == 1)
    check("last_id returns 1 after 1 event", bus.last_id() == 1)

    events = list(bus.subscribe(since_id=0))
    check("subscribe returns 1 event", len(events) == 1)
    check("event has correct source", events[0]["source"] == "watchdog")
    check("event has correct type", events[0]["type"] == "motion_detected")
    check("event payload is dict", events[0].get("payload", {}).get("cam") == "doorbell")

    bus.ack(events[0]["id"])
    stats = bus.stats()
    check("ack sets processed=1", stats["processed"] == 1)
    check("stats shows total", stats["total_events"] == 1)

    # subscribe with type filter
    bus.publish("inbox_triage", "new_email", {"subject": "Hello"})
    bus.publish("watchdog", "sensor_alert", {"sensor": "temp"})
    filtered = list(bus.subscribe(since_id=0, type="new_email"))
    check("type filter works", len(filtered) == 1)
    check("filtered has the right type", filtered[0]["type"] == "new_email")

    # subscribe with source filter
    src_filtered = list(bus.subscribe(since_id=0, source="watchdog"))
    check("source filter works", len(src_filtered) == 2)

    # ack_many
    new_ids = []
    for i in range(3):
        eid = bus.publish("test", "test_type", {"n": i})
        new_ids.append(eid)
    bus.ack_many(new_ids)
    stats = bus.stats()
    # 4 processed: 1 (first ack) + 3 (ack_many of events 4-6)
    check("processed=4 after ack_many", stats["processed"] == 4)

    # by_type in stats
    check("by_type has watchdog category",
          stats["by_type"].get("motion_detected", 0) >= 1)

    # subscribe_blocking (with short timeout)
    polling_events = list(bus.subscribe_blocking(
        since_id=bus.last_id(), type="test_type", poll_interval=0.1, timeout=0.3))
    check("blocking with no new events returns empty", len(polling_events) == 0)

    # Publish and catch via blocking (cross-thread publish)
    import threading as th
    def delayed_publish():
        time.sleep(0.2)
        bus.publish("test", "late_event", {"msg": "hello"})
    th.Thread(target=delayed_publish, daemon=True).start()
    late_events = list(bus.subscribe_blocking(
        since_id=bus.last_id(), poll_interval=0.1, timeout=2.0))
    check("blocking catches cross-thread event", len(late_events) >= 1)
    check("late event has correct payload",
          late_events[0].get("payload", {}).get("msg") == "hello")

    os.unlink(tmp)
    print("\n%s (%d checks, %d failed)"
          % ("PASS" if not failures else "FAIL", ran[0], len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--selftest" in args:
        raise SystemExit(_selftest())
    # CLI for manual inspection
    bus = EventBus()
    if "publish" in args and len(args) >= 4:
        idx = args.index("publish")
        eid = bus.publish(args[idx + 1], args[idx + 2],
                          json.loads(args[idx + 3]) if len(args) > idx + 3 else {})
        print(json.dumps({"published": eid}))
    elif "subscribe" in args:
        idx = args.index("subscribe")
        since = int(args[idx + 1]) if len(args) > idx + 1 else 0
        events = list(bus.subscribe(since_id=since))
        print(json.dumps(events, indent=2, ensure_ascii=False, default=str))
    elif "stats" in args:
        print(json.dumps(bus.stats(), indent=2))
    else:
        print("Usage: event_bus.py publish <source> <type> [payload_json]")
        print("       event_bus.py subscribe [since_id]")
        print("       event_bus.py stats")
        print("       event_bus.py --selftest")
