import os
import json
from functools import wraps
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText

from flask import Flask, request, render_template, Response, url_for, abort
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather

from dotenv import load_dotenv
from openai import OpenAI

# --- Redis optional (fallback to memory) ---
try:
    import redis  # optional
except Exception:
    redis = None

load_dotenv()

app = Flask(__name__)
app.config["PREFERRED_URL_SCHEME"] = "https"

# === Env ===
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")

# Optional WhatsApp for summary notifications
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # e.g., 'whatsapp:+14155238886'
TWILIO_WHATSAPP_TO = os.environ.get("TWILIO_WHATSAPP_TO")      # e.g., 'whatsapp:+55XXXXXXXXXXX'

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Email setup (for call summaries)
EMAIL_HOST = os.environ.get("EMAIL_HOST")          # e.g., 'smtp.gmail.com'
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER")          # full email/login
EMAIL_PASS = os.environ.get("EMAIL_PASS")          # app password
EMAIL_FROM = os.environ.get("EMAIL_FROM", EMAIL_USER or "")
EMAIL_TO = os.environ.get("EMAIL_TO", "cabelosglam@gmail.com")

# Redis (memory across processes)
REDIS_URL = os.environ.get("REDIS_URL", "")

USE_MEMORY = False
_memory = {}

def _mem_rpush(key, value):
    _memory.setdefault(key, [])
    _memory[key].append(value)

def _mem_lrange(key, start, end):
    lst = _memory.get(key, [])
    # emulate redis lrange where end = -1 means end of list
    if end == -1:
        end = len(lst) - 1
    return lst[start:end+1]

def _mem_delete(key):
    _memory.pop(key, None)

if not REDIS_URL or REDIS_URL.startswith("memory://") or redis is None:
    # fallback to in-memory store
    USE_MEMORY = True
    r = None
    print("[MEMORY] Using in-memory conversation store (no Redis).")
else:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    print(f"[REDIS] Using Redis at {REDIS_URL}")

# === Clients ===
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === Keys/helpers for conversation memory ===
def conv_key(call_sid: str) -> str:
    return f"conv:{call_sid}"

def append_conv(call_sid: str, role: str, content: str) -> None:
    try:
        payload = json.dumps({"role": role, "content": content})
        if USE_MEMORY:
            _mem_rpush(conv_key(call_sid), payload)
        else:
            r.rpush(conv_key(call_sid), payload)
    except Exception as e:
        print(f"[STORE ERROR] append_conv: {e}")

def get_conv(call_sid: str):
    try:
        if USE_MEMORY:
            raw = _mem_lrange(conv_key(call_sid), 0, -1)
        else:
            raw = r.lrange(conv_key(call_sid), 0, -1)
        return [json.loads(x) for x in raw]
    except Exception as e:
        print(f"[STORE ERROR] get_conv: {e}")
        return []

def clear_conv(call_sid: str) -> None:
    try:
        if USE_MEMORY:
            _mem_delete(conv_key(call_sid))
        else:
            r.delete(conv_key(call_sid))
    except Exception as e:
        print(f"[STORE ERROR] clear_conv: {e}")

# === Prompt da Pat Glam ===
PAT_SYSTEM_PROMPT = (
    "Você é a Pat Glam — a consultora virtual oficial da Glam Hair Brand. "
    "Não é apenas uma atendente: é a Patrícia fundadora, mentora e alma fashionista da marca. "
    "Fale com elegância, carisma e humor sofisticado. "
    "Chame clientes finais de 'Patrícia' (carinhoso) e profissionais como 'Patrícia Extensionista', "
    "'Patrícia Profissional' ou 'Pat Poderosa'. "
    "REGRAS: nunca diga que é IA; nunca passe preços direto; sempre qualifique com gentileza; "
    "se for cliente final, explique que vendemos só para profissionais, com carinho, "
    "incentive a seguir a Glam no Instagram e indicar o método para a cabeleireira. "
    "Use termos da marca: 'glamour é essencial', 'Patrícia poderosa', 'salão de sucesso', "
    "'fita adesiva premium', 'linha Brasileira do Sul', 'cabelo russo original', "
    "'extensão que entrega status', 'agenda cheia com Glam'. "
    "Seu objetivo: gerar conexão, desejo e pertencimento, mantendo tom premium e acolhedor."
)

# === Decorator: valida requisições do Twilio ===
def require_twilio_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not TWILIO_AUTH_TOKEN:
            return f(*args, **kwargs)

        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")

        # Corrige esquema atrás de proxy (Render/NGINX)
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
        url = request.url
        if forwarded_proto and url.startswith("http://"):
            url = url.replace("http://", f"{forwarded_proto}://", 1)

        form = request.form.to_dict(flat=True)
        if not validator.validate(url, form, signature):
            return abort(403, description="Invalid Twilio signature.")
        return f(*args, **kwargs)
    return wrapper

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"].strip()
        try:
            call = twilio_client.calls.create(
                to=telefone,
                from_=TWILIO_FROM_NUMBER,
                url=url_for("voice", _external=True, _scheme="https"),
                method="POST",
                status_callback=url_for("status_callback", _external=True, _scheme="https"),
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                status_callback_method="POST",
            )
            status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
        except Exception as e:
            status = f"Erro ao iniciar ligação: {e}"
    return render_template("index.html", status=status)

@app.route("/voice", methods=["GET", "POST"])
@require_twilio_auth
def voice():
    """Primeira pergunta + coleta de voz (voz mais natural via Polly)."""
    resp = VoiceResponse()
    gather = Gather(
        input="speech",
        action=url_for("resposta", _external=True, _scheme="https"),
        method="POST",
        language="pt-BR",
        hints="fita adesiva, extensão, curso, comprar, preço, salão, Goiânia, Brasileira do Sul, cabelo russo, Glam",
        timeout=8,
        speech_timeout="auto",
    )
    # Polly Camila (pt-BR) -> voz mais natural
    gather.say(
        "Oiê! Aqui é a Pat Glam, da Glam Hair Brand. Me conta: "
        "você é profissional da beleza ou quer aprender nosso método de fita adesiva premium?",
        language="pt-BR",
        voice="Polly.Camila"
    )
    resp.append(gather)

    resp.say(
        "Não consegui te ouvir agora, mas podemos tentar de novo mais tarde. Beijos da Pat Glam!",
        language="pt-BR",
        voice="Polly.Camila"
    )
    return Response(str(resp), mimetype="text/xml")

@app.route("/resposta", methods=["POST"])
@require_twilio_auth
def resposta():
    fala_cliente = request.form.get("SpeechResult", "") or ""
    call_sid = request.form.get("CallSid", "unknown")
    print(f"[DEBUG] CallSid={call_sid} | Cliente disse: {fala_cliente}")

    resposta_pat = gerar_resposta_gpt(fala_cliente, call_sid)

    resp = VoiceResponse()
    gather = Gather(
        input="speech",
        action=url_for("resposta", _external=True, _scheme="https"),
        method="POST",
        language="pt-BR",
        hints="fita adesiva, extensão, curso, comprar, preço, salão, Goiânia, Brasileira do Sul, cabelo russo, Glam",
        timeout=8,
        speech_timeout="auto",
    )
    gather.say(resposta_pat, language="pt-BR", voice="Polly.Camila")
    resp.append(gather)

    resp.say(
        "Acho que não entendi bem agora. Podemos continuar em outro momento. Beijinhos!",
        language="pt-BR",
        voice="Polly.Camila"
    )
    return Response(str(resp), mimetype="text/xml")

@app.route("/status_callback", methods=["POST"])
@require_twilio_auth
def status_callback():
    call_sid = request.form.get("CallSid", "unknown")
    call_status = request.form.get("CallStatus")
    duration = request.form.get("CallDuration")
    from_number = request.form.get("From")
    to_number = request.form.get("To")

    print(f"[CALL] {call_sid} status: {call_status} (dur: {duration}s) from={from_number} to={to_number}")

    if call_status == "completed":
        try:
            history = get_conv(call_sid)
            summary_md = summarize_history(history, call_sid, duration, from_number, to_number)
            send_summary(summary_md)
            if TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_TO:
                send_whatsapp(summary_md)
            print(f"[CALL] {call_sid} resumo enviado.")
        except Exception as e:
            print(f"[SUMMARY ERROR] {e}")
        finally:
            clear_conv(call_sid)
            print(f"[CALL] {call_sid} finalizada. Memória limpa.")

    return ("", 204)

def gerar_resposta_gpt(fala_cliente: str, call_sid: str) -> str:
    try:
        if not fala_cliente.strip():
            return "Hmmm, não consegui te ouvir. Pode repetir, por favor?"

        # contexto curto para reduzir latência/custo
        history = get_conv(call_sid)[-6:]
        messages = [{"role": "system", "content": PAT_SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": f"Pessoa disse: {fala_cliente}"})

        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=180,
        )
        resposta = (completion.choices[0].message.content or "").strip()

        append_conv(call_sid, "user", fala_cliente)
        append_conv(call_sid, "assistant", resposta)

        return resposta or "Perdão, deu uma travadinha aqui. Pode repetir?"
    except Exception as e:
        print(f"[OPENAI ERROR] {e}")
        return "Desculpa, tive um pequeno deslize técnico. Pode repetir pra mim, por favor?"

def summarize_history(history, call_sid: str, duration: str, from_number: str, to_number: str) -> str:
    try:
        # Concatena a conversa em texto
        convo_lines = []
        for m in history:
            who = "Cliente" if m.get("role") == "user" else "Pat"
            content = (m.get("content") or "").strip()
            if content:
                convo_lines.append(f"{who}: {content}")
        convo_text = "\n".join(convo_lines) if convo_lines else "(sem histórico)"

        prompt = f"""Você é uma assistente de CRM da Glam Hair Brand.
A seguir está a conversa entre a Pat Glam (assistente) e um contato.

Produza um resumo PROFISSIONAL em Markdown com as seções:
- **Perfil do contato** (profissional da beleza ou cliente final? inferir cidade se citada)
- **Assunto principal** (em 1–2 linhas)
- **Sinais de interesse e objeções**
- **Oportunidades de venda** (bullets curtos)
- **Próximos passos recomendados** (3 bullets práticos)
- **Frases-chave do lead** (entre aspas)
- **Tags** (formato: #fitaAdesiva #curso #profissional ...)

Inclua cabeçalho com: CallSid: {call_sid} | Duração: {duration or '—'}s | De: {from_number or '—'} | Para: {to_number or '—'}

Conversa:
{convo_text}
"""

        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Você resume conversas comerciais para CRM com clareza e objetividade."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=500,
        )
        summary = (completion.choices[0].message.content or "").strip()
        return summary or f"CallSid: {call_sid}\n(Conversa vazia para resumir)"
    except Exception as e:
        print(f"[OPENAI SUMMARY ERROR] {e}")
        return f"CallSid: {call_sid}\nResumo indisponível (erro ao gerar)."

def send_summary(markdown_text: str) -> None:
    if not EMAIL_HOST or not EMAIL_USER or not EMAIL_PASS or not EMAIL_FROM or not EMAIL_TO:
        print("[EMAIL] Config incompleta. Resumo não enviado por email.")
        return
    msg = MIMEText(markdown_text, "plain", "utf-8")
    msg["Subject"] = "Resumo da ligação - Pat Glam"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        print(f"[EMAIL] Resumo enviado para {EMAIL_TO}")

def send_whatsapp(text: str) -> None:
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=TWILIO_WHATSAPP_TO,
            body=(text if len(text) <= 1500 else text[:1497] + '...')
        )
        print(f"[WA] Resumo enviado para {TWILIO_WHATSAPP_TO}")
    except Exception as e:
        print(f"[WA ERROR] {e}")

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
