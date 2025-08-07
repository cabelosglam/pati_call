
from flask import Flask, request, render_template, redirect, url_for
from twilio.rest import Client

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    status = None
    if request.method == "POST":
        telefone = request.form["telefone"]
        account_sid = "SEU_SID_TWILIO"
        auth_token = "SEU_AUTH_TOKEN"
        client = Client(account_sid, auth_token)

        call = client.calls.create(
            to=telefone,
            from_="SEU_NUMERO_TWILIO",
            url="https://pati-ligacoes.onrender.com/voice"
        )
        status = f"Ligação iniciada para {telefone}. SID: {call.sid}"
    return render_template("index.html", status=status)
