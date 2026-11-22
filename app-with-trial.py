import json, os, sqlite3, time
from flask import Flask, request, jsonify

DB = "/data/events.db"
SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = Flask(__name__)

def db():
    os.makedirs("/data", exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at REAL, event_type TEXT, payload TEXT)")
    return c

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
    c.close()
    return jsonify({"ok": True, "id": eid})

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
