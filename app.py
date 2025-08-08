import os
from flask import Flask, request, render_template
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import openai
from dotenv import load_dotenv

load_dotenv()

# Ambiente
app = Flask(__name__)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("BASE_URL")  # Ex: https://patglam.onrender.com

openai.api_key = OPENAI_API_KEY
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Página principal
@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"]
        try:
            call = client.calls.create(
                to=telefone,
                from_=TWILIO_FROM_NUMBER,
                url=f"{BASE_URL}/voice"  # Corrigido: URL absoluta
            )
            status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
        except Exception as e:
            status = f"Erro ao iniciar ligação: {str(e)}"
    return render_template("index.html", status=status)

# Rota de voz inicial
@app.route("/voice", methods=["POST"])
def voice():
    response = VoiceResponse()
    gather = Gather(input="speech", action="/processa_resposta", method="POST", timeout=5)
    gather.say(
        "Oiê! Aqui é a Pat Glam, da Glam Hair Brand. "
        "Antes de tudo, me conta: você é profissional da beleza ou quer aprender nosso método exclusivo de fita adesiva?",
        voice="Polly", language="pt-BR"
    )
    response.append(gather)
    response.redirect("/voice")  # Repete caso não responda
    return str(response)

# Processa a resposta
@app.route("/processa_resposta", methods=["POST"])
def processa_resposta():
    resposta_usuario = request.form.get("SpeechResult", "")
    resposta_usuario = resposta_usuario.lower()

    response = VoiceResponse()
    print(f"Usuário disse: {resposta_usuario}")

    if "cliente" in resposta_usuario:
        response.say("A Glam trabalha exclusivamente com profissionais. Mas se você ama cabelo incrível, peça para sua cabeleireira usar Glam!", voice="Polly", language="pt-BR")
    elif "profissional" in resposta_usuario or "beleza" in resposta_usuario:
        gather = Gather(input="speech", action="/cidade", method="POST", timeout=5)
        gather.say("Amei saber! Em qual cidade você atende?", voice="Polly", language="pt-BR")
        response.append(gather)
    else:
        response.say("Desculpa, não entendi. Você pode repetir?", voice="Polly", language="pt-BR")
        response.redirect("/voice")
    return str(response)

# Pega cidade
@app.route("/cidade", methods=["POST"])
def cidade():
    cidade = request.form.get("SpeechResult", "")
    cidade = cidade.strip().capitalize()

    response = VoiceResponse()
    gather = Gather(input="speech", action=f"/confirm_metodo?cidade={cidade}", method="POST", timeout=5)
    gather.say(f"Você atende em {cidade}, certo? Agora me conta: qual método de alongamento usa hoje?", voice="Polly", language="pt-BR")
    response.append(gather)
    return str(response)

# Confirma método
@app.route("/confirm_metodo", methods=["POST"])
def confirm_metodo():
    metodo = request.form.get("SpeechResult", "")
    cidade = request.args.get("cidade", "")
    metodo = metodo.strip().lower()

    response = VoiceResponse()
    gather = Gather(input="speech", action=f"/finaliza?cidade={cidade}&metodo={metodo}", method="POST", timeout=5)
    gather.say(f"Certo, então você está em {cidade} e usa o método {metodo}, é isso? Agora me fala seu arroba no Instagram pra equipe te achar!", voice="Polly", language="pt-BR")
    response.append(gather)
    return str(response)

# Finaliza e envia WhatsApp
@app.route("/finaliza", methods=["POST"])
def finaliza():
    instagram = request.form.get("SpeechResult", "")
    cidade = request.args.get("cidade", "")
    metodo = request.args.get("metodo", "")

    response = VoiceResponse()
    response.say(f"Perfeito! Já pedi pra equipe Glam te chamar no WhatsApp. Glamour é essencial — nos vemos em breve!", voice="Polly", language="pt-BR")
    return str(response)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render injeta PORT
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
