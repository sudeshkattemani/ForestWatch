# ForestWatch AI — ESP32 direct-to-website version

This project contains the Flask dashboard and a backend endpoint for an ESP32 audio node. No LoRa gateway is required when the ESP32 can connect to Wi-Fi.

## What is included

- `app.py`: Flask API and dashboard server.
- `static/index.html`: existing dashboard UI.
- `static/forestwatch-model/browser/`: browser/WebAssembly Edge Impulse model assets from the original project. These are currently wired into the dashboard's webcam inference UI; they do **not** prove that an ESP32 audio model is ready.
- `esp32_node_post_example.ino`: Wi-Fi HTTPS upload helper. It does not perform inference; integrate `sendForestWatchAlert(...)` after the deployed audio model returns a prediction.
- `render.yaml`: Render web-service configuration.

## Run locally

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000/`.

## Deploy online using Render

1. Put the contents of this project folder into a GitHub repository.
2. In Render, choose **New → Web Service** and connect that repository. Render reads `render.yaml`.
3. Add an environment variable named `DEVICE_API_KEY` with a long random secret value. Use the same value in the ESP32 sketch. Do not commit a real secret to GitHub.
4. Deploy and copy the HTTPS service URL, such as `https://your-service.onrender.com`.
5. Open `https://your-service.onrender.com/health`. It should return JSON with `ok: true` and `device_api_configured: true`.
6. Put the URL and Wi-Fi credentials in `esp32_node_post_example.ino`, then upload the sketch for a connectivity test. It will not create fake detections; the actual audio model must call the upload helper.

Render start command in `render.yaml`: `gunicorn app:app --workers 1 --threads 4`.

## ESP32 API contract

Send a JSON `POST` to `/device/alert` with header `X-API-Key: YOUR_DEVICE_API_KEY` and `Content-Type: application/json`:

```json
{
  "node": "FORESTWATCH-NODE-01",
  "threat": "CHAINSAW DETECTED",
  "label": "Chainsaw Detected",
  "confidence": 0.94,
  "source": "esp32-audio",
  "lat": 15.123456,
  "lng": 75.123456
}
```

`confidence` can be a fraction from `0` to `1` or a percentage from `0` to `100`. GPS coordinates may both be `null` while the GPS has no fix. The endpoint is `/device/alert`; `/alert` remains available for the existing browser dashboard.

The server also provides `GET /alerts`, `GET /nodes`, `POST /heartbeat`, and `GET /health`.

## Important deployment notes

- Edge Impulse: in your actual audio project, train and test the five sound classes, then use **Deployment → Arduino library** to download the model library. Add that ZIP through Arduino IDE's **Sketch → Include Library → Add .ZIP Library**. Integrate its inference example with the INMP441 I2S capture code before calling the HTTP helper. The included browser `.wasm` assets are not the ESP32 audio library.
- The current dashboard's webcam inference section is still present. The backend now accepts ESP32 audio alerts and confidence/GPS fields, but the browser UI has not been redesigned into an audio-only dashboard.
- Render free instances may sleep, and local SQLite storage on ephemeral hosting may reset after a restart or redeploy. Use managed persistent storage/database for durable production history.
- The example sketch uses `client.setInsecure()` only to simplify a prototype HTTPS test. For production, validate the server certificate.
