import os
from flask import Flask, request, render_template, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import openai

app = Flask(__name__)

# Configuração Twilio
TWILIO_ACCOUNT_SID = "AC9c625b11d1695783923b691057363a06"
TWILIO_AUTH_TOKEN = "1c8add9939ee4a77227571a89dfc8437"
TWILIO_FROM_NUMBER = "+17752615122"

# Configuração OpenAI
oai_key = os.getenv("OPENAI_API_KEY")
openai.api_key = oai_key

# Memória da conversa
conversa = [
    {"role": "system", "content": "Você é a Pat Glam, uma atendente carismática, divertida e falante. Fale como uma brasileira descolada, com gírias leves e tom humano. Seu papel é conversar com cabeleireiros ou interessados no método de fita adesiva. Seja espontânea."},
    {"role": "assistant", "content": "Oi, meu bem! Aqui é a Pat Glam! Você já trabalha com cabelo ou quer entrar nesse mundão dos fios maravilhosos?"}
]

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"]
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        try:
            call = client.calls.create(
                to=telefone,
                from_=TWILIO_FROM_NUMBER,
                url="https://pati-call.onrender.com/voice"
            )
            status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
        except Exception as e:
            status = f"Erro ao iniciar ligação: {str(e)}"
    return render_template("index.html", status=status)

@app.route("/voice", methods=["GET", "POST"])
def voice():
    resp = VoiceResponse()
    gather = Gather(input='speech', action='/responder', method='POST', timeout=5)
    gather.say(conversa[-1]["content"], voice='Polly.Brazilian.Portuguese.Female')
    resp.append(gather)
    resp.redirect('/voice')  # se nada for dito, repete
    return Response(str(resp), mimetype="text/xml")

@app.route("/responder", methods=["POST"])
def responder():
    user_input = request.values.get('SpeechResult', '')
    if not user_input:
        return Response("<Response><Redirect>/voice</Redirect></Response>", mimetype="text/xml")

    conversa.append({"role": "user", "content": user_input})

    try:
        completion = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=conversa
        )
        resposta_pat = completion.choices[0].message.content
    except Exception as e:
        resposta_pat = "Ai, deu um bugzinho aqui... tenta de novo, meu bem!"

    conversa.append({"role": "assistant", "content": resposta_pat})

    resp = VoiceResponse()
    gather = Gather(input='speech', action='/responder', method='POST', timeout=5)
    gather.say(resposta_pat, voice='Polly.Brazilian.Portuguese.Female')
    resp.append(gather)
    resp.redirect('/voice')
    return Response(str(resp), mimetype="text/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
