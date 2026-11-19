"""Retell webhook receiver — DEPLOY CANDIDATE with trial enforcement.

This is a COPY of the live receiver (~/workspace/system/retell-webhook/app.py)
plus the trial tracker integration. It is NOT live. Deploy it by replacing
app.py on the Railway retell-webhook service (project poofect-sg-webhooks,
service retell-webhook) and redeploying — see TRIAL-ENFORCEMENT.md.
The live app.py is untouched.

What changed vs live app.py (all additions marked TRIAL):
- imports trial_tracker (same directory; copy both files to the service)
- db() also creates the trial tables
- POST /retell-webhook: call_analyzed events for trial agents accumulate
  minutes via trial_tracker.record_call
- GET  /trial-status?secret=  — trials + pending alerts (read by the
  extended retell-lead-poll cron)
- GET  /trial-check?secret=   — runs the 14-day expiry sweep
- POST /trial-start   {secret, agent_id, business_name, owner_email, owner_phone}
- POST /trial-convert {secret, agent_id}
- POST /trial-delivered {secret, ids:[...]} — mark alerts delivered

Secret: read from the WEBHOOK_SECRET env var only, never hardcoded.
"""
import json, os, sqlite3, time
from flask import Flask, request, jsonify

import trial_tracker  # TRIAL: same directory as this file

DB = "/data/events.db"
SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = Flask(__name__)


def db():
    os.makedirs("/data", exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at REAL, event_type TEXT, payload TEXT)")
    trial_tracker.init_trial_tables(c)  # TRIAL
    return c


def _agent_and_call_id(payload):
    """Best-effort agent_id / call_id out of a Retell webhook payload."""
    call = payload.get("call") or payload.get("data", {}).get("call") or {}
    agent_id = call.get("agent_id") or payload.get("agent_id")
    call_id = call.get("call_id") or payload.get("call_id")
    return agent_id, call_id


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/retell-webhook")
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    etype = str(data.get("event") or data.get("type") or request.headers.get("X-SG-Event", "unknown"))[:80]
    c = db()
    c.execute("INSERT INTO events (received_at, event_type, payload) VALUES (?,?,?)",
              (time.time(), etype, json.dumps(data)))
    c.commit()
    eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    # TRIAL: accumulate trial minutes from call_analyzed events.
    # record_call is idempotent on call_id and no-ops for non-trial agents.
    alerts = []
    if etype == "call_analyzed":
        agent_id, call_id = _agent_and_call_id(data)
        if agent_id:
            minutes = trial_tracker.extract_duration_minutes(data)
            alerts = trial_tracker.record_call(c, agent_id, call_id, minutes)
    c.close()
    return jsonify({"ok": True, "id": eid, "trial_alerts": alerts})


@app.get("/events")
def events():
    if not SECRET or request.args.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    c = db()
    rows = c.execute("SELECT id, received_at, event_type, payload FROM events WHERE id > ? ORDER BY id LIMIT 200",
                     (since,)).fetchall()
    c.close()
    return jsonify({"events": [
        {"id": r[0], "received_at": r[1], "event_type": r[2], "payload": json.loads(r[3])}
        for r in rows]})


# ---- TRIAL endpoints (all secret-gated, same secret as /events) ----

@app.get("/trial-status")
def trial_status():
    if not SECRET or request.args.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    c = db()
    out = trial_tracker.trial_status(c)
    c.close()
    return jsonify(out)


@app.get("/trial-check")
def trial_check():
    """Run the 14-day expiry sweep. The poll cron hits this each run so
    date-based expiry fires even when a trial has no recent calls."""
    if not SECRET or request.args.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    c = db()
    alerts = trial_tracker.check_expiry(c)
    c.close()
    return jsonify({"ok": True, "alerts": alerts})


@app.post("/trial-start")
def trial_start():
    body = request.get_json(force=True, silent=True) or {}
    if not SECRET or body.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    agent_id = body.get("agent_id")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    c = db()
    trial_tracker.start_trial(
        c, agent_id, body.get("business_name"),
        body.get("owner_email"), body.get("owner_phone"),
    )
    c.close()
    return jsonify({"ok": True, "agent_id": agent_id,
                    "trial": "14 days free, up to 40 minutes"})


@app.post("/trial-convert")
def trial_convert():
    body = request.get_json(force=True, silent=True) or {}
    if not SECRET or body.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    agent_id = body.get("agent_id")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    c = db()
    trial_tracker.convert_trial(c, agent_id)
    c.close()
    return jsonify({"ok": True, "agent_id": agent_id, "status": "converted"})


@app.post("/trial-delivered")
def trial_delivered():
    body = request.get_json(force=True, silent=True) or {}
    if not SECRET or body.get("secret") != SECRET:
        return jsonify({"error": "unauthorized"}), 401
    ids = body.get("ids") or []
    c = db()
    trial_tracker.mark_delivered(c, [int(i) for i in ids])
    c.close()
    return jsonify({"ok": True, "marked": len(ids)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
