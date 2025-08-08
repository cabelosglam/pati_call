
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

# Optional Redis (fallback to in-memory)
try:
    import redis  # type: ignore
except Exception:
    redis = None

load_dotenv()

app = Flask(__name__)
app.config["PREFERRED_URL_SCHEME"] = "https"

# === SSML helper for more natural Google TTS (Aoede) ===
def ssml_pat(text: str) -> str:
    '''
    Envolve o texto em SSML com prosódia suave e corrige pronúncias.
    - rate levemente mais lento e pitch moderado para soar natural.
    - "Pat Glam" pronunciado como "Pati Glam".
    - substitui @ por "arroba" para não soletrar.
    '''
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

# WhatsApp autosend (deixe false por enquanto; ativaremos depois)
WHATSAPP_AUTOSEND = os.environ.get("WHATSAPP_AUTOSEND", "false").lower() == "true"
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # e.g., whatsapp:+14155238886

# === HELPERS ===
def abs_url(path: str) -> str:
    if not PUBLIC_BASE_URL:
        print("[WARN] PUBLIC_BASE_URL not set. Twilio won't reach your local server. "
              "Set PUBLIC_BASE_URL to your ngrok/Render domain.")
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
        return json.loads(raw) if raw else {"state":"classify","data":{}, "wa_sent": False}
    except Exception as e:
        print(f"[STORE ERROR] get_session: {e}")
        return {"state":"classify","data":{}, "wa_sent": False}

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
    if not t.startswith("@") and "@" in t:
        # already has @ somewhere
        pass
    elif not t.startswith("@"):
        t = "@" + t
    return t

# === Tone with GPT (optional) ===
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

# === Dialogue State Machine ===
def first_prompt():
    return ("Você é profissional da beleza ou uma Patricia Final curiosa? "
            "Se preferir, tecle 1 para profissional, ou 2 para cliente final.")

def ask_city():
    return "Atende em qual cidade, amor? Assim eu anoto com glitter dourado aqui."

def ask_experience():
    return "E você já trabalha com extensões? Qual método usa hoje no salão?"

def ask_instagram():
    return "Me passa o arroba do Instagram do salão ou o seu, por favor."

def ask_whatsapp():
    return ("Posso pedir para nossa equipe te chamar no WhatsApp para credenciar, "
            "enviar catálogo e detalhes da Masterclass? Diga 'sim' ou 'não'. "
            "Se preferir, tecle 1 para sim, 2 para não.")

def wrap_up(data: dict):
    city = data.get("city") or "sua cidade"
    exp = data.get("experience") or "seu método"
    insta = data.get("instagram") or "seu Instagram"
    thanks = (f"Perfeito, Patrícia poderosa! Anotei: cidade {city}, experiência {exp}, Insta {insta}. "
              "Glamour é essencial — nos vemos em breve! Posso te ajudar em mais alguma coisa?")
    return thanks

def next_state(current: str, profile: str=None) -> str:
    if current == "classify":
        return "ask_city" if profile != "final" else "final_flow"
    if current == "ask_city":
        return "ask_experience"
    if current == "ask_experience":
        return "ask_instagram"
    if current == "ask_instagram":
        return "ask_whatsapp"
    if current == "ask_whatsapp":
        return "wrap"
    return "wrap"

def handle_final_flow() -> str:
    return ("Ah, então você é uma Patrícia Final — das que só aceitam o melhor, né? 💁‍♀️ "
            "A Glam vende apenas para profissionais credenciados. Indica nosso método para sua cabeleireira "
            "e acompanha a gente no Instagram pra mais dicas e brilho!")

def speak_and_gather(text: str, action_url: str) -> Response:
    resp = VoiceResponse()
    gather = Gather(
        input="speech dtmf",
        action=action_url,
        method="POST",
        language="pt-BR",
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
    gather.say(ssml_pat(glam_tone(text)), language="pt-BR")
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
    # inicia estado
    call_sid = request.form.get("CallSid", "unknown") or "unknown"
    sess = {"state": "classify", "data": {}, "wa_sent": False}
    save_session(call_sid, sess)
    append_conv(call_sid, "assistant", "Saudação inicial.")
    intro = ("Oiê! Aqui é a Pat Glam, da Glam Hair Brand.")
    resp = VoiceResponse()
    resp.say(ssml_pat(glam_tone(intro)), language="pt-BR")
    # primeira pergunta
    return speak_and_gather(first_prompt(), abs_url("/resposta"))

@app.route("/resposta", methods=["POST"])
@require_twilio_auth
def resposta():
    call_sid = request.form.get("CallSid", "unknown")
    speech = (request.form.get("SpeechResult", "") or "").strip()
    digits = (request.form.get("Digits", "") or "").strip()
    confidence = request.form.get("Confidence", "") or ""

    # mapear DTMF
    if digits == "1" and get_session(call_sid).get("state") in ["classify","ask_whatsapp"]:
        # classificar como pro ou consentir no WA, trataremos adiante
        pass
    elif digits == "2" and get_session(call_sid).get("state") in ["classify","ask_whatsapp"]:
        pass

    print(f"[DEBUG] CallSid={call_sid} | SpeechResult={speech!r} | Confidence={confidence} | Digits={digits!r}")

    # eco no histórico para o resumo
    if speech:
        append_conv(call_sid, "user", speech)

    sess = get_session(call_sid)
    state = sess.get("state", "classify")
    data = sess.get("data", {})

    # --- state handlers ---
    if state == "classify":
        profile = None
        t = (speech or "").lower()
        if digits == "1" or "profiss" in t or "cabeleireir" in t or "salão" in t:
            profile = "pro"
        elif digits == "2" or "cliente" in t or "final" in t:
            profile = "final"

        if profile is None:
            # repete a pergunta
            return speak_and_gather("Não peguei. Você é profissional (tecle 1) ou cliente final (tecle 2)?",
                                    abs_url("/resposta"))

        data["profile"] = profile
        sess["state"] = next_state("classify", profile)
        sess["data"] = data
        save_session(call_sid, sess)

        if profile == "final":
            msg = handle_final_flow()
            append_conv(call_sid, "assistant", msg)
            # fecha conversa educadamente
            resp = VoiceResponse()
            resp.say(ssml_pat(glam_tone(msg)), language="pt-BR")
            resp.say(ssml_pat("Obrigada pelo carinho! Se quiser, peça para sua cabeleireira falar com a Glam. "
                              "Beijos!"), language="pt-BR")
            resp.hangup()
            return Response(str(resp), mimetype="text/xml")

        # profissional
        return speak_and_gather(ask_city(), abs_url("/resposta"))

    elif state == "ask_city":
        if not speech:
            return speak_and_gather("Não captei a cidade. Diz pra mim qual é?", abs_url("/resposta"))
        data["city"] = speech
        sess["state"] = next_state("ask_city")
        sess["data"] = data
        save_session(call_sid, sess)
        return speak_and_gather(ask_experience(), abs_url("/resposta"))

    elif state == "ask_experience":
        if not speech:
            return speak_and_gather("Me conta rapidinho: você já trabalha com extensões? Qual método usa hoje?",
                                    abs_url("/resposta"))
        data["experience"] = speech
        sess["state"] = next_state("ask_experience")
        sess["data"] = data
        save_session(call_sid, sess)
        return speak_and_gather(ask_instagram(), abs_url("/resposta"))

    elif state == "ask_instagram":
        if not speech:
            return speak_and_gather("Passa o arroba do Instagram do salão ou o seu, por favor.",
                                    abs_url("/resposta"))
        data["instagram"] = looks_like_instagram(speech)
        sess["state"] = next_state("ask_instagram")
        sess["data"] = data
        save_session(call_sid, sess)
        return speak_and_gather(ask_whatsapp(), abs_url("/resposta"))

    elif state == "ask_whatsapp":
        consent = None
        if digits in ("1","2"):
            consent = (digits == "1")
        elif is_yes(speech):
            consent = True
        elif is_no(speech):
            consent = False

        if consent is None:
            return speak_and_gather("Só pra confirmar: posso pedir para a equipe te chamar no WhatsApp? "
                                    "Diga 'sim' ou 'não'. (1 para sim, 2 para não)",
                                    abs_url("/resposta"))

        data["wa_consent"] = consent
        sess["state"] = next_state("ask_whatsapp")
        sess["data"] = data
        save_session(call_sid, sess)

        if consent and WHATSAPP_AUTOSEND and TWILIO_WHATSAPP_FROM:
            try:
                to_number = request.form.get("From", "")
                if to_number and not to_number.startswith("whatsapp:"):
                    to_number = "whatsapp:" + to_number
                msg = ("Oiê! Aqui é a Pat Glam 💖 Amei nosso papo. "
                       "Já separei catálogo e infos da Masterclass pra te mandar com glitter! ✨")
                twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_number, body=msg)
                sess["wa_sent"] = True
                save_session(call_sid, sess)
                append_conv(call_sid, "assistant", "WhatsApp enviado ao final da ligação.")
            except Exception as e:
                print(f"[WA ERROR] {e}")

        # concluir
        msg = wrap_up(data)
        append_conv(call_sid, "assistant", msg)
        resp = VoiceResponse()
        resp.say(ssml_pat(glam_tone(msg)), language="pt-BR")
        # opcional: uma última coleta curta
        gather = Gather(
            input="speech dtmf",
            action=abs_url("/wrap_followup"),
            method="POST",
            language="pt-BR",
            timeout=6,
            speech_timeout="auto",
            speech_model="phone_call",
            action_on_empty_result=True,
            num_digits=1
        )
        gather.say(ssml_pat("Se precisar de algo agora, pode dizer. Se não, eu já vou te deixar brilhar!"),
                   language="pt-BR")
        resp.append(gather)
        resp.say(ssml_pat("Beijinhos e até já!"), language="pt-BR")
        return Response(str(resp), mimetype="text/xml")

    else:
        # default
        return speak_and_gather("Desculpa, me perdi no salto alto aqui. Vamos de novo? Você é profissional (1) ou cliente final (2)?",
                                abs_url("/resposta"))

@app.route("/wrap_followup", methods=["POST"])
@require_twilio_auth
def wrap_followup():
    # qualquer coisa aqui encerra com carinho
    resp = VoiceResponse()
    resp.say(ssml_pat("Obrigada! Foi um prazer falar com você. Glamour é essencial — até breve!"),
             language="pt-BR")
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

# === OpenAI fallback (for free-style responses; not heavily used here) ===
PAT_SYSTEM_PROMPT = (
    '''Você é a Pat Glam — a consultora virtual oficial da Glam Hair Brand.
Não é apenas uma atendente: é a Patrícia fundadora, mentora e alma fashionista da marca.
Fale com elegância, carisma e humor sofisticado.
Chame clientes finais de 'Patrícia' (carinhoso) e profissionais como 'Patrícia Extensionista',
'Patrícia Profissional' ou 'Pat Poderosa'.

Sempre repita de forma gentil o que a pessoa acabou de dizer, para confirmar.
Exemplo: "Você é de Goiânia, né?" ou "Então você usa fita adesiva, certo?".

REGRAS: nunca diga que é IA; nunca passe preços direto; sempre qualifique com gentileza;
se for cliente final, explique que vendemos só para profissionais, com carinho,
incentive a seguir a Glam no Instagram e indicar o método para a cabeleireira.

Use termos da marca: 'glamour é essencial', 'Patrícia poderosa', 'salão de sucesso',
'fita adesiva premium', 'linha Brasileira do Sul', 'cabelo russo original',
'extensão que entrega status', 'agenda cheia com Glam'.

Seu objetivo: gerar conexão, desejo e pertencimento, mantendo tom premium e acolhedor.'''

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
