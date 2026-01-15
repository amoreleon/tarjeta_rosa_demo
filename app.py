
import os
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from twilio.rest import Client
from dotenv import load_dotenv
import csv

load_dotenv()

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FLOW_SID = os.getenv("TWILIO_STUDIO_FLOW_SID", "")
FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # E.164 (+52...)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret-demo-key")

client = None
if ACCOUNT_SID and AUTH_TOKEN:
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

RESP_FILE = os.path.join(os.path.dirname(__file__), "survey_responses.csv")
if not os.path.exists(RESP_FILE):
    with open(RESP_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp","to","campaign_id","received","rating","comment_url","call_sid","raw"])

def parse_numbers(raw):
    nums = []
    for line in raw.splitlines():
        clean = line.strip()
        if not clean:
            continue
        clean = clean.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not clean.startswith("+"):
            pass
        nums.append(clean)
    return list(dict.fromkeys(nums))

@app.route("/")
def index():
    return render_template("index.html", flow_sid=FLOW_SID, from_number=FROM_NUMBER)

@app.post("/call")
def make_calls():
    if client is None:
        flash("Twilio client not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in your .env", "error")
        return redirect(url_for("index"))
    to_numbers_raw = request.form.get("to_numbers", "")
    campaign_id = request.form.get("campaign_id", "piloto-tarjeta-rosa")
    extra_params_str = request.form.get("extra_params", "{}")
    from_number = request.form.get("from_number") or FROM_NUMBER

    try:
        extra_params = json.loads(extra_params_str) if extra_params_str.strip() else {}
    except Exception as e:
        flash(f"Extra params must be valid JSON: {e}", "error")
        return redirect(url_for("index"))

    if not FLOW_SID or not from_number:
        flash("Missing FLOW SID or FROM number. Check your .env", "error")
        return redirect(url_for("index"))

    to_numbers = parse_numbers(to_numbers_raw)
    if not to_numbers:
        flash("Please provide at least one E.164 phone number (e.g., +52XXXXXXXXXX).", "error")
        return redirect(url_for("index"))

    launched = []
    failures = []
    for to in to_numbers:
        try:
            params = {"campaignId": campaign_id, **extra_params}
            execution = client.studio.v2.flows(FLOW_SID).executions.create(
                to=to,
                from_=from_number,
                parameters=params
            )
            launched.append((to, execution.sid))
        except Exception as e:
            failures.append((to, str(e)))

    if launched:
        flash(f"Launched {len(launched)} call(s). First exec SID: {launched[0][1]}", "success")
    if failures:
        flash("Some calls failed: " + "; ".join([f"{n} -> {err}" for n, err in failures]), "error")

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
