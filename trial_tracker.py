"""Trial usage tracker for Pulse AI Front Desk trials.

Trial terms (Dylan, Nov 18 2026): "14 days free, up to 40 minutes" —
whichever comes first. Enforced OUTSIDE the Retell agent:

  1. TRACKING lives here, called from the Railway retell-webhook
     receiver (see app-with-trial.py): every call_analyzed event for
     a trial agent adds its duration to that agent's cumulative
     minutes. Nothing is ever silently undercounted — if a call
     arrives with no usable duration, a `duration_unknown` alert is
     raised so a human checks Retell dashboard usage.
  2. ALERTS surface through GET /trial-status, read by the existing
     retell-lead-poll cron (15 min, extended — see poll-extension.md):
     owner + Dylan are notified at 30 minutes and at 40 minutes.
  3. STOP happens in the Retell dashboard: when the 40-minute (or
     14-day) alert fires, the trial number is reassigned to the
     pre-built "Pulse Trial Fallback" agent (forward-only / voicemail
     — takes a message or forwards to the owner, never books or
     quotes). One click; the agent stops answering as a front desk
     until the client converts to $30/mo.

Storage: same SQLite DB as the webhook receiver (/data/events.db).
Two tables, created by init_trial_tables():
  trial_usage — one row per trial agent.
  trial_alerts — one row per threshold event, delivered flag cleared
    by the poll worker after Dylan + owner are notified.

Run the self-test:  python3 trial_tracker.py
"""
import sqlite3
import time
from datetime import datetime, timezone

TRIAL_DAYS = 14
TRIAL_MINUTES_CAP = 40
WARN_MINUTES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_usage (
  agent_id TEXT PRIMARY KEY,
  business_name TEXT,
  owner_email TEXT,
  owner_phone TEXT,
  trial_started_at REAL NOT NULL,
  trial_ends_at REAL NOT NULL,
  minutes_total REAL NOT NULL DEFAULT 0,
  notified_30 INTEGER NOT NULL DEFAULT 0,
  notified_40 INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  last_call_id TEXT
);
CREATE TABLE IF NOT EXISTS trial_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  minutes_total REAL,
  created_at REAL NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0
);
"""


def init_trial_tables(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def start_trial(conn, agent_id, business_name, owner_email, owner_phone, now=None):
    """Register a trial agent at onboarding. Trial ends 14 days from now."""
    now = now if now is not None else time.time()
    conn.execute(
        """INSERT OR REPLACE INTO trial_usage
           (agent_id, business_name, owner_email, owner_phone,
            trial_started_at, trial_ends_at, minutes_total,
            notified_30, notified_40, status, last_call_id)
           VALUES (?,?,?,?,?,?,0,0,0,'active',NULL)""",
        (agent_id, business_name, owner_email, owner_phone,
         now, now + TRIAL_DAYS * 86400),
    )
    conn.commit()


def convert_trial(conn, agent_id):
    """Client paid $30/mo — stop tracking, stop the cap."""
    conn.execute("UPDATE trial_usage SET status='converted' WHERE agent_id=?", (agent_id,))
    conn.commit()


def extract_duration_minutes(payload):
    """Pull call length (minutes) out of a Retell call_analyzed payload.

    Retell sends the call object with duration_ms. Shapes vary by
    event version, so try the known paths in order. Returns a float
    or None when no usable duration is present (caller must raise a
    duration_unknown alert — never guess 0).
    """
    if not isinstance(payload, dict):
        return None
    candidates = []
    for path in (("call", "duration_ms"), ("data", "call", "duration_ms"),
                 ("call", "duration"), ("duration_ms",),
                 ("call", "call_duration_seconds"), ("data", "duration_ms")):
        node = payload
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, (int, float)):
            candidates.append((path, node))
    if not candidates:
        return None
    path, value = candidates[0]
    if path[-1] in ("duration_ms",):
        return value / 60000.0
    if path[-1] in ("call_duration_seconds",):
        return value / 60.0
    return float(value)  # already minutes


def _raise_alert(conn, agent_id, kind, minutes_total, now):
    conn.execute(
        "INSERT INTO trial_alerts (agent_id, kind, minutes_total, created_at, delivered)"
        " VALUES (?,?,?,?,0)",
        (agent_id, kind, minutes_total, now),
    )


def record_call(conn, agent_id, call_id, duration_minutes, now=None):
    """Add one call's minutes to a trial agent. Returns alert kinds raised.

    Idempotent on call_id (Retell retries webhooks). No-ops for agents
    that are not on an active trial.
    """
    now = now if now is not None else time.time()
    row = conn.execute(
        "SELECT minutes_total, notified_30, notified_40, status, last_call_id,"
        " trial_ends_at FROM trial_usage WHERE agent_id=?", (agent_id,)
    ).fetchone()
    if not row:
        return []
    minutes_total, n30, n40, status, last_call_id, trial_ends_at = row
    if status != "active":
        return []
    if call_id and call_id == last_call_id:
        return []  # duplicate delivery — already counted
    alerts = []
    if duration_minutes is None:
        _raise_alert(conn, agent_id, "duration_unknown", minutes_total, now)
        conn.execute("UPDATE trial_usage SET last_call_id=? WHERE agent_id=?",
                     (call_id, agent_id))
        conn.commit()
        return ["duration_unknown"]
    minutes_total = minutes_total + duration_minutes
    conn.execute(
        "UPDATE trial_usage SET minutes_total=?, last_call_id=? WHERE agent_id=?",
        (minutes_total, call_id, agent_id),
    )
    if not n30 and minutes_total >= WARN_MINUTES:
        _raise_alert(conn, agent_id, "trial_30", minutes_total, now)
        conn.execute("UPDATE trial_usage SET notified_30=1 WHERE agent_id=?", (agent_id,))
        alerts.append("trial_30")
    if not n40 and minutes_total >= TRIAL_MINUTES_CAP:
        _raise_alert(conn, agent_id, "trial_40", minutes_total, now)
        conn.execute(
            "UPDATE trial_usage SET notified_40=1, status='exhausted' WHERE agent_id=?",
            (agent_id,),
        )
        alerts.append("trial_40")
    if now >= trial_ends_at:
        _raise_alert(conn, agent_id, "trial_14d", minutes_total, now)
        conn.execute("UPDATE trial_usage SET status='exhausted' WHERE agent_id=?", (agent_id,))
        if "trial_14d" not in alerts:
            alerts.append("trial_14d")
    conn.commit()
    return alerts


def check_expiry(conn, now=None):
    """Sweep for trials past their 14 days with no recent call. Returns alert kinds raised."""
    now = now if now is not None else time.time()
    alerts = []
    rows = conn.execute(
        "SELECT agent_id, minutes_total FROM trial_usage"
        " WHERE status='active' AND trial_ends_at <= ?",
        (now,),
    ).fetchall()
    for agent_id, minutes_total in rows:
        _raise_alert(conn, agent_id, "trial_14d", minutes_total, now)
        conn.execute("UPDATE trial_usage SET status='exhausted' WHERE agent_id=?", (agent_id,))
        alerts.append("trial_14d")
    conn.commit()
    return alerts


def pending_alerts(conn):
    """Undelivered alerts with the trial row attached, oldest first."""
    return conn.execute(
        """SELECT a.id, a.agent_id, a.kind, a.minutes_total, a.created_at,
                  u.business_name, u.owner_email, u.owner_phone, u.status
           FROM trial_alerts a JOIN trial_usage u ON u.agent_id = a.agent_id
           WHERE a.delivered = 0 ORDER BY a.created_at ASC"""
    ).fetchall()


def mark_delivered(conn, alert_ids):
    if not alert_ids:
        return
    conn.execute(
        f"UPDATE trial_alerts SET delivered=1 WHERE id IN ({','.join('?' * len(alert_ids))})",
        list(alert_ids),
    )
    conn.commit()


def trial_status(conn):
    """Snapshot for GET /trial-status: active trials, exhausted trials, pending alerts."""
    trials = conn.execute(
        """SELECT agent_id, business_name, owner_email, owner_phone,
                  trial_started_at, trial_ends_at, minutes_total, status
           FROM trial_usage ORDER BY trial_started_at DESC"""
    ).fetchall()
    out = []
    for t in trials:
        (agent_id, business, email, phone, started, ends, minutes, status) = t
        out.append({
            "agent_id": agent_id,
            "business_name": business,
            "owner_email": email,
            "owner_phone": phone,
            "trial_started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
            "trial_ends_at": datetime.fromtimestamp(ends, tz=timezone.utc).isoformat(),
            "minutes_total": round(minutes, 2),
            "minutes_left": round(max(0, TRIAL_MINUTES_CAP - minutes), 2),
            "status": status,
        })
    return {"trials": out, "pending_alerts": [
        {"id": a[0], "agent_id": a[1], "kind": a[2], "minutes_total": a[3],
         "business_name": a[5], "owner_email": a[6], "owner_phone": a[7]}
        for a in pending_alerts(conn)]}


if __name__ == "__main__":
    # Self-test: 30-min warning, 40-min exhaustion, idempotency, 14-day expiry.
    c = sqlite3.connect(":memory:")
    init_trial_tables(c)
    t0 = 1795000000.0
    start_trial(c, "agent_trial_1", "Riverside Plumbing", "owner@riverside.example",
                "+13155550100", now=t0)
    assert record_call(c, "agent_trial_1", "call_a", 12.0, now=t0 + 60) == []
    assert record_call(c, "agent_trial_1", "call_a", 12.0, now=t0 + 60) == []  # duplicate ignored
    assert record_call(c, "agent_trial_1", "call_b", 19.0, now=t0 + 120) == ["trial_30"]
    assert record_call(c, "agent_trial_1", "call_c", 9.5, now=t0 + 180) == ["trial_40"]
    assert record_call(c, "agent_trial_1", "call_d", 5.0, now=t0 + 240) == []  # exhausted: ignored
    start_trial(c, "agent_trial_2", "Summit HVAC", "owner@summit.example", None, now=t0)
    assert record_call(c, "agent_trial_2", "call_x", None, now=t0 + 60) == ["duration_unknown"]
    assert check_expiry(c, now=t0 + 15 * 86400) == ["trial_14d"]
    st = trial_status(c)
    assert len(st["pending_alerts"]) == 4, st["pending_alerts"]
    assert st["trials"][0]["status"] == "exhausted"
    mark_delivered(c, [a["id"] for a in st["pending_alerts"]])
    assert trial_status(c)["pending_alerts"] == []
    print("trial_tracker self-test: all assertions passed")
