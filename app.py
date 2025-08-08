import os
import json
import smtplib
from email.mime.text import MIMEText
from functools import wraps

from flask import Flask, request, render_template, Response, abort
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from openai import OpenAI

VOICE_ID = os.environ.get("TTS_VOICE", "pt-BR-Chirp3-HD-Kore")

# Optional Redis (fallback to in-memory)
try:
    import redis  # type: ignore
except Exception:
    redis = None

load_dotenv()

app = Flask(__name__)
app.config["PREFERRED_URL_SCHEME"] = "https"

# === SSML helper for more natural TTS ===
def ssml_pat(text: str) -> str:
    text = text.replace("Pat Glam", '<sub alias="Pati Glam">Pat Glam</sub>')
    text = text.replace("@", " arroba ")
    return f'''
<speak>
  <prosody rate="0.96" pitch="+2st">
    {text}
  </prosody>
</speak>
'''.strip()

# === ENV ===
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
TWILIO_VALIDATE_SIGNATURE = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "true").lower() == "true"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
USE_GPT_TONE = os.environ.get("USE_GPT_TONE", "true").lower() == "true"

EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", EMAIL_USER or "")
EMAIL_TO = os.environ.get("EMAIL_TO", "cabelosglam@gmail.com")

REDIS_URL = os.environ.get("REDIS_URL", "")

# WhatsApp autosend
WHATSAPP_AUTOSEND = os.environ.get("WHATSAPP_AUTOSEND", "false").lower() == "true"
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # e.g., whatsapp:+1415...

# === HELPERS ===
def abs_url(path: str) -> str:
    if not PUBLIC_BASE_URL:
        print("[WARN] PUBLIC_BASE_URL not set. Twilio won't reach your server.")
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

def _mem_delete(key):
    _memory.pop(key, None)

def _mem_rpush(key, value):
    _memory.setdefault(key, [])
    _memory[key].append(value)

def _mem_lrange(key, start, end):
    lst = _memory.get(key, [])
    if end == -1:
        end = len(lst) - 1
    return lst[start:end+1]

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

# === Conversation + Session helpers ===
def conv_key(call_sid: str) -> str:
    return f"conv:{call_sid}"

def sess_key(call_sid: str) -> str:
    return f"sess:{call_sid}"

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

def get_session(call_sid: str):
    try:
        if USE_MEMORY:
            raw = _mem_get(sess_key(call_sid))
        else:
            raw = r.get(sess_key(call_sid))
        return json.loads(raw) if raw else {
            "state": "run",
            "data": {},
            "auto_index": 0,
            "wa_sent": False
        }
    except Exception as e:
        print(f"[STORE ERROR] get_session: {e}")
        return {"state": "run", "data": {}, "auto_index": 0, "wa_sent": False}

def save_session(call_sid: str, sess: dict):
    try:
        raw = json.dumps(sess)
        if USE_MEMORY:
            _mem_set(sess_key(call_sid), raw)
        else:
            r.set(sess_key(call_sid), raw)
    except Exception as e:
        print(f"[STORE ERROR] save_session: {e}")

def clear_session(call_sid: str):
    try:
        if USE_MEMORY:
            _mem_delete(sess_key(call_sid))
            _mem_delete(conv_key(call_sid))
        else:
            r.delete(sess_key(call_sid))
            r.delete(conv_key(call_sid))
    except Exception as e:
        print(f"[STORE ERROR] clear_session: {e}")

# === Signature validation ===
def require_twilio_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not TWILIO_VALIDATE_SIGNATURE or not TWILIO_AUTH_TOKEN:
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

# === Brand tone ===
def glam_tone(base_text: str) -> str:
    if not USE_GPT_TONE:
        return base_text
    try:
        messages = [
            {"role":"system","content":
             "Você é a Pat Glam. Reescreva a fala mantendo a intenção, com elegância, humor sutil, "
             "e termos de marca (glamour é essencial, Patrícia poderosa, agenda cheia com Glam). "
             "Português do Brasil, conciso e caloroso."},
            {"role":"user","content": base_text},
        ]
        c = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=120,
        )
        return (c.choices[0].message.content or "").strip() or base_text
    except Exception as e:
        print(f"[OPENAI TONE ERROR] {e}")
        return base_text

# === Mixed-initiative planner ===
AUTO_QUESTIONS = [
    "Você é profissional da beleza ou cliente final? Se preferir, tecle 1 para profissional e 2 para cliente final.",
    "Você atende em qual cidade?",
    "Você já trabalha com fita adesiva ou quer começar agora com a Glam?",
]

FAQ_FACTS = (
    "A Glam vende apenas para profissionais credenciados; a cliente final deve indicar a marca à sua cabeleireira. "
    "Enviamos para todo o Brasil. Não passamos preços em ligações; o credenciamento libera catálogo. "
    "Nosso foco é extensão com fita adesiva premium e formação de profissionais."
)

PLANNER_SYSTEM = (
    "Você é a Pat Glam — a vendedora/consultora oficial da Glam Hair Brand (não diga que é IA). \n"
    "Objetivo: conduzir um papo natural, responder dúvidas e, sem forçar, coletar dados-chave: perfil (pro/final), cidade, experiência, instagram e consentimento para contato no WhatsApp. \n"
    "Tom: premium, acolhedor, espirituoso. Use termos da marca: 'glamour é essencial', 'Patrícia poderosa', 'agenda cheia com Glam'. \n"
    f"Informações oficiais: {FAQ_FACTS}\n"
    "Instruções: sempre responda o que a pessoa perguntar (se houver), confirme o que entendeu com uma frase curta (ex: 'Você é de Goiânia, né?'), e finalize a fala com uma pergunta aberta ou a próxima pergunta automática se ainda houver (máx 3). \n"
    "Saída obrigatória em JSON com as chaves: \n"
    "  reply: string (fala natural da Pat que responde e puxa o próximo passo),\n"
    "  extracted: {profile: 'pro'|'final'|null, city: string|null, experience: string|null, instagram: string|null, whatsapp_consent: true|false|null},\n"
    "  ask_auto: string|null (a próxima pergunta automática se ainda faltar; senão null),\n"
    "  end: boolean (true quando já deu para encerrar com elegância)."
)


def plan_turn(user_text: str, sess: dict) -> dict:
    data = sess.get("data", {})
    auto_index = int(sess.get("auto_index", 0))

    # Decide se ainda cabe uma pergunta automática
    next_auto = AUTO_QUESTIONS[auto_index] if auto_index < len(AUTO_QUESTIONS) else None

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": json.dumps({
            "so_far": data,
            "auto_index": auto_index,
            "next_auto": next_auto,
            "utterance": user_text or "",
        }, ensure_ascii=False)}
    ]
    out = None
    raw = None
    try:
        # 1) tentativa com JSON mode (model adequado)
        c = openai_client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=messages,
            temperature=0.5,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = c.choices[0].message.content or "{}"
        out = json.loads(raw)
    except Exception as e:
        print(f"[OPENAI PLAN ERROR#1] {e}")
        # 2) fallback sem JSON mode, forçando instrução e parseando manualmente
        try:
            messages_fallback = messages + [
                {"role": "system", "content": "Responda ESTRITAMENTE em JSON válido, sem markdown, sem comentários."}
            ]
            c2 = openai_client.chat.completions.create(
                model=PLANNER_MODEL,
                messages=messages_fallback,
                temperature=0.5,
                max_tokens=300,
            )
            raw2 = c2.choices[0].message.content or "{}"
            raw2 = _extract_json(raw2)
            out = json.loads(raw2)
        except Exception as e2:
            print(f"[OPENAI PLAN ERROR#2] {e2}")
            print(f"[OPENAI PLAN RAW] {raw!r}")
            out = {
                "reply": "Desculpa, deu um micro bug do glitter. Pode repetir?",
                "extracted": {},
                "ask_auto": next_auto,
                "end": False,
            }

    # Merge extracted slots
    ext = out.get("extracted") or {}
    for k, v in ext.items():
        if v in (None, ""):
            continue
        if k == "instagram":
            v = looks_like_instagram(v)
        data[k] = v

    # Update auto index if model asked one
    asked = out.get("ask_auto")
    if asked and next_auto and asked.strip()[:10] == next_auto.strip()[:10]:
        auto_index += 1

    sess["data"] = data
    sess["auto_index"] = auto_index
    return {
        "reply": out.get("reply") or (asked or next_auto) or "Pode me contar mais?",
        "end": bool(out.get("end")),
    }
# === Simple NLU helpers ===
def is_yes(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(w in t for w in ["sim","claro","por favor","pode","quero","manda","yes","yep"]) or t == "1"

def is_no(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(w in t for w in ["não","nao","negativo","prefiro não","prefiro nao","nope","não agora"]) or t == "2"

def looks_like_instagram(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = t.replace("arroba","@").replace(" ", "").lower()
    if not t.startswith("@"):
        t = "@" + t
    return t

# === Twilio helpers ===
def speak_and_gather(text: str, action_url: str) -> Response:
    resp = VoiceResponse()
    gather = Gather(
        input="speech dtmf",
        action=action_url,
        method="POST",
        language="pt-BR", voice=VOICE_ID,
        hints="fita adesiva, extensão, curso, comprar, preço, salão, Goiânia, Brasileira do Sul, cabelo russo, Glam",
        timeout=8,
        speech_timeout="auto",
        speech_model="phone_call",
        action_on_empty_result=True,
        partial_results=True,
        partial_result_callback=abs_url("/partial"),
        partial_result_callback_method="POST",
        num_digits=1,
    )
    gather.say(ssml_pat(glam_tone(text)), language="pt-BR", voice=VOICE_ID)
    resp.append(gather)
    return Response(str(resp), mimetype="text/xml")

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
    call_sid = request.form.get("CallSid", "unknown") or "unknown"
    sess = {"state": "run", "data": {}, "auto_index": 0, "wa_sent": False}
    save_session(call_sid, sess)
    append_conv(call_sid, "assistant", "Saudação inicial.")

    intro = (
        "Oiê! Aqui é a Pat Glam, da Glam Hair Brand. Eu mesma que te liguei — consultoria de verdade, sem robô. "
        "Vamos fazer um papo rápido pra te ajudar a lucrar com extensões?"
    )
    resp = VoiceResponse()
    resp.say(ssml_pat(glam_tone(intro)), language="pt-BR", voice=VOICE_ID)
    # 1ª pergunta automática
    return speak_and_gather(AUTO_QUESTIONS[0], abs_url("/resposta"))

@app.route("/resposta", methods=["POST"])
@require_twilio_auth
def resposta():
    call_sid = request.form.get("CallSid", "unknown")
    speech = (request.form.get("SpeechResult", "") or "").strip()
    digits = (request.form.get("Digits", "") or "").strip()
    confidence = request.form.get("Confidence", "") or ""

    print(f"[DEBUG] CallSid={call_sid} | SpeechResult={speech!r} | Confidence={confidence} | Digits={digits!r}")

    if speech:
        append_conv(call_sid, "user", speech)

    sess = get_session(call_sid)

    # Map quick DTMF for first question (1 pro / 2 final) and consents later
    if digits in ("1","2"):
        data = sess.get("data", {})
        if "profile" not in data:
            data["profile"] = "pro" if digits == "1" else "final"
            sess["data"] = data
            sess["auto_index"] = max(sess.get("auto_index", 0), 1)  # contamos a 1ª automática

    # Run planner (mixed-initiative)
    out = plan_turn(speech, sess)

    save_session(call_sid, sess)

    # If lead is cliente final, responda com carinho e encerre após esclarecer
    if sess.get("data", {}).get("profile") == "final" and sess.get("auto_index", 0) >= 1:
        # Permite uma última fala humanizada e encerra
        resp = VoiceResponse()
        msg = (
            out["reply"] + " " +
            "A Glam vende só para profissionais credenciados, Patrícia. Indica nossa marca pra sua cabeleireira, tá? "
            "Se quiser, me chama no Instagram @glamhairbrand. Beijos!"
        )
        resp.say(ssml_pat(glam_tone(msg)), language="pt-BR", voice=VOICE_ID)
        resp.hangup()
        append_conv(call_sid, "assistant", msg)
        return Response(str(resp), mimetype="text/xml")

    # Normal flow: keep the conversation going
    reply_text = out["reply"]
    append_conv(call_sid, "assistant", reply_text)
    return speak_and_gather(reply_text, abs_url("/resposta"))

@app.route("/wrap_followup", methods=["POST"])
@require_twilio_auth
def wrap_followup():
    resp = VoiceResponse()
    resp.say(ssml_pat("Obrigada! Foi um prazer falar com você. Glamour é essencial — até breve!"),
             language="pt-BR", voice=VOICE_ID)
    resp.hangup()
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
            print(f"[CALL] {call_sid} resumo enviado.")
        except Exception as e:
            print(f"[SUMMARY ERROR] {e}")
        finally:
            clear_session(call_sid)
            print(f"[CALL] {call_sid} finalizada. Memória limpa.")

    return ("", 204)

# === OpenAI summary ===
PAT_SYSTEM_PROMPT = (
    "Você é a Pat Glam — consultora oficial da Glam Hair Brand. "
    "Fale no tom premium, acolhedor e objetivo. Não revele que é IA."
)


def summarize_history(history, call_sid: str, duration: str, from_number: str, to_number: str) -> str:
    try:
        convo_lines = []
        for m in history:
            who = "Cliente" if m.get("role") == "user" else "Pat"
            content = (m.get("content") or "").strip()
            if content:
                convo_lines.append(f"{who}: {content}")
        convo_text = "\n".join(convo_lines) if convo_lines else "(sem histórico)"

        prompt = f'''Você é uma assistente de CRM da Glam Hair Brand.
A seguir está a conversa entre a Pat Glam (assistente) e um contato.

Produza um resumo PROFISSIONAL em Markdown com as seções:
- **Perfil do contato** (profissional da beleza ou cliente final? inferir cidade se citada)
- **Assunto principal** (em 1-2 linhas)
- **Sinais de interesse e objeções**
- **Oportunidades de venda** (bullets curtos)
- **Próximos passos recomendados** (3 bullets práticos)
- **Frases-chave do lead** (entre aspas)
- **Tags** (formato: #fitaAdesiva #curso #profissional ...)

Cabeçalho: CallSid: {call_sid} | Duração: {duration or '—'}s | De: {from_number or '—'} | Para: {to_number or '—'}

Conversa:
{convo_text}
'''
        c = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content": "Você resume conversas comerciais para CRM com clareza e objetividade."},
                {"role":"user","content": prompt},
            ],
            temperature=0.4,
            max_tokens=500,
        )
        summary = (c.choices[0].message.content or "").strip()
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

# === Healthcheck ===
@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
