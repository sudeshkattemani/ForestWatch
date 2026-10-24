
import os
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

DB = os.environ.get("DB_PATH", "forestwatch.db")
OFFLINE_AFTER = 300  # seconds without heartbeat/alert -> node offline

app = Flask(__name__, static_folder="static")
CORS(app)

lock = threading.Lock()

alerts = []      # newest first
heartbeats = {}  # node -> {time, battery, rssi, lat, lng}

# Stores the latest accepted chainsaw audio trigger.
audio_trigger_state = {
    "id": 0,
    "label": None,
    "confidence": 0,
    "time": None
}


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.strptime(
        s, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT,
                threat TEXT,
                lat REAL,
                lng REAL,
                time TEXT,
                ack INTEGER DEFAULT 0,
                image_url TEXT,
                label TEXT
            )
        """)
        rows = c.execute(
            "SELECT * FROM alerts ORDER BY id DESC"
        ).fetchall()

    alerts.extend(
        {**dict(r), "ack": bool(r["ack"])}
        for r in rows
    )


@app.post("/alert")
def add_alert():
    d = request.get_json(silent=True) or {}

    node = str(
        d.get("node") or d.get("node_id") or "forestwatch-browser"
    )

    label = str(
        d.get("label") or d.get("threat") or "Unknown"
    )

    threat = str(d.get("threat") or label).upper()

    try:
        lat_value = d.get("lat", d.get("latitude"))
        lng_value = d.get("lng", d.get("longitude"))

        lat = (
            float(lat_value)
            if lat_value not in (None, "")
            else None
        )
        lng = (
            float(lng_value)
            if lng_value not in (None, "")
            else None
        )

        if lat is not None and not -90 <= lat <= 90:
            raise ValueError("Latitude must be between -90 and 90")

        if lng is not None and not -180 <= lng <= 180:
            raise ValueError("Longitude must be between -180 and 180")

    except (TypeError, ValueError) as error:
        return jsonify(error=str(error)), 400

    t = iso(now())

    with lock, db() as c:
        cur = c.execute(
            """INSERT INTO alerts
               (node, threat, lat, lng, time, image_url, label)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                node, threat, lat, lng, t,
                d.get("image_url"), label
            )
        )

        alert = dict(
            id=cur.lastrowid,
            node=node,
            threat=threat,
            lat=lat,
            lng=lng,
            time=t,
            ack=False,
            image_url=d.get("image_url"),
            label=label
        )

        alerts.insert(0, alert)

    return jsonify(alert), 201

@app.get("/alerts")
def get_alerts():
    with lock:
        return jsonify(alerts)


@app.post("/alerts/<int:aid>/ack")
def ack(aid):
    with lock, db() as c:
        for a in alerts:
            if a["id"] == aid:
                a["ack"] = True
                c.execute(
                    "UPDATE alerts SET ack=1 WHERE id=?",
                    (aid,)
                )
                return jsonify(a)

    return jsonify(error="not found"), 404


@app.post("/heartbeat")
def heartbeat():
    d = request.get_json(silent=True) or {}

    if "node" not in d:
        return jsonify(error="need node"), 400

    with lock:
        heartbeats[str(d["node"])] = dict(
            time=iso(now()),
            battery=d.get("battery"),
            rssi=d.get("rssi"),
            lat=d.get("lat"),
            lng=d.get("lng")
        )

    return jsonify(ok=True)


@app.get("/nodes")
def nodes():
    out = {}
    t = now()

    with lock:
        for a in reversed(alerts):
            n = out.setdefault(a["node"], {})
            n.update(
                node=a["node"],
                lat=a["lat"],
                lng=a["lng"],
                last_threat=a["threat"],
                last_alert=a["time"],
                last_seen=a["time"]
            )

        for name, h in heartbeats.items():
            n = out.setdefault(
                name,
                {
                    "node": name,
                    "last_threat": None,
                    "last_alert": None
                }
            )

            if h["lat"] is not None and "lat" not in n:
                n.update(lat=h["lat"], lng=h["lng"])

            n.update(
                battery=h["battery"],
                rssi=h["rssi"],
                last_heartbeat=h["time"]
            )

            if h["time"] > n.get("last_seen", ""):
                n["last_seen"] = h["time"]

    for n in out.values():
        n["online"] = (
            "last_seen" in n
            and (t - parse(n["last_seen"])).total_seconds()
            < OFFLINE_AFTER
        )

    return jsonify(list(out.values()))


# Receives a chainsaw-audio detection.
@app.post("/audio-trigger")
def audio_trigger():
    d = request.get_json(silent=True) or {}

    label = str(d.get("label", "")).strip().lower()

    try:
        confidence = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        return jsonify(
            error="confidence must be a number"
        ), 400

    if not 0 <= confidence <= 1:
        return jsonify(
            error="confidence must be between 0 and 1"
        ), 400

    if label != "chainsaw detected":
        return jsonify(
            ok=False,
            message="No chainsaw trigger"
        ), 200

    if confidence < 0.6:
        return jsonify(
            ok=False,
            message="Confidence below threshold"
        ), 200

    with lock:
        audio_trigger_state["id"] += 1
        audio_trigger_state["label"] = "Chainsaw Detected"
        audio_trigger_state["confidence"] = confidence
        audio_trigger_state["time"] = iso(now())

        result = dict(audio_trigger_state)

    return jsonify(
        ok=True,
        trigger="chainsaw_detected",
        message="Chainsaw audio detected; ready for visual detection",
        **result
    ), 200


# Lets the dashboard check whether a new audio trigger arrived.
@app.get("/audio-trigger/status")
def audio_trigger_status():
    with lock:
        result = dict(audio_trigger_state)

    return jsonify(result)


# Existing dashboard homepage.
@app.get("/")
def index():
    return send_from_directory("static", "index.html")


init()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
