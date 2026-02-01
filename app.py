import os
import json
import re
import time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from flask_cors import CORS
from twilio.rest import Client
from dotenv import load_dotenv
import csv

load_dotenv()

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FLOW_SID = os.getenv("TWILIO_STUDIO_FLOW_SID", "")
FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # E.164 (+52...)
DEMO_PASSCODE = os.getenv("DEMO_PASSCODE", "").strip()

# CORS: permite que la UI (Bolt/Lovable) llame a este backend
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "").strip()

# Controles operativos (para evitar accidentes)
MAX_CALLS_PER_REQUEST = int(os.getenv("MAX_CALLS_PER_REQUEST", "1000"))  # ajustable
CALLS_PER_SECOND = float(os.getenv("CALLS_PER_SECOND", "1"))  # ritmo de arranque (honesto y controlado)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret-demo-key")

CORS(app, resources={
    r"/call": {"origins": [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else "*"},
    r"/studio-webhook": {"origins": [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else "*"},
}, methods=["POST", "OPTIONS"])

client = None
if ACCOUNT_SID and AUTH_TOKEN:
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

RESP_FILE = os.path.join(os.path.dirname(__file__), "survey_responses.csv")
if not os.path.exists(RESP_FILE):
    with open(RESP_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "to", "campaign_id", "received", "rating", "comment_url", "call_sid", "raw"])

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

def parse_numbers(raw):
    """
    Acepta string con líneas o lista.
    Regresa (valid_numbers, invalid_numbers)
    """
    if raw is None:
        return [], []
    if isinstance(raw, list):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw).splitlines()

    valid = []
    invalid = []
    seen = set()

    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        clean = clean.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

        if not E164_RE.match(clean):
            invalid.append(clean)
            continue

        if clean not in seen:
            seen.add(clean)
            valid.append(clean)

    return valid, invalid

def require_passcode(payload):
    """
    Si DEMO_PASSCODE está configurado, exige que venga en payload.passcode o header X-CC-Passcode
    """
    if not DEMO_PASSCODE:
        return None  # no se exige
    provided = ""
    if isinstance(payload, dict):
        provided = (payload.get("passcode") or "").strip()
    if not provided:
        provided = (request.headers.get("X-CC-Passcode", "") or "").strip()
    if provided != DEMO_PASSCODE:
        return {"ok": False, "error": "Passcode inválido"}, 401
    return None

@app.route("/")
def index():
    return render_template("index.html", flow_sid=FLOW_SID, from_number=FROM_NUMBER)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/call")
def make_calls():
    if client is None:
        # Si viene de UI externa, respondemos JSON
        if request.is_json:
            return {"ok": False, "error": "Twilio no configurado (faltan credenciales)."}, 500
        flash("Twilio client not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.", "error")
        return redirect(url_for("index"))

    # Acepta JSON o form
    payload = request.get_json(silent=True) or request.form.to_dict(flat=True)

    # Seguridad: passcode (si está configurado)
    pass_fail = require_passcode(payload)
    if pass_fail:
        return pass_fail

    to_numbers_raw = payload.get("to_numbers", "")
    campaign_id = payload.get("campaign_id", "contact-center-v1")
    extra_params = payload.get("extra_params", {})
    from_number = payload.get("from_number") or FROM_NUMBER

    # extra_params puede venir como string JSON desde form
    if isinstance(extra_params, str):
        try:
            extra_params = json.loads(extra_params) if extra_params.strip() else {}
        except Exception as e:
            if request.is_json:
                return {"ok": False, "error": f"extra_params debe ser JSON válido: {e}"}, 400
            flash(f"Extra params must be valid JSON: {e}", "error")
            return redirect(url_for("index"))

    if not FLOW_SID or not from_number:
        if request.is_json:
            return {"ok": False, "error": "Falta FLOW_SID o FROM_NUMBER."}, 500
        flash("Missing FLOW SID or FROM number. Check your .env", "error")
        return redirect(url_for("index"))

    to_numbers, invalid = parse_numbers(to_numbers_raw)
    if not to_numbers:
        msg = "No hay números válidos. Usa formato E.164, ej. +521234567890"
        if request.is_json:
            return {"ok": False, "error": msg, "invalid": invalid}, 400
        flash(msg, "error")
        return redirect(url_for("index"))

    # Control por seguridad operativa (ajustable)
    if MAX_CALLS_PER_REQUEST > 0 and len(to_numbers) > MAX_CALLS_PER_REQUEST:
        to_numbers = to_numbers[:MAX_CALLS_PER_REQUEST]

    launched = []
    failures = []

    # Ritmo de arranque (CPS): evita saturar API / carriers
    sleep_between = 0
    if CALLS_PER_SECOND and CALLS_PER_SECOND > 0:
        sleep_between = 1.0 / CALLS_PER_SECOND

    for to in to_numbers:
        try:
            params = {"campaignId": campaign_id, **(extra_params or {})}
            execution = client.studio.v2.flows(FLOW_SID).executions.create(
                to=to,
                from_=from_number,
                parameters=params
            )
            launched.append({"to": to, "execution_sid": execution.sid})
        except Exception as e:
            failures.append({"to": to, "error": str(e)})

        if sleep_between:
            time.sleep(sleep_between)

    # Respuesta JSON (para Bolt/Lovable)
    if request.is_json:
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "launched_count": len(launched),
            "failed_count": len(failures),
            "invalid_count": len(invalid),
            "invalid": invalid[:50],
            "launched": launched[:50],
            "failures": failures[:50]
        }

    # Respuesta para la UI vieja (Flask HTML)
    if launched:
        flash(f"Launched {len(launched)} call(s).", "success")
    if invalid:
        flash(f"Se ignoraron {len(invalid)} números inválidos.", "error")
    if failures:
        flash("Some calls failed: " + "; ".join([f"{x['to']} -> {x['error']}" for x in failures[:10]]), "error")

    return redirect(url_for("index"))

@app.post("/studio-webhook")
def studio_webhook():
    payload = request.get_json(silent=True) or request.form.to_dict(flat=True)

    to = payload.get("to") or payload.get("contact") or ""
    campaign_id = payload.get("campaignId") or payload.get("campaign_id") or ""
    received = payload.get("received") or payload.get("recibio") or payload.get("q1") or ""
    rating = payload.get("rating") or payload.get("q2") or ""
    comment_url = payload.get("commentUrl") or payload.get("comentario_url") or ""
    call_sid = payload.get("callSid") or payload.get("CallSid") or ""
    raw = json.dumps(payload, ensure_ascii=False)

    with open(RESP_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), to, campaign_id, received, rating, comment_url, call_sid, raw])

    return {"status": "ok"}

@app.get("/download-responses")
def download_responses():
    return send_file(RESP_FILE, as_attachment=True, download_name="survey_responses.csv")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
