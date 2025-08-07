import os
from flask import Flask, request, render_template, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import openai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")

# OpenAI
openai.api_key = os.environ.get("OPENAI_API_KEY")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"]
        try:
            call = client.calls.create(
                to=telefone,
                from_=TWILIO_FROM_NUMBER,
                url="https://pati-call.onrender.com/voice"
            )
            status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
        except Exception as e:
            status = f"Erro: {e}"
    return render_template("index.html", status=status)

@app.route("/voice", methods=["GET", "POST"])
def voice():
    resp = VoiceResponse()
    gather = Gather(
        input='speech',
        timeout=5,
        language='pt-BR',
        speech_model='default',
        action='/resposta',
        method='POST',
        voice='Polly.Brazilian.Portuguese.Female'
    )
    gather.say("Oiê! Aqui é a Pat Glam da Glam Hair Brand! Me conta, você é cabeleireiro ou quer aprender nosso método de fita adesiva topíssimo?", language='pt-BR')
    resp.append(gather)
    resp.say("Hmm... não ouvi nada, mas tudo bem! Beijos da Pat Glam!", language='pt-BR')
    return Response(str(resp), mimetype='text/xml')

@app.route("/resposta", methods=["POST"])
def resposta():
    fala_cliente = request.form.get("SpeechResult", "")
    resposta_pat = gerar_resposta_gpt(fala_cliente)

    resp = VoiceResponse()
    gather = Gather(
        input='speech',
        timeout=5,
        language='pt-BR',
        speech_model='default',
        action='/resposta',
        method='POST',
        voice='Polly.Brazilian.Portuguese.Female'
    )
    gather.say(resposta_pat, language='pt-BR')
    resp.append(gather)
    resp.say("Acho que não entendi bem. Tenta me contar de novo depois, tá? Beijinhos!", language='pt-BR')
    return Response(str(resp), mimetype='text/xml')

def gerar_resposta_gpt(fala_cliente):
    prompt = f"""
Você é a Pat Glam, uma atendente carismática, divertida e especialista em extensões capilares com fita adesiva. 
Responda de forma amigável, como se estivesse conversando com um cabeleireiro ou uma pessoa interessada no método Glam. 
Use gírias leves e um tom próximo.

Pessoa disse: "{fala_cliente}"
Resposta da Pat:
"""
    try:
        resposta = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Você é a Pat Glam, especialista da Glam Hair Brand."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=120
        )
        return resposta['choices'][0]['message']['content'].strip()
    except Exception as e:
        return "Desculpa, deu uma travadinha aqui. Você pode repetir?"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
