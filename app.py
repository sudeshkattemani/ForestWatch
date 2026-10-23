import os
import sqlite3
import threading
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "forestwatch.db"))
OFFLINE_AFTER = 300  # seconds without heartbeat/alert -> node offline
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")

app = Flask(__name__, static_folder="static")
CORS(app)
lock = threading.Lock()
alerts = []      # newest first
heartbeats = {}  # node -> {time, battery, rssi, lat, lng}


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT, threat TEXT,
            lat REAL, lng REAL, time TEXT, ack INTEGER DEFAULT 0,
            image_url TEXT, label TEXT, confidence REAL, source TEXT)""")
        # Migrate an existing copy of the original project without deleting its alerts.
        columns = {row[1] for row in c.execute("PRAGMA table_info(alerts)").fetchall()}
        if "confidence" not in columns:
            c.execute("ALTER TABLE alerts ADD COLUMN confidence REAL")
        if "source" not in columns:
            c.execute("ALTER TABLE alerts ADD COLUMN source TEXT")
        rows = c.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
    alerts.clear()
    alerts.extend(alert_from_row(r) for r in rows)


def alert_from_row(row):
    item = dict(row)
    item["ack"] = bool(item.get("ack"))
    # Return both naming styles so the original dashboard and ESP32 clients agree.
    item["latitude"] = item.get("lat")
    item["longitude"] = item.get("lng")
    item["node_id"] = item.get("node")
    item["timestamp"] = item.get("time")
    return item


def parse_alert_payload(d):
    node = str(d.get("node", d.get("node_id", "ESP32-NODE")))
    raw_threat = d.get("threat") or d.get("label") or d.get("message") or d.get("type")
    if not raw_threat:
        raise ValueError("need threat or label")
    threat = str(raw_threat).strip().upper()
    raw_lat = d.get("lat", d.get("latitude"))
    raw_lng = d.get("lng", d.get("longitude"))
    lat = None if raw_lat in (None, "") else float(raw_lat)
    lng = None if raw_lng in (None, "") else float(raw_lng)
    if (lat is None) != (lng is None):
        raise ValueError("send both latitude and longitude, or neither")
    if lat is not None and not (-90 <= lat <= 90):
        raise ValueError("latitude must be between -90 and 90")
    if lng is not None and not (-180 <= lng <= 180):
        raise ValueError("longitude must be between -180 and 180")
    confidence = d.get("confidence")
    if confidence not in (None, ""):
        confidence = float(confidence)
        if confidence > 1 and confidence <= 100:
            confidence /= 100.0
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1, or 0 and 100 percent")
    else:
        confidence = None
    return {
        "node": node,
        "threat": threat,
        "lat": lat,
        "lng": lng,
        "confidence": confidence,
        "source": str(d.get("source", "esp32-audio")),
        "image_url": d.get("image_url"),
        "label": str(d.get("label") or raw_threat),
    }


def store_alert(d):
    try:
        payload = parse_alert_payload(d)
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    t = iso(now())
    with lock, db() as c:
        cur = c.execute(
            """INSERT INTO alerts(node,threat,lat,lng,time,image_url,label,confidence,source)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (payload["node"], payload["threat"], payload["lat"], payload["lng"], t,
             payload["image_url"], payload["label"], payload["confidence"], payload["source"]),
        )
        item = dict(id=cur.lastrowid, **payload, time=t, ack=False)
        item.update(latitude=item["lat"], longitude=item["lng"], node_id=item["node"], timestamp=t)
        alerts.insert(0, item)
        del alerts[200:]
    return jsonify(item), 201


def require_device_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not DEVICE_API_KEY:
            return jsonify(error="Server is missing DEVICE_API_KEY configuration"), 503
        supplied = request.headers.get("X-API-Key", "")
        if supplied != DEVICE_API_KEY:
            return jsonify(error="unauthorized device"), 401
        return fn(*args, **kwargs)
    return wrapper


@app.post("/alert")
def add_alert():
    # Existing browser dashboard endpoint; retained for backward compatibility.
    return store_alert(request.get_json(silent=True) or {})


@app.post("/device/alert")
@require_device_key
def device_alert():
    # ESP32 endpoint: configure DEVICE_API_KEY in the hosting provider's environment.
    return store_alert(request.get_json(silent=True) or {})


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
                c.execute("UPDATE alerts SET ack=1 WHERE id=?", (aid,))
                return jsonify(a)
    return jsonify(error="not found"), 404


@app.post("/heartbeat")
def heartbeat():
    d = request.get_json(silent=True) or {}
    node = d.get("node", d.get("node_id"))
    if not node:
        return jsonify(error="need node or node_id"), 400
    try:
        raw_lat = d.get("lat", d.get("latitude"))
        raw_lng = d.get("lng", d.get("longitude"))
        lat = None if raw_lat in (None, "") else float(raw_lat)
        lng = None if raw_lng in (None, "") else float(raw_lng)
    except (TypeError, ValueError):
        return jsonify(error="invalid GPS coordinates"), 400
    t = iso(now())
    with lock:
        heartbeats[str(node)] = dict(time=t, battery=d.get("battery"), rssi=d.get("rssi"), lat=lat, lng=lng)
    return jsonify(ok=True, node=str(node), time=t)


@app.get("/nodes")
def nodes():
    out, t = {}, now()
    with lock:
        for a in reversed(alerts):
            n = out.setdefault(a["node"], {})
            n.update(node=a["node"], node_id=a["node"], lat=a["lat"], lng=a["lng"],
                     latitude=a["lat"], longitude=a["lng"], last_threat=a["threat"],
                     last_alert=a["time"], last_seen=a["time"])
        for name, h in heartbeats.items():
            n = out.setdefault(name, {"node": name, "node_id": name, "last_threat": None, "last_alert": None})
            if h["lat"] is not None:
                n.update(lat=h["lat"], lng=h["lng"], latitude=h["lat"], longitude=h["lng"])
            n.update(battery=h["battery"], rssi=h["rssi"], last_heartbeat=h["time"])
            if h["time"] > n.get("last_seen", ""):
                n["last_seen"] = h["time"]
    for n in out.values():
        last_seen = n.get("last_seen")
        n["online"] = bool(last_seen) and (t - parse(last_seen)).total_seconds() < OFFLINE_AFTER
        n["status"] = "online" if n["online"] else "offline"
    return jsonify(list(out.values()))


@app.get("/health")
def health():
    return jsonify(ok=True, service="ForestWatch AI", device_api_configured=bool(DEVICE_API_KEY))


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


init()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
