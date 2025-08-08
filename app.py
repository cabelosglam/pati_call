import os
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configurações
VOICE_ID = os.environ.get("TTS_VOICE", "pt-BR-Chirp3-HD-Aoede")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client_openai = OpenAI(api_key=OPENAI_API_KEY)

# Memória curta por chamada (em memória local)
call_memory = {}

# Prompt base com instruções da Pat Glam
PROMPT_INICIAL = """
Você é a Pat Glam, atendente virtual da Glam Hair Brand.
Fale como uma mulher brasileira simpática, estilosa, segura, divertida e profissional.
Sempre que o cliente falar algo, repita de forma natural para confirmar, como:
"Ah, entendi. Você está em Goiânia, certo?"

Se a pessoa perguntar:

- "Vocês vendem para cliente final?" → Responda: "Ahhh, não vendemos para cliente final, tá? Nossos apliques são exclusivos para profissionais credenciados Glam. Mas posso te indicar um!"
- "Vocês enviam para o Brasil todo?" → Responda: "Com certeza! A Glam envia para todo o Brasil com frete super seguro."
- "Quando a Glam foi criada?" → Responda: "A Glam nasceu em 2012 com o propósito de elevar o padrão das extensões capilares. Somos especialistas em fita adesiva premium."

Se a pessoa não falar nada, incentive com frases como:
"Pode me perguntar o que quiser, viu? Tô aqui pra te ajudar."
"""

def gerar_resposta(texto_usuario, sid):
    memoria = call_memory.get(sid, "")
    prompt = PROMPT_INICIAL + "\nHistórico:\n" + memoria + f"\nUsuário: {texto_usuario}\nPat Glam:"
    
    resposta = client_openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": PROMPT_INICIAL},
            {"role": "user", "content": texto_usuario}
        ],
        max_tokens=200,
        temperature=0.7,
    )

    texto_resposta = resposta.choices[0].message.content.strip()

    # Atualiza memória curta
    call_memory[sid] = memoria + f"\nUsuário: {texto_usuario}\nPat Glam: {texto_resposta}"
    return texto_resposta

@app.route("/voice", methods=["POST"])
def voice():
    sid = request.form.get("CallSid")
    speech_result = request.form.get("SpeechResult", "").strip()
    response = VoiceResponse()

    if not speech_result:
        gather = Gather(input="speech", timeout=3, speech_timeout="auto", action="/voice", method="POST")
        gather.say("Oi, aqui é a Pat Glam. Seja bem-vinda! Me conta, o que você gostaria de saber?", language="pt-BR", voice=VOICE_ID)
        response.append(gather)
        response.redirect("/voice")
    else:
        resposta_pat = gerar_resposta(speech_result, sid)
        gather = Gather(input="speech", timeout=3, speech_timeout="auto", action="/voice", method="POST")
        gather.say(resposta_pat, language="pt-BR", voice=VOICE_ID)
        response.append(gather)
        response.redirect("/voice")

    return Response(str(response), mimetype="text/xml")

@app.route("/", methods=["GET"])
def home():
    return "Pat Glam está rodando com inteligência conversacional."

if __name__ == "__main__":
    app.run(debug=True)
