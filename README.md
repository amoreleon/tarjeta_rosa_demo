
# Demo Outbound - Tarjeta Rosa (Twilio Studio)

Pequeña app Flask para lanzar ejecuciones de un **Twilio Studio Flow** (encuesta IVR) y capturar respuestas vía webhook.

## 1) Requisitos
- Python 3.10+
- Cuenta Twilio con: número de voz, **Flow SID** de Studio (trigger REST API)

## 2) Setup
```bash
cd tarjeta_rosa_twilio_demo
python -m venv .venv && source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
cp .env.example .env  # y llena las variables
python app.py
# abre http://localhost:5000
```

## 3) Studio Flow (resumen)
- Trigger: **REST API**
- Widgets:
  - **Say/Play**: saludo y aviso de privacidad.
  - **Gather Input on Call** (`recibio`): “¿Recibiste la Tarjeta Rosa? 1=Sí, 2=No” (DTMF, maxDigits=1, timeout=5s).
  - **Split Based On...** por `widgets.recibio.Digits`.
  - Si `1`: **Gather Input on Call** (`rating`): “Del 1 al 5, ¿qué tan útil te ha sido la tarjeta?”
  - **Record Voicemail** (`comentario`): “Opcional: deja un comentario”
  - **Make HTTP Request** a `POST https://TU_HOST/studio-webhook` con JSON:
    ```json
    {
      "to": "{{contact.channel.address}}",
      "campaignId": "{{trigger.parameters.campaignId}}",
      "received": "{{widgets.recibio.Digits}}",
      "rating": "{{widgets.rating.Digits}}",
      "commentUrl": "{{widgets.comentario.RecordingUrl}}",
      "callSid": "{{trigger.call.sid}}"
    }
    ```

## 4) Prueba rápida por cURL (corrige SIDs/números)
```bash
curl -X POST "https://studio.twilio.com/v2/Flows/FWxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/Executions" \
  --data-urlencode "To=+52XXXXXXXXXX" \
  --data-urlencode "From=+1XXXXXXXXXX" \
  --data-urlencode 'Parameters={\"campaignId\":\"piloto-001\",\"survey\":\"tarjeta_rosa\"}' \
  -u $TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN
```

> Nota: `Parameters` debe ser **JSON válido** escapado si usas cURL.

## 5) Respuestas
Studio enviará un POST al webhook y se irán agregando en `survey_responses.csv`. Puedes descargarlo desde la UI.

## 6) Producción
- Hospédalo en Render, Railway, Fly.io o cualquier VPS.
- Usa HTTPS público (si es local, usa ngrok).
- Asegura el webhook con firma/Token o la autenticación del widget.
