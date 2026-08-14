"""
Liten alltid-igång webbserver som gör "30 dagars-utmaning"-checkboxarna klickbara på riktigt.

Läser samma cachade data som hub.py redan skrivit (raw_debug.json, historik.csv,
aktier_cache.json, utmaning_status.json) och bygger sidan med samma bygg_html()
som hub.py — men bockar sparas direkt via /bocka istället för att vänta på
nästa timkörning.

Kör så här:
    py server.py
Nås sedan på http://<din-tailscale-ip-eller-namn>:5000
"""

import json
import csv
import os
import subprocess
import sys

from flask import Flask, request, jsonify

import hub

app = Flask(__name__)
PROJEKTMAPP = os.path.dirname(os.path.abspath(__file__))


def läs_json(filnamn, standard):
    if os.path.exists(filnamn):
        with open(filnamn, "r", encoding="utf-8") as f:
            return json.load(f)
    return standard


def bygg_sida():
    garmin_data = läs_json("raw_debug.json", {"datum": "", "aktiviteter": [], "stats": {}})

    historik = []
    if os.path.exists(hub.HISTORIK_FIL):
        with open(hub.HISTORIK_FIL, newline="", encoding="utf-8") as f:
            historik = list(csv.DictReader(f))

    aktiedata = läs_json("aktier_cache.json", {})
    utmaning_data = läs_json(hub.UTMANING_FIL, {"start_datum": hub.UTMANING_START, "dagar": {}})

    return hub.bygg_html(garmin_data, aktiedata, historik, utmaning_data)


@app.route("/")
def index():
    return bygg_sida()


@app.route("/bocka", methods=["POST"])
def bocka():
    body = request.get_json(force=True)
    datum = body.get("datum")
    kategori = body.get("kategori")
    klar = bool(body.get("klar"))

    if not datum or not kategori:
        return jsonify({"fel": "datum och kategori krävs"}), 400

    data = läs_json(hub.UTMANING_FIL, {"start_datum": hub.UTMANING_START, "dagar": {}})
    dag = data["dagar"].get(datum, {})
    post = dag.get(kategori, {"text": ""})
    post["klar"] = klar
    dag[kategori] = post
    data["dagar"][datum] = dag
    hub.spara_utmaning_data(data)

    return jsonify({"ok": True})


@app.route("/kost", methods=["POST"])
def kost():
    body = request.get_json(force=True)
    datum = body.get("datum")
    fält = body.get("falt")
    värde = body.get("värde")

    if not datum or not fält:
        return jsonify({"fel": "datum och falt krävs"}), 400

    try:
        hub.spara_kost_fält(datum, fält, värde)
    except ValueError as e:
        return jsonify({"fel": str(e)}), 400

    return jsonify({"ok": True})


@app.route("/uppdatera", methods=["POST"])
def uppdatera():
    """Körs när man trycker på 'Uppdatera nu' — kör hub.py på riktigt (Garmin-inloggning,
    aktiekurser, AI-analys, allt) och blockerar tills det är klart så sidan kan laddas om
    med färsk data direkt."""
    try:
        resultat = subprocess.run(
            [sys.executable, "hub.py", "--tyst"],
            cwd=PROJEKTMAPP,
            capture_output=True,
            text=True,
            timeout=240,
        )
        if resultat.returncode != 0:
            return jsonify({"fel": "hub.py misslyckades", "detaljer": resultat.stderr[-1000:]}), 500
        return jsonify({"ok": True})
    except subprocess.TimeoutExpired:
        return jsonify({"fel": "Tog för lång tid (>240s), troligen Garmin-inloggningsproblem"}), 504
    except Exception as e:
        return jsonify({"fel": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
