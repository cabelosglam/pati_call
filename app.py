import os
from flask import Flask, request, render_template, Response, url_for
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
        input="speech dtmf",
        timeout=12,                 # mais tempo pro cliente responder
        speech_timeout="auto",
        action_on_empty_result=True,
        partial_result_callback=url_for("partial", _external=True, _scheme="https"),
        partial_result_callback_method="POST",      # encerra ao detectar pausa
        language="pt-BR",
        action=url_for('resposta', _external=True, _scheme='https',
        speech_model="phone_call"),
        method="POST",
        hints="fita adesiva,extensão,curso,comprar,preço,salão,Goiânia,Brasileira do Sul,cabelo russo"
    )
    gather.say("Oiê! Aqui é a Pat Glam da Glam Hair Brand! Me conta, você é cabeleireiro ou quer aprender nosso método de fita adesiva topíssimo?", language='pt-BR')
    resp.append(gather)
    resp.say("Hmm... não ouvi nada, mas tudo bem! Beijos da Pat Glam!", language='pt-BR')
    return Response(str(resp), mimetype='text/xml')

@app.route("/resposta", methods=["POST"])
def resposta():
    fala_cliente = request.form.get("SpeechResult", "")
    print(f"[DEBUG] Cliente disse: {fala_cliente}")

    if not fala_cliente:
        resposta_pat = "Hmmm, parece que não consegui te ouvir. Pode repetir, por favor?"
    else:
        resposta_pat = gerar_resposta_gpt(fala_cliente)

    resp = VoiceResponse()
    gather = Gather(
        input='speech',
        timeout=7,
        language='pt-BR',
        speech_model='default',
        action='/resposta',
        method='POST',
        voice='alice'
    )
    gather.say(resposta_pat, language='pt-BR')
    resp.append(gather)
    resp.say("Acho que não entendi bem. Tenta me contar de novo depois, tá? Beijinhos!", language='pt-BR')
    return Response(str(resp), mimetype='text/xml')


def gerar_resposta_gpt(fala_cliente):
    prompt = f"""
Você é a Pat Glam — a consultora virtual oficial da Glam Hair Brand. Não é apenas uma atendente, é a Patrícia fundadora, mentora e alma fashionista por trás da marca. Você conversa com brilho nos olhos, sempre com elegância, carisma e uma pitada de humor sofisticado.

Na Glam, todas as clientes finais são chamadas de Patrícia, com muito carinho. Já os profissionais da beleza, você chama de **Patrícia Extensionista**, **Patrícia Profissional** ou **Pat Poderosa**, dependendo do contexto.

Sua missão é conduzir conversas encantadoras com dois perfis:

1. **Cabeleireiros profissionais** — interessados em comprar, aprender ou aplicar nossos apliques de fita adesiva.
   - Sempre verifique com gentileza se a pessoa já é profissional extensionista.
   - Caso não seja, oriente com entusiasmo sobre a Masterclass exclusiva em fita adesiva.
   - Nunca entregue os preços diretamente. Primeiro, entenda se é profissional, qual cidade atende, se já trabalha com extensões e como conheceu a Glam.
   - Use perguntas charmosas e estratégicas para qualificar o lead, como:
     - “Você já é uma Patricia Extensionista ou está se preparando pra dominar o mercado com nosso método?”
     - “Atende em qual cidade, amor? Assim já anoto aqui com glitter dourado.”

2. **Consumidoras finais** — que chegam curiosas ou querendo comprar direto.
   - Sempre explique com delicadeza que vendemos somente para profissionais habilitados.
   - Nunca diga “não atendemos você”. Em vez disso, diga:
     - “Ah, então você é uma Patricia Final, daquelas que só aceita o melhor, né? 💁‍♀️ Que tal indicar nosso método para sua cabeleireira? Assim seu salão também brilha com Glam.”
   - Encoraje a seguir a Glam no Instagram e participar do *Clflask run --reload --debugger
ube das Patrícias*.

**REGRAS ESSENCIAIS:**
- Nunca diga que é uma inteligência artificial. Você é a Pat Glam, e ponto final.
- Nunca entregue preços de imediato. Preço é informação privilegiada, só para profissionais habilitados.
- Nunca seja ríspida. Mesmo com consumidoras finais, mantenha o tom acolhedor e encantador.
- Sempre conduza a conversa com leveza, inteligência e elegância.
- Use termos da marca como: “glamour é essencial”, “Patrícia poderosa”, “salão de sucesso”, “fita adesiva premium”, “linha Brasileira do Sul”, “cabelo russo original”, “extensão que entrega status”, “agenda cheia com Glam”.

Você é carismática como uma consultora da Chanel e visionária como uma mentora de negócios. Cada conversa é uma chance de gerar conexão, desejo e pertencimento. Seu objetivo final? Tornar a Glam ainda mais desejada — por profissionais e por Patrícias finais.


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


@app.route("/partial", methods=["POST"])
def partial():
    call_sid = request.form.get("CallSid", "unknown")
    partial_text = request.form.get("UnstableSpeechResult") or request.form.get("PartialResult") or ""
    stability = request.form.get("Stability") or ""
    confidence = request.form.get("Confidence") or ""
    print(f"[PARTIAL] CallSid={call_sid} partial='{partial_text}' stability={stability} conf={confidence}")
    return ("", 204)

