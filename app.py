import os
import re
import json
import time
import io
import csv
from datetime import datetime, timezone

from flask import Flask, request, render_template, redirect, url_for, flash, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from twilio.rest import Client

from flask_sqlalchemy import SQLAlchemy

load_dotenv()

# =========================
# Environment / Config
# =========================
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
DEFAULT_FLOW_SID = os.getenv("TWILIO_STUDIO_FLOW_SID", "").strip()
DEFAULT_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()

DEMO_PASSCODE = (os.getenv("DEMO_PASSCODE", "") or "").strip()
FRONTEND_ORIGIN = (os.getenv("FRONTEND_ORIGIN", "") or "").strip()

# Optional secret for Studio webhook (recommended once you get Studio access)
STUDIO_WEBHOOK_SECRET = (os.getenv("STUDIO_WEBHOOK_SECRET", "") or "").strip()

MAX_CALLS_PER_REQUEST = int(os.getenv("MAX_CALLS_PER_REQUEST", "1000"))
CALLS_PER_SECOND = float(os.getenv("CALLS_PER_SECOND", "1"))

# Render gives DATABASE_URL for Postgres if you configure it
DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()

# SQLAlchemy expects "postgresql://", Render sometimes provides "postgres://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Fallback local DB if DATABASE_URL not set (useful for local dev)
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///local.db"

# =========================
# App init
# =========================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-me")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# CORS for browser clients (Bolt/Lovable)
cors_origins = [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else "*"
CORS(app, resources={
    r"/call": {"origins": cors_origins},
    r"/auth-check": {"origins": cors_origins},
    r"/api/*": {"origins": cors_origins},
    r"/studio-webhook": {"origins": "*"},  # Studio comes from Twilio; keep open but protect via secret below
}, methods=["GET", "POST", "OPTIONS"])

twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN) if (ACCOUNT_SID and AUTH_TOKEN) else None


# =========================
# DB Models
# =========================
def utcnow():
    return datetime.now(timezone.utc)

class Launch(db.Model):
    __tablename__ = "launches"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    campaign_id = db.Column(db.String(128), index=True, nullable=False)
    flow_sid = db.Column(db.String(64), index=True, nullable=True)
    execution_sid = db.Column(db.String(64), index=True, nullable=True)
    to_number = db.Column(db.String(32), index=True, nullable=False)
    from_number = db.Column(db.String(32), nullable=True)

class Result(db.Model):
    __tablename__ = "results"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    campaign_id = db.Column(db.String(128), index=True, nullable=False)
    flow_sid = db.Column(db.String(64), index=True, nullable=True)
    execution_sid = db.Column(db.String(64), index=True, nullable=True)
    call_sid = db.Column(db.String(64), index=True, nullable=True)
    to_number = db.Column(db.String(32), index=True, nullable=True)
    answers_json = db.Column(db.Text, nullable=False, default="{}")  # flexible q1..qN
    raw_json = db.Column(db.Text, nullable=False, default="{}")      # full payload


with app.app_context():
    db.create_all()


# =========================
# Helpers
# =========================
def require_passcode(payload: dict):
    """
    If DEMO_PASSCODE is set, require:
      - payload.passcode  OR
      - header X-CC-Passcode
    """
    if not DEMO_PASSCODE:
        return None
    provided = (payload.get("passcode") or "").strip()
    if not provided:
        provided = (request.headers.get("X-CC-Passcode", "") or "").strip()
    if provided != DEMO_PASSCODE:
        return {"ok": False, "error": "Passcode inválido"}, 401
    return None

def normalize_mx_number(value: str):
    """
    Accept Mexico-only inputs and normalize to E.164:
      - 10 digits: 5512345678 -> +525512345678
      - 12 digits starting with 52: 525512345678 -> +525512345678
      - +52... also ok (we strip to digits then normalize)
    """
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return None

    if len(digits) == 10:
        digits = "52" + digits

    if len(digits) == 12 and digits.startswith("52"):
        return "+" + digits

    return None

def parse_numbers(raw):
    """
    raw may be:
      - string with lines
      - list of strings
    Returns (valid_e164_list, invalid_list)
    """
    if raw is None:
        return [], []

    if isinstance(raw, list):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw).splitlines()

    valid, invalid, seen = [], [], set()
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        norm = normalize_mx_number(line)
        if not norm:
            invalid.append(line)
            continue
        if norm not in seen:
            seen.add(norm)
            valid.append(norm)

    return valid, invalid

def safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return "{}"

def read_answers(payload: dict):
    """
    Flexible answers:
      - preferred: payload["answers"] (dict)
      - fallback: any keys like q1,q2,q3...
    """
    answers = payload.get("answers")
    if isinstance(answers, dict):
        return answers

    # fallback: q1..qN
    out = {}
    for k, v in payload.items():
        if isinstance(k, str) and re.fullmatch(r"q\d+", k.strip().lower()):
            out[k.strip().lower()] = str(v)
    return out

def _twilio_prop(obj, *keys):
    """Lee propiedades aunque cambien nombres entre versiones del SDK."""
    for k in keys:
        # 1) intentar como atributo directo
        try:
            v = getattr(obj, k)
            if v is not None:
                return v
        except Exception:
            pass

        # 2) intentar desde _properties (raw payload)
        try:
            props = getattr(obj, "_properties", None) or {}
            v = props.get(k)
            if v is not None:
                return v
        except Exception:
            pass

    return None

def _iso(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


# =========================
# Routes
# =========================
@app.get("/")
def index():
    # keep your old HTML UI if you want
    return render_template("index.html", flow_sid=DEFAULT_FLOW_SID, from_number=DEFAULT_FROM_NUMBER)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth-check")
def auth_check():
    payload = request.get_json(silent=True) or {}
    fail = require_passcode(payload)
    if fail:
        return fail
    return {"ok": True}

@app.post("/call")
def call():
    if not twilio_client:
        return {"ok": False, "error": "Twilio no configurado (credenciales faltantes)."}, 500

    payload = request.get_json(silent=True) or request.form.to_dict(flat=True) or {}
    fail = require_passcode(payload)
    if fail:
        return fail

    # Allow caller to specify flowSid (scenario C)
    flow_sid = (payload.get("flow_sid") or payload.get("flowSid") or DEFAULT_FLOW_SID or "").strip()
    from_number = (payload.get("from_number") or payload.get("fromNumber") or DEFAULT_FROM_NUMBER or "").strip()

    if not flow_sid:
        return {"ok": False, "error": "Falta flow_sid (TWILIO_STUDIO_FLOW_SID) o no se envió flow_sid."}, 400
    if not from_number:
        return {"ok": False, "error": "Falta from_number (TWILIO_FROM_NUMBER)."}, 400

    campaign_id = (payload.get("campaign_id") or payload.get("campaignId") or "contact-center-v1").strip()

    extra_params = payload.get("extra_params") or payload.get("extraParams") or {}
    if isinstance(extra_params, str):
        try:
            extra_params = json.loads(extra_params) if extra_params.strip() else {}
        except Exception as e:
            return {"ok": False, "error": f"extra_params debe ser JSON válido: {e}"}, 400
    if not isinstance(extra_params, dict):
        extra_params = {}

    to_numbers_raw = payload.get("to_numbers") or payload.get("toNumbers") or ""
    to_numbers, invalid = parse_numbers(to_numbers_raw)

    if not to_numbers:
        return {
            "ok": False,
            "error": "No hay números válidos. Usa teléfonos MX de 10 dígitos (ej. 5512345678).",
            "invalid": invalid
        }, 400

    if MAX_CALLS_PER_REQUEST > 0 and len(to_numbers) > MAX_CALLS_PER_REQUEST:
        to_numbers = to_numbers[:MAX_CALLS_PER_REQUEST]

    sleep_between = (1.0 / CALLS_PER_SECOND) if (CALLS_PER_SECOND and CALLS_PER_SECOND > 0) else 0

    launched, failures = [], []

    for to in to_numbers:
        try:
            params = {"campaignId": campaign_id, **extra_params}
            execution = twilio_client.studio.v2.flows(flow_sid).executions.create(
                to=to,
                from_=from_number,
                parameters=params
            )

            db_item = Launch(
                campaign_id=campaign_id,
                flow_sid=flow_sid,
                execution_sid=execution.sid,
                to_number=to,
                from_number=from_number
            )
            db.session.add(db_item)
            db.session.commit()

            launched.append({
                "to": to,
                "execution_sid": execution.sid
            })

        except Exception as e:
            failures.append({"to": to, "error": str(e)})

        if sleep_between:
            time.sleep(sleep_between)

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "flow_sid": flow_sid,
        "launched_count": len(launched),
        "failed_count": len(failures),
        "invalid_count": len(invalid),
        "invalid_preview": invalid[:50],
        "launched_preview": launched[:50],
        "failures_preview": failures[:50],
    }

@app.post("/studio-webhook")
def studio_webhook():
    """
    Twilio Studio should POST survey results here.

    Recommended security:
      - set STUDIO_WEBHOOK_SECRET in Render
      - configure Studio Make HTTP Request to send header:
          X-Webhook-Secret: <same value>
    """
    if STUDIO_WEBHOOK_SECRET:
        provided = (request.headers.get("X-Webhook-Secret", "") or "").strip()
        if provided != STUDIO_WEBHOOK_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 401

    payload = request.get_json(silent=True) or request.form.to_dict(flat=True) or {}

    campaign_id = (payload.get("campaignId") or payload.get("campaign_id") or "unknown").strip()
    flow_sid = (payload.get("flowSid") or payload.get("flow_sid") or "").strip()
    execution_sid = (payload.get("executionSid") or payload.get("execution_sid") or "").strip()
    call_sid = (payload.get("callSid") or payload.get("CallSid") or "").strip()
    to_number = (payload.get("to") or payload.get("To") or payload.get("contact") or "").strip()

    answers = read_answers(payload)

    row = Result(
        campaign_id=campaign_id,
        flow_sid=flow_sid or None,
        execution_sid=execution_sid or None,
        call_sid=call_sid or None,
        to_number=to_number or None,
        answers_json=safe_json(answers),
        raw_json=safe_json(payload),
    )
    db.session.add(row)
    db.session.commit()

    return {"ok": True}

@app.get("/api/results")
def api_results():
    # protect results behind passcode (called from your UI)
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    campaign_id = (request.args.get("campaign_id") or request.args.get("campaignId") or "").strip()
    flow_sid = (request.args.get("flow_sid") or request.args.get("flowSid") or "").strip()
    limit = int(request.args.get("limit", "200"))

    q = Result.query.order_by(Result.created_at.desc())
    if campaign_id:
        q = q.filter(Result.campaign_id == campaign_id)
    if flow_sid:
        q = q.filter(Result.flow_sid == flow_sid)

    rows = q.limit(min(limit, 1000)).all()

    out = []
    for r in rows:
        try:
            answers = json.loads(r.answers_json or "{}")
        except Exception:
            answers = {}
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "campaign_id": r.campaign_id,
            "flow_sid": r.flow_sid,
            "execution_sid": r.execution_sid,
            "call_sid": r.call_sid,
            "to": r.to_number,
            "answers": answers,
        })

    return {"ok": True, "count": len(out), "results": out}

@app.get("/api/summary")
def api_summary():
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    campaign_id = (request.args.get("campaign_id") or request.args.get("campaignId") or "").strip()
    flow_sid = (request.args.get("flow_sid") or request.args.get("flowSid") or "").strip()

    q = Result.query
    if campaign_id:
        q = q.filter(Result.campaign_id == campaign_id)
    if flow_sid:
        q = q.filter(Result.flow_sid == flow_sid)

    rows = q.all()

    summary = {}  # {q1: { "1": 10, "2": 5 } }
    total = 0

    for r in rows:
        total += 1
        try:
            answers = json.loads(r.answers_json or "{}")
        except Exception:
            answers = {}
        if not isinstance(answers, dict):
            continue

        for k, v in answers.items():
            k = str(k).lower()
            v = str(v)
            summary.setdefault(k, {})
            summary[k][v] = summary[k].get(v, 0) + 1

    return {"ok": True, "total_results": total, "summary": summary}

@app.get("/api/launches")
def api_launches():
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    campaign_id = (request.args.get("campaign_id") or request.args.get("campaignId") or "").strip()
    flow_sid = (request.args.get("flow_sid") or request.args.get("flowSid") or "").strip()
    limit = int(request.args.get("limit", "200"))

    q = Launch.query.order_by(Launch.created_at.desc())
    if campaign_id:
        q = q.filter(Launch.campaign_id == campaign_id)
    if flow_sid:
        q = q.filter(Launch.flow_sid == flow_sid)

    rows = q.limit(min(limit, 2000)).all()

    out = [{
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "campaign_id": r.campaign_id,
        "flow_sid": r.flow_sid,
        "execution_sid": r.execution_sid,
        "to": r.to_number,
        "from": r.from_number,
    } for r in rows]

    return {"ok": True, "count": len(out), "launches": out}

@app.get("/api/twilio-calls")
def api_twilio_calls():
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    if not twilio_client:
        return {"ok": False, "error": "Twilio no configurado."}, 500

    limit = int(request.args.get("limit", "50"))
    # Default elegante: solo outbound-api
    direction_filter = (request.args.get("direction", "outbound-api") or "").strip()
    include_inbound = (request.args.get("include_inbound", "false") or "").lower() == "true"

    def pick_attr(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if v:
                return v
        return None

    try:
        calls = twilio_client.calls.list(limit=min(limit, 200))
        items = []

        for c in calls:
            direction = pick_attr(c, "direction") or ""

            # filtro elegante
            if not include_inbound and direction_filter:
                if direction != direction_filter:
                    continue

            from_val = (
                pick_attr(c, "from_", "from", "from_formatted", "caller", "caller_formatted")
                or (DEFAULT_FROM_NUMBER if direction == "outbound-api" else None)
            )
            to_val = pick_attr(c, "to", "to_formatted")

            start_time = pick_attr(c, "start_time")
            end_time = pick_attr(c, "end_time")

            items.append({
                "sid": c.sid,
                "from": from_val,
                "to": to_val,
                "status": pick_attr(c, "status"),
                "start_time": start_time.isoformat() if start_time else None,
                "end_time": end_time.isoformat() if end_time else None,
                "duration": pick_attr(c, "duration"),
                "direction": direction,
            })

        return {"ok": True, "count": len(items), "calls": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.get("/download-results.csv")
def download_results_csv():
    # protect behind passcode
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    campaign_id = (request.args.get("campaign_id") or "").strip()
    q = Result.query.order_by(Result.created_at.desc())
    if campaign_id:
        q = q.filter(Result.campaign_id == campaign_id)

    rows = q.limit(5000).all()

    # Build CSV dynamically with union of question keys
    all_keys = set()
    parsed = []
    for r in rows:
        try:
            answers = json.loads(r.answers_json or "{}")
        except Exception:
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        all_keys.update(answers.keys())
        parsed.append((r, answers))

    qkeys = sorted([str(k) for k in all_keys])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "campaign_id", "flow_sid", "execution_sid", "call_sid", "to"] + qkeys)

    for r, answers in parsed:
        row = [
            r.created_at.isoformat() if r.created_at else "",
            r.campaign_id,
            r.flow_sid or "",
            r.execution_sid or "",
            r.call_sid or "",
            r.to_number or "",
        ] + [answers.get(k, "") for k in qkeys]
        writer.writerow(row)

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="results.csv", mimetype="text/csv")

@app.get("/download-launches.csv")
def download_launches_csv():
    fail = require_passcode(request.args.to_dict(flat=True))
    if fail:
        return fail

    campaign_id = (request.args.get("campaign_id") or "").strip()
    q = Launch.query.order_by(Launch.created_at.desc())
    if campaign_id:
        q = q.filter(Launch.campaign_id == campaign_id)

    rows = q.limit(10000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "campaign_id", "flow_sid", "execution_sid", "from", "to"])
    for r in rows:
        writer.writerow([
            r.created_at.isoformat() if r.created_at else "",
            r.campaign_id,
            r.flow_sid or "",
            r.execution_sid or "",
            r.from_number or "",
            r.to_number or "",
        ])

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="launches.csv", mimetype="text/csv")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)


