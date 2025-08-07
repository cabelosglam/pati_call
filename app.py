
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
import requests
import openai

app = Flask(__name__)

@app.route("/voice", methods=["GET", "POST"])
def voice():
    resp = VoiceResponse()
    resp.say("Olá, aqui é a Pat Glam da Glam Hair Brand! Estou ligando para saber se você é cabeleireiro ou deseja aprender sobre nosso método de fita adesiva.", voice='alice', language='pt-BR')
    resp.record(max_length=60, transcribe=False, recording_status_callback="/recording")
    resp.say("Obrigada, até mais!", voice='alice', language='pt-BR')
    return Response(str(resp), mimetype="text/xml")

@app.route("/recording", methods=["POST"])
def recording_callback():
    recording_url = request.form["RecordingUrl"] + ".mp3"

    # Baixar gravação
    audio = requests.get(recording_url).content
    with open("reuniao.mp3", "wb") as f:
        f.write(audio)

    # Transcrever com Whisper
    audio_file = open("reuniao.mp3", "rb")
    transcript = openai.Audio.transcribe("whisper-1", audio_file)

    # Enviar para GPT
    prompt = f"""Resuma a seguinte conversa da Pat Glam com um cliente. Destaque:
- Quem é o cliente (profissional ou não)
- Interesses ou dúvidas
- O que foi explicado sobre o método ou curso
- Objeções, elogios ou respostas importantes
- Próximos passos sugeridos

Conversa:
{transcript['text']}
"""

    resposta = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Você é uma assistente que resume reuniões comerciais em tópicos objetivos."},
            {"role": "user", "content": prompt}
        ]
    )

    resumo = resposta["choices"][0]["message"]["content"]

    with open("resumo.txt", "w", encoding="utf-8") as f:
        f.write(resumo)

    return "Resumo gerado com sucesso."
