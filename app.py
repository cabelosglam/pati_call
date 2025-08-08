
import os
import json
import smtplib
from email.mime.text import MIMEText
from functools import wraps
from typing import Dict, Any

from flask import Flask, request, render_template, Response, abort
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from openai import OpenAI

# Optional Redis (fallback to in-memory)
try:
    import redis  # type: ignore
except Exception:
    redis = None

load_dotenv()

app = Flask(__name__)
app.config["PREFERRED_URL_SCHEME"] = "https"

# === ENV ===
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # whatsapp:+14155238886
TWILIO_WHATSAPP_TO = os.environ.get("TWILIO_WHATSAPP_TO")      # whatsapp:+55XXXXXXXXXXX
TWILIO_VALIDATE_SIGNATURE = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "true").lower() == "true"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")  # set to a model you have access to
USE_GPT_TONE = os.environ.get("USE_GPT_TONE", "true").lower() == "true"

EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", EMAIL_USER or "")
EMAIL_TO = os.environ.get("EMAIL_TO", "cabelosglam@gmail.com")

REDIS_URL = os.environ.get("REDIS_URL", "")

# === HELPERS ===
def abs_url(path: str) -> str:
    if not PUBLIC_BASE_URL:
        print("[WARN] PUBLIC_BASE_URL not set. Twilio won't reach your local server. Set PUBLIC_BASE_URL to your ngrok/Render domain.")
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{PUBLIC_BASE_URL}{path}"

USE_MEMORY = False
_memory = {}

def _mem_set(key, value):
    _memory[key] = value

def _mem_get(key):
    return _memory.get(key)

def _mem_rpush(key, value):
    _memory.setdefault(key, [])
    _memory[key].append(value)

def _mem_lrange(key, start, end):
    lst = _memory.get(key, [])
    if end == -1:
        end = len(lst) - 1
    return lst[start:end+1]

def _mem_delete(key):
    _memory.pop(key, None)

if not REDIS_URL or REDIS_URL.startswith("memory://") or redis is None:
    USE_MEMORY = True
    r = None
    print("[STORE] Using in-memory store.")
else:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    print(f"[STORE] Using Redis at {REDIS_URL}")

# === Clients ===
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === Conversation helpers ===
def conv_key(call_sid: str) -> str:
    return f"conv:{call_sid}"

def state_key(call_sid: str) -> str:
    return f"state:{call_sid}"

def append_conv(call_sid: str, role: str, content: str) -> None:
    payload = json.dumps({"role": role, "content": content})
    try:
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
            _mem_delete(state_key(call_sid))
        else:
            r.delete(conv_key(call_sid))
            r.delete(state_key(call_sid))
    except Exception as e:
        print(f"[STORE ERROR] clear_conv: {e}")

def get_state(call_sid: str) -> Dict[str, Any]:
    try:
        if USE_MEMORY:
            state = _mem_get(state_key(call_sid))
        else:
            state_json = r.get(state_key(call_sid))
            state = json.loads(state_json) if state_json else None
        if not state:
            state = {"stage": "classify_role", "role": None, "city": None, "experience": None, "instagram": None, "whatsapp_ok": None}
            set_state(call_sid, state)
        return state
    except Exception as e:
        print(f"[STORE ERROR] get_state: {e}")
        return {"stage": "classify_role", "role": None, "city": None, "experience": None, "instagram": None, "whatsapp_ok": None}

def set_state(call_sid: str, state: Dict[str, Any]) -> None:
    try:
        if USE_MEMORY:
            _mem_set(state_key(call_sid), state)
        else:
            r.set(state_key(call_sid), json.dumps(state))
    except Exception as e:
        print(f"[STORE ERROR] set_state: {e}")

# === Prompt base ===
PAT_SYSTEM_PROMPT = (
    "Você é a Pat Glam — a consultora virtual oficial da Glam Hair Brand. "
    "Fale com elegância, carisma e humor sofisticado. "
    "Nunca diga que é IA; nunca passe preços direto; sempre qualifique com gentileza. "
    "Use termos da marca: 'glamour é essencial', 'Patrícia poderosa', 'salão de sucesso', "
    "'fita adesiva premium', 'linha Brasileira do Sul', 'cabelo russo original', "
    "'extensão que entrega status', 'agenda cheia com Glam'."
)

# === Signature validation ===
def require_twilio_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not TWILIO_VALIDATE_SIGNATURE:
            return f(*args, **kwargs)
        if not TWILIO_AUTH_TOKEN:
            return f(*args, **kwargs)
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        url = request.url
        if forwarded_proto and url.startswith("http://"):
            url = url.replace("http://", f"{forwarded_proto}://", 1)
        form = request.form.to_dict(flat=True)
        if not validator.validate(url, form, signature):
            print(f"[AUTH] Invalid Twilio signature for {url}")
            return abort(403, description="Invalid Twilio signature.")
        return f(*args, **kwargs)
    return wrapper

# === NLU helpers ===
def norm(s: str) -> str:
    return (s or "").lower().strip()

def detect_role(text: str, digits: str):
    t = norm(text)
    if digits == "1" or "prof" in t or "cabele" in t:
        return "professional"
    if digits == "2" or "cliente" in t or "final" in t:
        return "consumer"
    return None

# === Stage engine ===
def next_prompt(call_sid: str, user_text: str, digits: str) -> str:
    state = get_state(call_sid)
    stage = state.get("stage")

    if stage == "classify_role":
        role = detect_role(user_text, digits)
        if role == "professional":
            state["role"] = "professional"
            state["stage"] = "ask_city"
            set_state(call_sid, state)
            return "Amei saber! Você atende em qual cidade, Patrícia Extensionista?"
        elif role == "consumer":
            state["role"] = "consumer"
            state["stage"] = "final_msg"
            set_state(call_sid, state)
            return ("Você é uma Patrícia Final exigente, eu amo! Vendemos só para profissionais habilitados, "
                    "mas posso te ajudar: indique nosso método para a sua cabeleireira e peça para falar com a Glam. "
                    "Quer que eu envie um guia para você mostrar pra ela?")
        else:
            return "Só para eu te atender direitinho: você é profissional da beleza (aperte 1) ou é cliente final (aperte 2)?"
    elif stage == "ask_city":
        state["city"] = user_text.strip() or state.get("city")
        state["stage"] = "ask_experience"
        set_state(call_sid, state)
        return "Perfeito! E você já trabalha com extensões? Qual método usa hoje no salão?"
    elif stage == "ask_experience":
        state["experience"] = user_text.strip() or state.get("experience")
        state["stage"] = "ask_instagram"
        set_state(call_sid, state)
        return "Maravilha. Me passa o @ do Instagram do salão ou o seu, para eu anotar com glitter dourado aqui?"
    elif stage == "ask_instagram":
        state["instagram"] = user_text.strip() or state.get("instagram")
        state["stage"] = "ask_whatsapp"
        set_state(call_sid, state)
        return "Quer que nossa equipe te chame no WhatsApp para credenciar e te enviar o catálogo e agenda da Masterclass? Diga 'sim' ou 'não'."
    elif stage == "ask_whatsapp":
        t = norm(user_text)
        state["whatsapp_ok"] = True if ("sim" in t or t in ("s","ss","pode")) else False if ("não" in t or "nao" in t or t=="n") else None
        # Finaliza
        state["stage"] = "wrap"
        set_state(call_sid, state)
        if state["whatsapp_ok"] is True:
            return "Feito, Patrícia poderosa! Já pedindo para a equipe te chamar no WhatsApp. Glamour é essencial — nos vemos em breve!"
        elif state["whatsapp_ok"] is False:
            return "Sem problemas! Vou te mandar um resumo por e-mail com os próximos passos. Conta comigo sempre!"
        else:
            return "Perfeito. Se preferir, posso chamar no WhatsApp depois. Posso te ajudar em mais alguma coisa agora?"
    elif stage == "final_msg":
        # Consumidora final — encerrar com orientação gentil
        state["stage"] = "wrap"
        set_state(call_sid, state)
        return ("Combinado! Segue a Glam no Instagram e mostra para a sua cabeleireira o método de fita adesiva premium. "
                "Quando ela falar com a gente, eu cuido do resto. Beijos da Pat Glam!")
    else:
        # wrap / default
        return "Anotado! Posso te ajudar em mais alguma coisa?"

def style_with_gpt(base_text: str, state: Dict[str, Any]) -> str:
    if not USE_GPT_TONE:
        return base_text
    try:
        # Compose a short style prompt using known slots
        persona = (
            "Adapte o texto a seguir para o tom da Pat Glam (premium, acolhedor, charmoso), "
            "mantendo a pergunta final clara e objetiva. Se houver cidade/instagram/método, cite com naturalidade."
        )
        slots = {k: v for k, v in state.items() if v}
        messages = [
            {"role": "system", "content": PAT_SYSTEM_PROMPT},
            {"role": "user", "content": f"{persona}\n\nSlots: {json.dumps(slots, ensure_ascii=False)}\n\nTexto base: {base_text}"}
        ]
        completion = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=160,
        )
        out = (completion.choices[0].message.content or "").strip()
        return out or base_text
    except Exception as e:
        print(f"[OPENAI TONE ERROR] {e}")
        return base_text

# === Routes ===

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"].strip()
        try:
            call = twilio_client.calls.create(
                to=telefone,
                from_=TWILIO_FROM_NUMBER,
                url=abs_url("/voice"),
                method="POST",
                status_callback=abs_url("/status_callback"),
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
    resp = VoiceResponse()
    ssml = ('<speak>Oiê! Aqui é a <sub alias="Pati Glam">Pat Glam</sub>, da Glam Hair Brand. '
            'Me conta: você é profissional da beleza (aperte 1) ou é cliente final (aperte 2)?</speak>')
    gather = Gather(
        input="speech dtmf",
        action=abs_url("/resposta"),
        method="POST",
        language="pt-BR",
        hints="profissional, cliente final, cabeleireiro, extensão, curso, comprar, preço, salão",
        timeout=8,
        speech_timeout="auto",
        speech_model="phone_call",
        action_on_empty_result=True,
        partial_results=True,
        partial_result_callback=abs_url("/partial"),
        partial_result_callback_method="POST",
        num_digits=1,
    )
    gather.say(ssml, language="pt-BR")
    resp.append(gather)

    resp.say("Não consegui te ouvir agora, mas podemos tentar de novo mais tarde. Beijos da Pat Glam!", language="pt-BR")
    return Response(str(resp), mimetype="text/xml")

@app.route("/resposta", methods=["POST"])
@require_twilio_auth
def resposta():
    call_sid = request.form.get("CallSid", "unknown")
    speech = request.form.get("SpeechResult", "") or ""
    digits = request.form.get("Digits", "") or ""
    confidence = request.form.get("Confidence", "") or ""

    # DTMF fallback mapping
    if digits == "1":
        speech = "Eu sou profissional da beleza."
    elif digits == "2":
        speech = "Eu sou cliente final."

    print(f"[DEBUG] CallSid={call_sid} | SpeechResult={speech!r} | Confidence={confidence} | Digits={digits!r}")

    # Orchestrate stage-based flow
    base_reply = next_prompt(call_sid, speech, digits)

    # Try to style with GPT (optional). If it fails, keep base text.
    state = get_state(call_sid)
    reply = style_with_gpt(base_reply, state)

    # Store turns
    append_conv(call_sid, "user", speech)
    append_conv(call_sid, "assistant", reply)

    # Echo what we heard (escaped) + the reply
    eco = f"Eu ouvi: {speech or 'nada'}."
    safe_text = eco + " " + reply
    # Escape for SSML safety
    from xml.sax.saxutils import escape as _esc
    safe_text = _esc(safe_text)

    resp = VoiceResponse()
    gather = Gather(
        input="speech dtmf",
        action=abs_url("/resposta"),
        method="POST",
        language="pt-BR",
        hints="cidade, experiência, instagram, whatsapp, extensão, curso, comprar, preço, salão",
        timeout=8,
        speech_timeout="auto",
        speech_model="phone_call",
        action_on_empty_result=True,
        partial_results=True,
        partial_result_callback=abs_url("/partial"),
        partial_result_callback_method="POST",
        num_digits=1,
    )
    gather.say(f"<speak>{safe_text}</speak>", language="pt-BR")
    resp.append(gather)

    resp.say("Acho que não entendi bem agora. Podemos continuar em outro momento. Beijinhos!", language="pt-BR")
    return Response(str(resp), mimetype="text/xml")

@app.route("/partial", methods=["POST"])
def partial():
    call_sid = request.form.get("CallSid", "unknown")
    data = {k: v for k, v in request.form.items()}
    print(f"[PARTIAL] CallSid={call_sid} data={data}")
    return ("", 204)

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

def summarize_history(history, call_sid: str, duration: str, from_number: str, to_number: str) -> str:
    try:
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
            model=OPENAI_MODEL,
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
        raw = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in history]) or "(sem histórico)"
        return f"CallSid: {call_sid}\nResumo indisponível (erro ao gerar).\n\nTranscrição bruta:\n{raw}"

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
