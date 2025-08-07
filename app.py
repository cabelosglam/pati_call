
from flask import Flask, request, render_template, redirect, url_for, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse


app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"]
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

        client = Client(account_sid, auth_token)

        call = client.calls.create(
            to=telefone,
            from_="+17752615122",
            url="https://pati-call.onrender.com/voice"
        )
        status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
    return render_template("index.html", status=status)

@app.route("/voice", methods=["GET", "POST"])
def voice():
    resp = VoiceResponse()
    resp.say("Olá, aqui é a Pat Glam da Glam Hair Brand! Estou ligando para saber se você é cabeleireiro ou deseja aprender sobre nosso método de fita adesiva.", voice='alice', language='pt-BR')
    resp.record(max_length=60, transcribe=False, recording_status_callback="/recording")
    resp.say("Obrigada, até mais!", voice='alice', language='pt-BR')
    return Response(str(resp), mimetype="text/xml")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
