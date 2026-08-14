"""
Min personliga hub: Garmin (utökad) + aktier + historik + dagsvariation -> en HTML-sida.

Sidan byggs och serveras live av server.py (http://<tailscale-ip-eller-namn>:5000) —
det här scriptet uppdaterar bara den underliggande datan (Garmin, aktier, utmaning) och
committar historiken till git. Körs via schemaläggning en gång i timmen:
    py hub.py
"""

import getpass
import datetime
import json
import csv
import math
import os
import sqlite3
import subprocess

from garminconnect import Garmin
import yfinance as yf
import requests

BRANSCHER = [
    ("Index", [
        ("^OMX", "OMX Stockholm", " p"),
    ]),
    ("Bank & Finans", [
        ("SEB-A.ST", "SEB", " kr"),
        ("SWED-A.ST", "Swedbank", " kr"),
        ("SHB-A.ST", "Handelsbanken", " kr"),
        ("NDA-SE.ST", "Nordea", " kr"),
    ]),
    ("Verkstad & Industri", [
        ("VOLV-B.ST", "Volvo", " kr"),
        ("ATCO-A.ST", "Atlas Copco", " kr"),
        ("SAND.ST", "Sandvik", " kr"),
    ]),
    ("Telekom", [
        ("ERIC-B.ST", "Ericsson", " kr"),
        ("TEL2-B.ST", "Tele2", " kr"),
    ]),
    ("Konsument & Retail", [
        ("HM-B.ST", "H&M", " kr"),
        ("AXFO.ST", "Axfood", " kr"),
    ]),
    ("Läkemedel & Hälsa", [
        ("AZN.ST", "AstraZeneca", " kr"),
    ]),
    ("Fastigheter", [
        ("CAST.ST", "Castellum", " kr"),
        ("FABG.ST", "Fabege", " kr"),
    ]),
    ("Tech & Gaming", [
        ("EVO.ST", "Evolution", " kr"),
        ("EMBRAC-B.ST", "Embracer Group", " kr"),
    ]),
    ("Snabbt växande (senaste året)", [
        ("SAVE.ST", "Nordnet", " kr"),
        ("ALFA.ST", "Alfa Laval", " kr"),
        ("STORY-B.ST", "Storytel", " kr"),
        ("SINCH.ST", "Sinch", " kr"),
        ("MIPS.ST", "Mips", " kr"),
    ]),
    ("Internationellt", [
        ("AAPL", "Apple", " USD"),
        ("TSLA", "Tesla", " USD"),
    ]),
]
TICKERS = [ticker for _, bolag in BRANSCHER for ticker, _, _ in bolag]
HISTORIK_FIL = "historik.csv"
KONTOFIL = "garmin_konto.json"
AI_NYCKEL_FIL = "ai_nyckel.json"
AI_CACHE_FIL = "ai_analys_cache.json"
AI_MODELL = "claude-haiku-4-5-20251001"

UTMANING_FIL = "utmaning_status.json"
UTMANING_START = "2026-08-01"
UTMANING_LÄNGD = 30
DAGLIGA_KATEGORIER = [
    ("tiktoks", "TikTok"),
    ("hub", "Hub-utveckling"),
    ("jobb_plugg", "Jobb/plugg"),
    ("stada", "Städa/fixa hemma"),
    ("fri", "Fri utmaning"),
]

STRONG_EXPORT_FIL = os.path.join("strong-export", "strong_workouts.csv")
TIKTOK_EXPORT_FIL = os.path.join("tiktok-export", "tiktok_analytics.csv")

MÅL_VAKEN = "05:00"
MÅL_GYM = "06:00"
MÅL_GYM_PASS_PER_VECKA = 5
MÅL_LÖP_PASS_PER_VECKA_MIN = 3
MÅL_TOTALT_PASS_PER_VECKA = 10

KOST_DB = "kost.db"
KOST_GAMLA_JSON_FIL = "kost_status.json"
KOST_MÅL_KCAL = 3000
KOST_MÅL_PROTEIN = 170
KOST_MÅL_FETT = 70
KOST_MÅL_KOLHYDRATER = round((KOST_MÅL_KCAL - KOST_MÅL_PROTEIN * 4 - KOST_MÅL_FETT * 9) / 4)


def hamta_sparade_uppgifter():
    if os.path.exists(KONTOFIL):
        with open(KONTOFIL, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def spara_uppgifter(email, password):
    with open(KONTOFIL, "w", encoding="utf-8") as f:
        json.dump({"email": email, "password": password}, f)


def säker(func, *args, **kwargs):
    """Kör en Garmin-funktion, men krascha inte om den misslyckas."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"  (kunde inte hämta {func.__name__}: {e})")
        return None


def hämta_rutt(client, activity_id, max_punkter=150):
    """Hämtar GPS-rutten för ett träningspass och gör den lagom stor för en webbsida."""
    if not activity_id:
        return None
    detaljer = client.get_activity_details(activity_id)
    polyline = (detaljer.get("geoPolylineDTO") or {}).get("polyline") or []
    punkter = [[p["lat"], p["lon"]] for p in polyline if p.get("lat") is not None and p.get("lon") is not None]
    return nedsampla(punkter, max_punkter) if punkter else None


def hamta_garmin_data():
    sparat = hamta_sparade_uppgifter()
    if sparat:
        print("Loggar in på Garmin Connect (sparade uppgifter)...")
        email = sparat["email"]
        password = sparat["password"]
    else:
        print("Loggar in på Garmin Connect...")
        email = input("Garmin-epost: ")
        password = getpass.getpass("Garmin-lösenord (syns inte när du skriver): ")
        svar = input(
            "Vill du spara uppgifterna lokalt så scriptet kan köras automatiskt "
            "utan att du behöver skriva in dem varje gång? (j/n): "
        )
        if svar.strip().lower() == "j":
            spara_uppgifter(email, password)
            print(f"Sparat i {KONTOFIL} (ligger bara på din egen dator).")

    client = Garmin(email, password)
    client.login()

    today = datetime.date.today().isoformat()

    print("Hämtar grunddata...")
    sleep_data = säker(client.get_sleep_data, today)
    stats = säker(client.get_stats, today)

    print("Hämtar träningspass...")
    activities = säker(client.get_activities, 0, 15)

    print("Hämtar GPS-rutter för löppass...")
    for a in (activities or [])[:5]:
        if (a.get("distance") or 0) > 0:
            rutt = säker(hämta_rutt, client, a.get("activityId"))
            if rutt:
                a["rutt"] = rutt

    print("Hämtar kroppsbatteri...")
    body_battery = säker(client.get_body_battery, today)

    print("Hämtar VO2 max...")
    max_metrics = säker(client.get_max_metrics, today)

    print("Hämtar HRV...")
    hrv = säker(client.get_hrv_data, today)

    print("Hämtar kroppssammansättning...")
    body_comp = säker(client.get_body_composition, today)

    print("Hämtar training readiness...")
    training_readiness = säker(client.get_training_readiness, today)

    print("Hämtar training status...")
    training_status = säker(client.get_training_status, today)

    print("Hämtar puls under dagen...")
    hr_metod = getattr(client, "get_heart_rates", None)
    puls_dagen = säker(hr_metod, today) if hr_metod else None

    print("Hämtar stress under dagen...")
    stress_metod = getattr(client, "get_all_day_stress", None) or getattr(client, "get_stress_data", None)
    stress_dagen = säker(stress_metod, today) if stress_metod else None

    all_data = {
        "datum": today,
        "sömn": sleep_data,
        "stats": stats,
        "aktiviteter": activities,
        "kroppsbatteri": body_battery,
        "vo2max": max_metrics,
        "hrv": hrv,
        "kroppssammansättning": body_comp,
        "training_readiness": training_readiness,
        "training_status": training_status,
        "puls_dagen": puls_dagen,
        "stress_dagen": stress_dagen,
    }

    with open("raw_debug.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2, default=str)

    return all_data


def hamta_aktiekurser(tickers):
    resultat = {}
    for symbol in tickers:
        t = yf.Ticker(symbol)
        hist = t.history(period="2d")
        if len(hist) >= 1:
            senaste = hist["Close"].iloc[-1]
            if len(hist) >= 2:
                foregaende = hist["Close"].iloc[-2]
                förändring = ((senaste - foregaende) / foregaende) * 100
            else:
                förändring = 0.0
            resultat[symbol] = {"pris": round(senaste, 2), "förändring_pct": round(förändring, 2)}
        else:
            resultat[symbol] = {"pris": None, "förändring_pct": None}
    return resultat


def hämta_sleep_score(sömn):
    """Garmins fältnamn för sleep score kan variera - vi testar flera kända varianter."""
    dto = {}
    try:
        dto = sömn.get("dailySleepDTO") or {}
    except Exception:
        pass

    kandidater = []
    try:
        kandidater.append(dto.get("sleepScores", {}).get("overall", {}).get("value"))
    except Exception:
        pass
    try:
        kandidater.append(dto.get("overallSleepScore"))
    except Exception:
        pass
    try:
        kandidater.append(sömn.get("overallSleepScore"))
    except Exception:
        pass
    try:
        kandidater.append(sömn.get("sleepScores", {}).get("overall", {}).get("value"))
    except Exception:
        pass

    for k in kandidater:
        if k is not None:
            return k
    return None


def hämta_training_readiness(data):
    """Plockar ut readiness-poäng och återhämtningstid. Fältnamn kan variera mellan Garmin-versioner."""
    post = None
    if isinstance(data, list) and data:
        post = data[0]
    elif isinstance(data, dict):
        post = data
    if not post:
        return None, None

    poäng = None
    for nyckel in ("score", "trainingReadinessScore", "readinessScore"):
        try:
            v = post.get(nyckel)
        except Exception:
            v = None
        if v is not None:
            poäng = v
            break

    recovery_tid = None
    for nyckel in ("recoveryTime", "recoveryTimeHours", "recoveryTimeInHours"):
        try:
            v = post.get(nyckel)
        except Exception:
            v = None
        if v is not None:
            recovery_tid = v
            break

    return poäng, recovery_tid


def hämta_training_status(data):
    """Plockar ut senaste träningsstatusen (t.ex. Productive, Maintaining, Recovery)."""
    if not data:
        return None
    try:
        senaste = data.get("mostRecentTrainingStatus") or {}
        enheter = senaste.get("latestTrainingStatusData") or {}
        for info in enheter.values():
            fras = info.get("trainingStatusFeedbackPhrase") or info.get("trainingStatusFeedbackPhraseKey")
            if fras:
                return fras.replace("_", " ").strip().capitalize()
            status = info.get("trainingStatus")
            if status:
                return str(status).replace("_", " ").strip().capitalize()
    except Exception:
        pass
    try:
        fras = data.get("trainingStatusFeedbackPhrase")
        if fras:
            return fras.replace("_", " ").strip().capitalize()
    except Exception:
        pass
    return None


def extrahera_nyckeltal(garmin):
    """Plockar ut de viktigaste siffrorna ur den råa Garmin-datan, på ett ställe."""
    stats = garmin.get("stats") or {}
    sömn = garmin.get("sömn") or {}
    aktiviteter = garmin.get("aktiviteter") or []
    kroppsbatteri = garmin.get("kroppsbatteri") or []
    vo2max = garmin.get("vo2max") or {}
    hrv = garmin.get("hrv") or {}
    kropp = garmin.get("kroppssammansättning") or {}

    vilopuls = stats.get("restingHeartRate")
    totala_steg = stats.get("totalSteps")
    stress = stats.get("averageStressLevel")
    total_kalorier = stats.get("totalKilocalories")
    aktiv_kalorier = stats.get("activeKilocalories")

    sömn_sekunder = None
    try:
        sömn_sekunder = sömn.get("dailySleepDTO", {}).get("sleepTimeSeconds")
    except Exception:
        pass
    somn_min = round(sömn_sekunder / 60) if sömn_sekunder else None
    sömn_tid = tid_kort(somn_min) if somn_min else None

    sleep_score = hämta_sleep_score(sömn)

    vo2_värde = None
    try:
        if isinstance(vo2max, dict):
            generic = vo2max.get("generic") or {}
            vo2_värde = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
    except Exception:
        pass
    if vo2_värde is None:
        try:
            for a in aktiviteter:
                if a.get("vO2MaxValue"):
                    vo2_värde = a.get("vO2MaxValue")
                    break
        except Exception:
            pass

    hrv_värde = None
    hrv_status = None
    try:
        summary = hrv.get("hrvSummary") or {}
        hrv_värde = summary.get("lastNightAvg")
        hrv_status = summary.get("status")
    except Exception:
        pass

    vikt = None
    kroppsfett = None
    try:
        avg = kropp.get("totalAverage") or {}
        if avg.get("weight"):
            vikt = round(avg["weight"] / 1000, 1)
        kroppsfett = avg.get("bodyFat")
    except Exception:
        pass

    batteri_värde = None
    try:
        if isinstance(kroppsbatteri, list) and len(kroppsbatteri) > 0:
            senaste_post = kroppsbatteri[0]
            punkter = senaste_post.get("bodyBatteryValuesArray") or []
            if punkter:
                batteri_värde = punkter[-1][1]
    except Exception:
        pass

    training_readiness, recovery_tid = hämta_training_readiness(garmin.get("training_readiness"))
    training_status = hämta_training_status(garmin.get("training_status"))

    return {
        "vilopuls": vilopuls,
        "steg": totala_steg,
        "stress": stress,
        "total_kalorier": total_kalorier,
        "aktiv_kalorier": aktiv_kalorier,
        "somn_min": somn_min,
        "somn_tid": sömn_tid,
        "sleep_score": sleep_score,
        "vo2max": vo2_värde,
        "hrv": hrv_värde,
        "hrv_status": hrv_status,
        "vikt": vikt,
        "kroppsfett": kroppsfett,
        "batteri": batteri_värde,
        "training_readiness": training_readiness,
        "recovery_tid": recovery_tid,
        "training_status": training_status,
    }


def ticker_kolumn(symbol):
    return "pris_" + symbol.replace("^", "").replace("-", "_").replace(".", "_")


def spara_historik(garmin, aktier):
    """Sparar dagens nyckeltal i historik.csv. Kör du samma dag igen skrivs raden över."""
    nyckeltal = extrahera_nyckeltal(garmin)

    ny_rad = {
        "datum": garmin["datum"],
        "somn_min": nyckeltal["somn_min"],
        "sleep_score": nyckeltal["sleep_score"],
        "vilopuls": nyckeltal["vilopuls"],
        "steg": nyckeltal["steg"],
        "stress": nyckeltal["stress"],
        "hrv": nyckeltal["hrv"],
        "batteri": nyckeltal["batteri"],
        "vo2max": nyckeltal["vo2max"],
        "total_kalorier": nyckeltal["total_kalorier"],
    }
    for symbol, info in aktier.items():
        ny_rad[ticker_kolumn(symbol)] = info["pris"]

    rader = []
    if os.path.exists(HISTORIK_FIL):
        with open(HISTORIK_FIL, newline="", encoding="utf-8") as f:
            rader = list(csv.DictReader(f))

    rader = [r for r in rader if r.get("datum") != ny_rad["datum"]]
    rader.append(ny_rad)

    alla_fält = []
    for r in rader:
        for key in r.keys():
            if key not in alla_fält:
                alla_fält.append(key)

    with open(HISTORIK_FIL, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=alla_fält)
        writer.writeheader()
        for r in rader:
            writer.writerow(r)

    return rader


def till_tal(x):
    """Försöker göra om något till ett flyttal, annars None."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kort_datum(text):
    """2026-07-29 -> 07-29. Kortare strängar (t.ex. HH:MM) lämnas orörda."""
    if text and len(text) >= 10 and text[4] == "-":
        return text[5:10]
    return text or ""


def intraday_serie(data, array_nyckel):
    """Plockar ut [(tidsstämpel_ms, värde), ...] ur Garmins dagsdata-strukturer.
    Garmin använder negativa specialkoder (t.ex. -1, -2) för "ingen mätning just nu"
    (under vissa aktiviteter) — de är inga riktiga värden och filtreras bort här."""
    if not data:
        return []
    try:
        punkter = data.get(array_nyckel) or []
        resultat = []
        for p in punkter:
            if p and len(p) >= 2 and p[1] is not None and p[1] >= 0:
                resultat.append((p[0], p[1]))
        return resultat
    except Exception:
        return []


def nedsampla(punkter, max_antal=50):
    """Minskar antalet punkter så diagrammet inte blir överbelamrat."""
    if len(punkter) <= max_antal:
        return punkter
    steg = len(punkter) / max_antal
    resultat = []
    i = 0.0
    while int(i) < len(punkter):
        resultat.append(punkter[int(i)])
        i += steg
    return resultat


def ms_till_klockslag(ms):
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M")
    except Exception:
        return ""


IKON_SVG = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Crect width='100' height='100' rx='22' fill='%230b0e14'/%3E"
    "%3Ctext x='50' y='68' font-size='55' text-anchor='middle' fill='%2322c55e' "
    "font-family='sans-serif' font-weight='bold'%3EM%3C/text%3E"
    "%3C/svg%3E"
)

MANIFEST_JSON = json.dumps({
    "name": "Min Hub",
    "short_name": "Min Hub",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b0e14",
    "theme_color": "#0b0e14",
    "icons": [{"src": IKON_SVG, "sizes": "100x100", "type": "image/svg+xml"}],
}, ensure_ascii=False)

NAMN = "Markus"

AKTIE_NAMN = {
    ticker: (namn, bransch, enhet)
    for bransch, bolag in BRANSCHER
    for ticker, namn, enhet in bolag
}


def hälsning():
    timme = datetime.datetime.now().hour
    if timme < 10:
        del_av_dagen = "God morgon"
    elif timme < 17:
        del_av_dagen = "God eftermiddag"
    else:
        del_av_dagen = "God kväll"
    return f"{del_av_dagen}, {NAMN}."


def ikon_för_aktivitet(namn):
    namn = (namn or "").lower()
    if "löp" in namn or "run" in namn:
        return "🏃"
    if "styrka" in namn or "strength" in namn:
        return "🏋️"
    if "cyk" in namn or "bike" in namn:
        return "🚴"
    if "sim" in namn or "swim" in namn:
        return "🏊"
    if "yoga" in namn:
        return "🧘"
    if "vandr" in namn or "hik" in namn:
        return "🥾"
    return "⚡"


def tal_sep(n):
    """Formaterar tal med mellanslag som tusentalsavgränsare, t.ex. 12345 -> '12 345'."""
    if n is None:
        return "–"
    return f"{n:,}".replace(",", " ")


def kort_datumtid(text):
    """'2026-07-29 10:15:00' -> '07-29 10:15'."""
    if text and len(text) >= 16:
        return text[5:16]
    return text or ""


def tid_kort(minuter):
    """425 (minuter) -> '7t 5m'."""
    if minuter is None:
        return None
    minuter = round(minuter)
    return f"{minuter // 60}t {minuter % 60}m"


def format_tempo(duration_sek, distans_m):
    """714.9 sekunder, 2209.7 meter -> '5:24 /km'."""
    if not duration_sek or not distans_m:
        return None
    distans_km = distans_m / 1000
    if distans_km <= 0:
        return None
    sek_per_km = duration_sek / distans_km
    minuter = int(sek_per_km // 60)
    sekunder = int(round(sek_per_km % 60))
    if sekunder == 60:
        minuter += 1
        sekunder = 0
    return f"{minuter}:{sekunder:02d} /km"


def format_varaktighet(sekunder):
    """714.9 -> '12 min'. 4500 -> '1t 15m'."""
    if not sekunder:
        return None
    total_min = round(sekunder / 60)
    if total_min < 60:
        return f"{total_min} min"
    return f"{total_min // 60}t {total_min % 60}m"


def läs_strong_pass():
    """Läser in Strong-appens CSV-export (om den finns) och grupperar sets per träningsdatum."""
    if not os.path.exists(STRONG_EXPORT_FIL):
        return {}
    pass_per_datum = {}
    try:
        with open(STRONG_EXPORT_FIL, newline="", encoding="utf-8") as f:
            for rad in csv.DictReader(f):
                datum = (rad.get("Date") or "")[:10]
                if datum:
                    pass_per_datum.setdefault(datum, []).append(rad)
    except Exception as e:
        print(f"  (kunde inte läsa Strong-export: {e})")
        return {}
    return pass_per_datum


def bygg_strong_html(sets_för_dagen):
    """Bygger en övning-för-övning-sammanfattning (vikt×reps per set) för ett styrkepass."""
    övningar = []
    senast_sedd = {}
    for rad in sets_för_dagen:
        namn = rad.get("Exercise Name") or "Okänd övning"
        if namn not in senast_sedd:
            senast_sedd[namn] = len(övningar)
            övningar.append((namn, []))
        övningar[senast_sedd[namn]][1].append(rad)

    rader = ""
    for namn, sets in övningar:
        set_texter = []
        for s in sets:
            vikt = till_tal(s.get("Weight"))
            reps = till_tal(s.get("Reps"))
            if not reps:
                continue
            märke = "W: " if s.get("Set Order") == "W" else ""
            if vikt:
                set_texter.append(f"{märke}{vikt:g} kg × {int(reps)}")
            else:
                set_texter.append(f"{märke}{int(reps)} reps")
        rader += f"""
        <div class="strong-ovning">
            <div class="strong-namn">{namn}</div>
            <div class="strong-sets">{' · '.join(set_texter) if set_texter else '–'}</div>
        </div>"""
    return rader


def hämta_vaknade(sömn):
    """Plockar ut vaknatid (HH:MM) ur dagens sömndata."""
    try:
        ts = (sömn.get("dailySleepDTO") or {}).get("sleepEndTimestampLocal")
        if ts:
            return datetime.datetime.fromtimestamp(ts / 1000).strftime("%H:%M")
    except Exception:
        pass
    return None


def dagens_första_pass(aktiviteter, datum):
    """HH:MM för det tidigaste träningspasset som loggats idag."""
    kandidater = [a for a in (aktiviteter or []) if (a.get("startTimeLocal") or "").startswith(datum)]
    if not kandidater:
        return None
    kandidater.sort(key=lambda a: a.get("startTimeLocal", ""))
    return kandidater[0].get("startTimeLocal", "")[11:16] or None


def veckans_träning(aktiviteter, dagar_bak=7):
    """Räknar antal gym- och löppass de senaste `dagar_bak` dagarna."""
    gräns = datetime.datetime.now() - datetime.timedelta(days=dagar_bak)
    gym = 0
    löp = 0
    for a in aktiviteter or []:
        try:
            start = datetime.datetime.strptime(a.get("startTimeLocal", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if start < gräns:
            continue
        typ = (a.get("activityType") or {}).get("typeKey", "")
        if "strength" in typ:
            gym += 1
        elif "running" in typ:
            löp += 1
    return {"gym": gym, "löp": löp, "totalt": gym + löp}


def bygg_morgonrutin_html(garmin):
    aktiviteter = garmin.get("aktiviteter") or []
    vaknade = hämta_vaknade(garmin.get("sömn") or {})
    första_pass = dagens_första_pass(aktiviteter, garmin["datum"])
    vecka = veckans_träning(aktiviteter)

    vaken_bra = vaknade is not None and vaknade <= "05:15"
    gym_bra = första_pass is not None and första_pass <= "06:15"
    totalt_procent = max(0, min(100, round(vecka["totalt"] / MÅL_TOTALT_PASS_PER_VECKA * 100)))

    return f"""
    <div class="card" style="margin-bottom:1rem;">
        <h2>Morgonrutin &amp; veckans träning <span class="tile-sub">mål: vakna {MÅL_VAKEN} · gym {MÅL_GYM}</span></h2>
        <div class="row-2">
            {tile("Vaknade", vaknade, accent="var(--green)" if vaken_bra else "var(--orange)")}
            {tile("Första passet idag", första_pass, accent="var(--green)" if gym_bra else "var(--orange)")}
        </div>
        <div class="tile-sub" style="margin-top:0.9rem;">
            Veckans pass: <strong style="color:var(--text)">{vecka['totalt']}/{MÅL_TOTALT_PASS_PER_VECKA}</strong>
            ({vecka['gym']} gym, {vecka['löp']} löp — mål {MÅL_GYM_PASS_PER_VECKA} gym + {MÅL_LÖP_PASS_PER_VECKA_MIN}+ löp)
        </div>
        <div class="progress" style="margin-top:0.5rem;">
            <div class="progress-fill" style="width:{totalt_procent}%"></div>
        </div>
    </div>"""


def _kost_db():
    """Öppnar (och skapar vid behov) den lättviktiga SQLite-databasen för kostloggen.
    Migrerar automatiskt in gammal data från kost_status.json en gång, om den finns."""
    ny_db = not os.path.exists(KOST_DB)
    conn = sqlite3.connect(KOST_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kost (
            datum TEXT PRIMARY KEY,
            kcal REAL,
            protein REAL,
            fett REAL,
            kolhydrater REAL
        )
    """)
    if ny_db and os.path.exists(KOST_GAMLA_JSON_FIL):
        try:
            with open(KOST_GAMLA_JSON_FIL, "r", encoding="utf-8") as f:
                gammal_data = json.load(f)
            for datum, fält in gammal_data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO kost (datum, kcal, protein, fett, kolhydrater) VALUES (?, ?, ?, ?, ?)",
                    (datum, fält.get("kcal"), fält.get("protein"), fält.get("fett"), fält.get("kolhydrater")),
                )
            conn.commit()
            os.rename(KOST_GAMLA_JSON_FIL, KOST_GAMLA_JSON_FIL + ".migrerad")
            print(f"Migrerade gammal kostdata från {KOST_GAMLA_JSON_FIL} till {KOST_DB}.")
        except Exception as e:
            print(f"  (kunde inte migrera gammal kostdata: {e})")
    return conn


def hämta_kost_data():
    """Returnerar all kostdata som {datum: {kcal, protein, fett, kolhydrater}}."""
    conn = _kost_db()
    try:
        rader = conn.execute("SELECT datum, kcal, protein, fett, kolhydrater FROM kost").fetchall()
    finally:
        conn.close()
    return {
        datum: {"kcal": kcal, "protein": protein, "fett": fett, "kolhydrater": kolhydrater}
        for datum, kcal, protein, fett, kolhydrater in rader
    }


def spara_kost_fält(datum, fält, värde):
    """Sparar (skapar eller uppdaterar) ett enskilt kostfält för ett datum."""
    if fält not in ("kcal", "protein", "fett", "kolhydrater"):
        raise ValueError(f"okänt kostfält: {fält}")
    conn = _kost_db()
    try:
        conn.execute(f"INSERT INTO kost (datum, {fält}) VALUES (?, ?) "
                     f"ON CONFLICT(datum) DO UPDATE SET {fält} = excluded.{fält}", (datum, värde))
        conn.commit()
    finally:
        conn.close()


def kost_rad(etikett, fält, värde, mål, enhet, färg):
    procent = max(0, min(100, round((värde or 0) / mål * 100))) if mål else 0
    visat_värde = värde if värde is not None else ""
    return f"""
    <div class="kost-rad" data-falt="{fält}">
        <div class="kost-topp">
            <span class="kost-etikett">{etikett}</span>
            <span class="kost-mal">/ {mål}{enhet}</span>
        </div>
        <input type="number" class="kost-input" value="{visat_värde}" placeholder="0"
               data-falt="{fält}" onchange="sparaKost(this)">
        <div class="progress" style="margin-top:0.4rem;">
            <div class="progress-fill" style="width:{procent}%; background:{färg};"></div>
        </div>
    </div>"""


def bygg_kost_html(idag_str):
    dagens = hämta_kost_data().get(idag_str, {})

    rader = (
        kost_rad("Kalorier", "kcal", dagens.get("kcal"), KOST_MÅL_KCAL, " kcal", "var(--orange)")
        + kost_rad("Protein", "protein", dagens.get("protein"), KOST_MÅL_PROTEIN, " g", "var(--red)")
        + kost_rad("Fett", "fett", dagens.get("fett"), KOST_MÅL_FETT, " g", "var(--purple)")
        + kost_rad("Kolhydrater", "kolhydrater", dagens.get("kolhydrater"), KOST_MÅL_KOLHYDRATER, " g", "var(--teal)")
    )

    return f"""
    <div class="card" style="margin-bottom:1rem;">
        <h2>Kost idag <span class="tile-sub">mål: {KOST_MÅL_KCAL} kcal · {KOST_MÅL_PROTEIN}g protein · {KOST_MÅL_FETT}g fett · {KOST_MÅL_KOLHYDRATER}g kolhydrater</span></h2>
        <div class="kost-grid">
            {rader}
        </div>
    </div>
    <script>
    function sparaKost(input) {{
        var rad = input.closest('.kost-rad');
        var värde = parseFloat(input.value) || 0;
        rad.classList.add('sparar');
        fetch('/kost', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{datum: '{idag_str}', falt: input.dataset.falt, värde: värde}})
        }}).then(function(svar) {{
            if (!svar.ok) throw new Error('fel');
            rad.classList.remove('sparar');
        }}).catch(function() {{
            rad.classList.remove('sparar');
            alert('Kunde inte spara. Öppna sidan via serverlänken (Tailscale) för att kunna logga.');
        }});
    }}
    </script>"""


def bygg_kost_historik_html():
    data = hämta_kost_data()
    datum_lista = sorted(data.keys())[-7:]

    kcal_värden = [till_tal((data.get(d) or {}).get("kcal")) for d in datum_lista]
    protein_värden = [till_tal((data.get(d) or {}).get("protein")) for d in datum_lista]
    fett_värden = [till_tal((data.get(d) or {}).get("fett")) for d in datum_lista]
    kolhydrater_värden = [till_tal((data.get(d) or {}).get("kolhydrater")) for d in datum_lista]

    return f"""
    <div class="card" style="margin-bottom:1rem;">
        <h2>Kost — veckosammanfattning <span class="tile-sub">senaste {len(datum_lista)} dagarna</span></h2>
        <div class="chart-grid">
            {svg_linjediagram("Kalorier", datum_lista, kcal_värden, " kcal", y_min=1, y_max=4000, färg="#f59e0b", fyllning=True)}
            {svg_linjediagram("Protein", datum_lista, protein_värden, " g", y_min=1, y_max=200, färg="#f43f5e", fyllning=True)}
            {svg_linjediagram("Fett", datum_lista, fett_värden, " g", y_min=1, y_max=80, färg="#a78bfa", fyllning=True)}
            {svg_linjediagram("Kolhydrater", datum_lista, kolhydrater_värden, " g", y_min=1, y_max=450, färg="#06b6d4", fyllning=True)}
        </div>
    </div>"""


def bygg_kaloribalans_html():
    """Kalorier in (loggat i Kost idag) minus kalorier ut (Garmins uppmätta förbrukning),
    per dag. Positivt = överskott, negativt = underskott."""
    kost_data = hämta_kost_data()
    datum_lista = sorted(kost_data.keys())[-7:]

    historik_per_datum = {}
    if os.path.exists(HISTORIK_FIL):
        with open(HISTORIK_FIL, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                historik_per_datum[r.get("datum")] = r

    balans_värden = []
    for d in datum_lista:
        in_kcal = till_tal((kost_data.get(d) or {}).get("kcal"))
        ut_kcal = till_tal((historik_per_datum.get(d) or {}).get("total_kalorier"))
        balans_värden.append(in_kcal - ut_kcal if in_kcal is not None and ut_kcal is not None else None)

    return f"""
    <div class="card" style="margin-bottom:1rem;">
        <h2>Kalorier in vs ut <span class="tile-sub">balans = in − ut</span></h2>
        {svg_linjediagram("Kaloribalans", datum_lista, balans_värden, " kcal", färg="#f59e0b", fyllning=True)}
    </div>"""


def snyggt_steg(min_v, max_v):
    """Räknar ut ett runt, läsbart skalsteg (1/2/5/10 × 10-potens) för ~4 rutnätslinjer."""
    rå_steg = (max_v - min_v) / 4 or 1
    magnitud = 10 ** math.floor(math.log10(rå_steg))
    normaliserat = rå_steg / magnitud
    snyggt = 1 if normaliserat <= 1 else 2 if normaliserat <= 2 else 5 if normaliserat <= 5 else 10
    return snyggt * magnitud


def svg_linjediagram(titel, datum_lista, värden, enhet="", y_min=None, y_max=None, y_steg=None,
                      färg="#3b82f6", fyllning=False, höjd=150, senaste_text=None):
    """Bygger ett SVG-linjediagram (mörkt tema) med etiketter på x-axeln och valfri fast y-skala."""
    bredd = 520
    pad_v, pad_h, pad_topp, pad_botten = 34, 10, 12, 24

    punktlista = [(i, v) for i, v in enumerate(värden) if v is not None]
    if len(punktlista) < 2:
        return (
            f'<div class="chart-box"><div class="chart-title">{titel}</div>'
            f'<div class="chart-empty">Behöver fler mätpunkter innan en graf syns här</div></div>'
        )

    n = max(len(värden) - 1, 1)

    if y_min is None or y_max is None:
        ys_alla = [v for _, v in punktlista]
        min_y, max_y = min(ys_alla), max(ys_alla)
        if min_y == max_y:
            min_y -= 1
            max_y += 1
        else:
            # Lägg på luft runt datan så små svängningar inte ser ut som stora berg
            padding = (max_y - min_y) * 0.3
            min_y -= padding
            max_y += padding
    else:
        min_y, max_y = y_min, y_max

    def x_pos(i):
        return pad_v + (i / n) * (bredd - pad_v - pad_h)

    def y_pos(v):
        v = max(min(v, max_y), min_y)
        return pad_topp + (1 - (v - min_y) / (max_y - min_y)) * (höjd - pad_topp - pad_botten)

    punkter_str = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in punktlista)
    punkt_cirklar = "".join(
        f'<circle cx="{x_pos(i):.1f}" cy="{y_pos(v):.1f}" r="3" fill="{färg}"/>'
        for i, v in punktlista
    )

    fyllnings_yta = ""
    if fyllning:
        bas_y = y_pos(min_y)
        första_x = x_pos(punktlista[0][0])
        sista_x = x_pos(punktlista[-1][0])
        fyllnings_yta = (
            f'<polygon points="{första_x:.1f},{bas_y:.1f} {punkter_str} {sista_x:.1f},{bas_y:.1f}" '
            f'fill="{färg}" fill-opacity="0.18"/>'
        )

    y_steg_faktisk = y_steg or snyggt_steg(min_y, max_y)

    gridlinjer = ""
    v = min_y
    while v <= max_y + 0.0001:
        y = y_pos(v)
        gridlinjer += (
            f'<line x1="{pad_v}" y1="{y:.1f}" x2="{bredd - pad_h}" y2="{y:.1f}" '
            f'stroke="rgba(255,255,255,0.07)" stroke-width="1"/>'
            f'<text x="{pad_v - 6}" y="{y + 3:.1f}" font-size="9" fill="#5b6472" '
            f'text-anchor="end">{int(round(v))}{enhet}</text>'
        )
        v += y_steg_faktisk

    antal = len(värden)
    steg_label = max(1, round(antal / 7))
    x_etiketter = ""
    for i in range(antal):
        if i % steg_label == 0 or i == antal - 1:
            x_etiketter += (
                f'<text x="{x_pos(i):.1f}" y="{höjd - pad_botten + 16}" font-size="9" '
                f'fill="#5b6472" text-anchor="middle">{kort_datum(datum_lista[i])}</text>'
            )

    senaste = punktlista[-1][1]
    visning_senaste = senaste_text if senaste_text is not None else f"{senaste}{enhet}"

    return f"""
    <div class="chart-box">
        <div class="chart-title">{titel} <span class="chart-latest">{visning_senaste}</span></div>
        <svg viewBox="0 0 {bredd} {höjd}" width="100%" height="{höjd}">
            {gridlinjer}
            {fyllnings_yta}
            <polyline points="{punkter_str}" fill="none" stroke="{färg}" stroke-width="2.5"/>
            {punkt_cirklar}
            {x_etiketter}
        </svg>
    </div>"""


def svg_dagsdiagram(titel, punkter, enhet="", y_min=None, y_max=None, y_steg=None, färg="#3b82f6", höjd=170):
    """Linjediagram för ett helt dygn (00.00–23.59). Placerar varje punkt efter dess
    faktiska klockslag istället för sitt index, så grafen alltid täcker hela dagen
    jämnt — annars blir det snett om mätpunkterna inte kommer med jämna mellanrum."""
    bredd = 520
    pad_v, pad_h, pad_topp, pad_botten = 34, 10, 12, 34
    dygn_min = 24 * 60

    giltiga = [(t, v) for t, v in punkter if v is not None]
    if len(giltiga) < 2:
        return (
            f'<div class="chart-box"><div class="chart-title">{titel}</div>'
            f'<div class="chart-empty">Behöver fler mätpunkter innan en graf syns här</div></div>'
        )

    def minut_på_dagen(ms):
        dt = datetime.datetime.fromtimestamp(ms / 1000)
        return dt.hour * 60 + dt.minute + dt.second / 60

    ys_alla = [v for _, v in giltiga]
    if y_min is None or y_max is None:
        min_y, max_y = min(ys_alla), max(ys_alla)
        if min_y == max_y:
            min_y -= 1
            max_y += 1
        else:
            padding = (max_y - min_y) * 0.3
            min_y -= padding
            max_y += padding
    else:
        min_y, max_y = y_min, y_max

    def x_pos(ms):
        return pad_v + (minut_på_dagen(ms) / dygn_min) * (bredd - pad_v - pad_h)

    def y_pos(v):
        v = max(min(v, max_y), min_y)
        return pad_topp + (1 - (v - min_y) / (max_y - min_y)) * (höjd - pad_topp - pad_botten)

    punkter_str = " ".join(f"{x_pos(t):.1f},{y_pos(v):.1f}" for t, v in giltiga)

    y_steg_faktisk = y_steg or snyggt_steg(min_y, max_y)
    gridlinjer = ""
    v = min_y
    while v <= max_y + 0.0001:
        y = y_pos(v)
        gridlinjer += (
            f'<line x1="{pad_v}" y1="{y:.1f}" x2="{bredd - pad_h}" y2="{y:.1f}" '
            f'stroke="rgba(255,255,255,0.06)" stroke-width="1"/>'
            f'<text x="{pad_v - 6}" y="{y + 3:.1f}" font-size="9" fill="#5b6472" '
            f'text-anchor="end">{int(round(v))}</text>'
        )
        v += y_steg_faktisk

    x_etiketter = ""
    for timme in range(24):
        x = pad_v + (timme * 60 / dygn_min) * (bredd - pad_v - pad_h)
        y_bas = höjd - pad_botten + 8
        x_etiketter += (
            f'<text x="{x:.1f}" y="{y_bas}" font-size="8" fill="#5b6472" text-anchor="end" '
            f'transform="rotate(-45 {x:.1f} {y_bas})">{timme:02d}.00</text>'
        )

    senaste = giltiga[-1][1]

    return f"""
    <div class="chart-box">
        <div class="chart-title">{titel} <span class="chart-latest">{senaste}{enhet}</span></div>
        <svg viewBox="0 0 {bredd} {höjd}" width="100%" height="{höjd}">
            {gridlinjer}
            <polyline points="{punkter_str}" fill="none" stroke="{färg}" stroke-width="2"/>
            {x_etiketter}
        </svg>
    </div>"""


def svg_dubbeldiagram(titel, datum_lista, värden1, etikett1, färg1, värden2, etikett2, färg2,
                       enhet1="", enhet2="", höjd=150, senaste_text1=None, senaste_text2=None,
                       y_min1=None, y_max1=None, y_min2=None, y_max2=None,
                       y_steg1=None, y_steg2=None):
    """Två linjer i samma diagram (t.ex. sömn och sleep score) för att se hur de följs åt.
    Varje serie skalas mot sitt eget min/max (eller en angiven fast skala) eftersom de kan ha olika enheter."""
    bredd = 520
    pad_v, pad_h, pad_topp, pad_botten = 34, 34, 26, 24

    punkter1 = [(i, v) for i, v in enumerate(värden1) if v is not None]
    punkter2 = [(i, v) for i, v in enumerate(värden2) if v is not None]
    if len(punkter1) < 2 or len(punkter2) < 2:
        return (
            f'<div class="chart-box"><div class="chart-title">{titel}</div>'
            f'<div class="chart-empty">Behöver fler mätpunkter innan en graf syns här</div></div>'
        )

    n = max(len(värden1) - 1, 1)

    def skala(punkter, fast_min, fast_max):
        if fast_min is not None and fast_max is not None:
            return fast_min, fast_max
        ys = [v for _, v in punkter]
        mn, mx = min(ys), max(ys)
        if mn == mx:
            mn -= 1
            mx += 1
        return mn, mx

    min1, max1 = skala(punkter1, y_min1, y_max1)
    min2, max2 = skala(punkter2, y_min2, y_max2)

    def x_pos(i):
        return pad_v + (i / n) * (bredd - pad_v - pad_h)

    def y_pos(v, mn, mx):
        v = max(min(v, mx), mn)
        return pad_topp + (1 - (v - mn) / (mx - mn)) * (höjd - pad_topp - pad_botten)

    linje1 = " ".join(f"{x_pos(i):.1f},{y_pos(v, min1, max1):.1f}" for i, v in punkter1)
    linje2 = " ".join(f"{x_pos(i):.1f},{y_pos(v, min2, max2):.1f}" for i, v in punkter2)
    cirklar1 = "".join(
        f'<circle cx="{x_pos(i):.1f}" cy="{y_pos(v, min1, max1):.1f}" r="3" fill="{färg1}"/>'
        for i, v in punkter1
    )
    cirklar2 = "".join(
        f'<circle cx="{x_pos(i):.1f}" cy="{y_pos(v, min2, max2):.1f}" r="3" fill="{färg2}"/>'
        for i, v in punkter2
    )

    def y_axel(mn, mx, färg, vänster, enhet_axel, fast_steg=None):
        steg = fast_steg or snyggt_steg(mn, mx)
        etiketter = ""
        v = mn
        while v <= mx + 0.0001:
            y = y_pos(v, mn, mx)
            x = pad_v - 6 if vänster else bredd - pad_h + 6
            ankare = "end" if vänster else "start"
            etiketter += (
                f'<line x1="{pad_v}" y1="{y:.1f}" x2="{bredd - pad_h}" y2="{y:.1f}" '
                f'stroke="rgba(255,255,255,0.06)" stroke-width="1"/>'
                f'<text x="{x}" y="{y + 3:.1f}" font-size="9" fill="{färg}" '
                f'text-anchor="{ankare}">{int(round(v))}{enhet_axel}</text>'
            )
            v += steg
        return etiketter

    y_axel1 = y_axel(min1, max1, färg1, True, enhet1, y_steg1)
    y_axel2 = y_axel(min2, max2, färg2, False, enhet2, y_steg2)

    antal = len(värden1)
    steg_label = max(1, round(antal / 7))
    x_etiketter = ""
    for i in range(antal):
        if i % steg_label == 0 or i == antal - 1:
            x_etiketter += (
                f'<text x="{x_pos(i):.1f}" y="{höjd - pad_botten + 16}" font-size="9" '
                f'fill="#5b6472" text-anchor="middle">{kort_datum(datum_lista[i])}</text>'
            )

    senaste1 = punkter1[-1][1]
    senaste2 = punkter2[-1][1]
    visning1 = senaste_text1 if senaste_text1 is not None else f"{senaste1}{enhet1}"
    visning2 = senaste_text2 if senaste_text2 is not None else f"{senaste2}{enhet2}"

    return f"""
    <div class="chart-box">
        <div class="chart-title">
            <span>{titel}</span>
            <span>
                <span style="color:{färg1}">● {etikett1} {visning1}</span>
                &nbsp;&nbsp;
                <span style="color:{färg2}">● {etikett2} {visning2}</span>
            </span>
        </div>
        <svg viewBox="0 0 {bredd} {höjd}" width="100%" height="{höjd}">
            {y_axel1}
            {y_axel2}
            <polyline points="{linje1}" fill="none" stroke="{färg1}" stroke-width="2.5"/>
            <polyline points="{linje2}" fill="none" stroke="{färg2}" stroke-width="2.5" stroke-dasharray="4,3"/>
            {cirklar1}
            {cirklar2}
            {x_etiketter}
        </svg>
    </div>"""


def sparkline(värden, färg="#3b82f6", bredd=56, höjd=20):
    """Liten inline-trendkurva utan axlar eller etiketter, till för att sätta bredvid ett tal."""
    punkter = [v for v in värden if v is not None][-10:]
    if len(punkter) < 2:
        return ""
    mn, mx = min(punkter), max(punkter)
    if mn == mx:
        mn -= 1
        mx += 1
    n = len(punkter) - 1
    pad = 2

    def x(i):
        return pad + (i / n) * (bredd - pad * 2)

    def y(v):
        return pad + (1 - (v - mn) / (mx - mn)) * (höjd - pad * 2)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(punkter))
    prick = f'<circle cx="{x(n):.1f}" cy="{y(punkter[-1]):.1f}" r="1.8" fill="{färg}"/>'
    return (
        f'<svg width="{bredd}" height="{höjd}" viewBox="0 0 {bredd} {höjd}" class="sparkline">'
        f'<polyline points="{pts}" fill="none" stroke="{färg}" stroke-width="1.5"/>{prick}</svg>'
    )


def tile(etikett, värde, enhet="", accent="var(--blue)", historik=None):
    visning = "–" if värde is None else f"{värde}{enhet}"
    spark = sparkline(historik, färg=accent) if historik else ""
    spark_html = f'<div class="tile-spark">{spark}</div>' if spark else ""
    return f"""<div class="tile" style="--accent:{accent}">
        <div class="tile-label">{etikett}</div>
        <div class="tile-value">{visning}</div>
        {spark_html}
    </div>"""


def analysera_trend(historik, kolumn, dagar_bak=7):
    """Jämför senaste ifyllda värdet mot snittet av upp till `dagar_bak` föregående ifyllda värden."""
    värden = [till_tal(r.get(kolumn)) for r in historik]
    ifyllda = [(i, v) for i, v in enumerate(värden) if v is not None]
    if len(ifyllda) < 2:
        return None
    senaste_i, senaste_v = ifyllda[-1]
    tidigare = [v for i, v in ifyllda[:-1] if senaste_i - i <= dagar_bak]
    if not tidigare:
        return None
    snitt = sum(tidigare) / len(tidigare)
    if snitt == 0:
        return None
    return {"senaste": senaste_v, "snitt": snitt, "förändring_pct": (senaste_v - snitt) / abs(snitt) * 100}


def insikt_rad(ikon, html):
    return f'<div class="insikt"><span>{ikon}</span><span>{html}</span></div>'


def trend_insikt(etikett, trend, god_riktning, enhet="", tröskel=3):
    """Bygger en insikts-rad om förändringen mot snittet är stor nog att nämna."""
    if trend is None or abs(trend["förändring_pct"]) < tröskel:
        return None
    upp = trend["förändring_pct"] > 0
    bra = upp if god_riktning == "upp" else not upp
    ikon = "🟢" if bra else "🟠"
    riktning = "upp" if upp else "ner"
    return insikt_rad(
        ikon,
        f'<strong>{etikett}</strong> är {riktning} {abs(trend["förändring_pct"]):.0f}% jämfört med '
        f'ditt snitt senaste dagarna ({trend["senaste"]:.0f}{enhet} vs {trend["snitt"]:.0f}{enhet}).',
    )


def bygg_insikter(nyckeltal, senaste_historik, aktier):
    """Regelbaserad analys av hälso- och aktiedata — ingen extern AI-tjänst, bara jämförelser mot eget snitt."""
    rader = []

    trend_specs = [
        ("vilopuls", "Vilopuls", "ner", " bpm"),
        ("sleep_score", "Sleep score", "upp", ""),
        ("stress", "Stress", "ner", ""),
        ("hrv", "HRV", "upp", " ms"),
        ("steg", "Steg", "upp", ""),
        ("batteri", "Kroppsbatteri", "upp", ""),
    ]
    for kolumn, etikett, riktning, enhet in trend_specs:
        rad = trend_insikt(etikett, analysera_trend(senaste_historik, kolumn), riktning, enhet)
        if rad:
            rader.append(rad)

    poäng = nyckeltal.get("training_readiness")
    if poäng is not None:
        if poäng >= 70:
            rader.append(insikt_rad("🟢", f"Training readiness är högt ({poäng}/100) — bra dag för ett tufft pass."))
        elif poäng >= 40:
            rader.append(insikt_rad("🟡", f"Training readiness är måttligt ({poäng}/100) — lyssna på kroppen idag."))
        else:
            rader.append(insikt_rad("🟠", f"Training readiness är lågt ({poäng}/100) — överväg vila eller lätt träning."))

    if nyckeltal.get("training_status"):
        rader.append(insikt_rad("📈", f'Träningsstatus: <strong>{nyckeltal["training_status"]}</strong>.'))

    aktie_förändringar = []
    for symbol in aktier.keys():
        namn, _, _ = AKTIE_NAMN.get(symbol, (symbol, "Aktie", ""))
        ifyllda = [till_tal(r.get(ticker_kolumn(symbol))) for r in senaste_historik]
        ifyllda = [v for v in ifyllda if v is not None]
        if len(ifyllda) >= 2 and ifyllda[0]:
            förändring = (ifyllda[-1] - ifyllda[0]) / abs(ifyllda[0]) * 100
            aktie_förändringar.append((namn, förändring))

    if len(aktie_förändringar) > 1:
        bäst = max(aktie_förändringar, key=lambda x: x[1])
        sämst = min(aktie_förändringar, key=lambda x: x[1])
        if bäst[0] != sämst[0]:
            rader.append(insikt_rad(
                "💰",
                f'Portföljen: <strong>{bäst[0]}</strong> starkast ({bäst[1]:+.1f}%), '
                f'<strong>{sämst[0]}</strong> svagast ({sämst[1]:+.1f}%) senaste perioden.',
            ))
    elif len(aktie_förändringar) == 1:
        namn, förändring = aktie_förändringar[0]
        rader.append(insikt_rad("💰", f'<strong>{namn}</strong> är {förändring:+.1f}% senaste perioden.'))

    if not rader:
        return '<div class="chart-empty">Behöver några dagars till historik innan en analys kan göras.</div>'
    return "".join(rader)


def hämta_ai_nyckel():
    if os.path.exists(AI_NYCKEL_FIL):
        with open(AI_NYCKEL_FIL, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("api_key")
    return None


def ai_data_sammanfattning(nyckeltal, senaste_historik, aktier):
    """Bygger en kompakt textrepresentation av dagens data + trender, till AI-prompten."""
    rader = [f"Idag: vilopuls {nyckeltal.get('vilopuls')} bpm, sleep score {nyckeltal.get('sleep_score')}, "
              f"sömn {nyckeltal.get('somn_tid')}, stress {nyckeltal.get('stress')}, hrv {nyckeltal.get('hrv')} ms, "
              f"steg {nyckeltal.get('steg')}, kroppsbatteri {nyckeltal.get('batteri')}, "
              f"training readiness {nyckeltal.get('training_readiness')}/100, "
              f"training status {nyckeltal.get('training_status')}, recovery {tid_kort(nyckeltal.get('recovery_tid')) or '–'}."]

    for kolumn, etikett, enhet in [
        ("vilopuls", "Vilopuls", " bpm"), ("sleep_score", "Sleep score", ""),
        ("stress", "Stress", ""), ("hrv", "HRV", " ms"),
        ("steg", "Steg", ""), ("batteri", "Kroppsbatteri", ""),
    ]:
        trend = analysera_trend(senaste_historik, kolumn)
        if trend:
            rader.append(
                f"{etikett}-trend: {trend['senaste']:.0f}{enhet} idag mot snitt "
                f"{trend['snitt']:.0f}{enhet} senaste dagarna ({trend['förändring_pct']:+.0f}%)."
            )

    aktie_rader = []
    for symbol in aktier.keys():
        namn, _, enhet = AKTIE_NAMN.get(symbol, (symbol, "Aktie", ""))
        ifyllda = [till_tal(r.get(ticker_kolumn(symbol))) for r in senaste_historik]
        ifyllda = [v for v in ifyllda if v is not None]
        if len(ifyllda) >= 2 and ifyllda[0]:
            förändring = (ifyllda[-1] - ifyllda[0]) / abs(ifyllda[0]) * 100
            aktie_rader.append(f"{namn}: {ifyllda[-1]:.2f}{enhet} ({förändring:+.1f}% senaste perioden)")
    if aktie_rader:
        rader.append("Aktier: " + "; ".join(aktie_rader) + ".")

    return "\n".join(rader)


def generera_ai_analys(nyckeltal, senaste_historik, aktier, api_nyckel):
    """Skickar dagens data till Claude och ber om en kort personlig analys. Returnerar None vid fel."""
    data_text = ai_data_sammanfattning(nyckeltal, senaste_historik, aktier)
    prompt = (
        "Du är en personlig hälso- och ekonomicoach. Baserat på datan nedan, skriv en kort "
        "(max 4-5 meningar) naturlig analys på svenska av hur dagen ser ut hälsomässigt och "
        "för portföljen, med ett konkret råd. Undvik klichéer och floskler, var specifik och "
        "referera till siffrorna. Svara med ren löptext utan markdown-formatering — inga "
        "rubriker (#), ingen fetstil (**), inga listor.\n\n" + data_text
    )
    try:
        svar = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_nyckel,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": AI_MODELL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        svar.raise_for_status()
        return svar.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  (AI-analys misslyckades: {e})")
        return None


def hämta_eller_generera_ai_analys(nyckeltal, senaste_historik, aktier):
    """Genererar högst en AI-analys per dag, första körningen på eller efter kl 09 (börsöppning)."""
    api_nyckel = hämta_ai_nyckel()
    if not api_nyckel:
        return None

    idag = datetime.date.today().isoformat()
    cache = {}
    if os.path.exists(AI_CACHE_FIL):
        try:
            with open(AI_CACHE_FIL, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    if cache.get("datum") == idag and cache.get("text"):
        return cache["text"]

    if datetime.datetime.now().hour < 9:
        return cache.get("text")

    text = generera_ai_analys(nyckeltal, senaste_historik, aktier, api_nyckel)
    if text:
        with open(AI_CACHE_FIL, "w", encoding="utf-8") as f:
            json.dump({"datum": idag, "text": text}, f, ensure_ascii=False)
        return text
    return cache.get("text")


def fraga_data_sammanfattning(nyckeltal, historik, aktier, kost_data, utmaning_data, aktiviteter=None):
    """Bygger kontext om all tillgänglig data (hälsa, aktier, kost, utmaning, träningspass) till en fri fråga till Claude."""
    delar = [ai_data_sammanfattning(nyckeltal, historik[-30:], aktier)]

    if aktiviteter:
        rader = ["Senaste träningspassen (datum/tid, typ, distans, kalorier):"]
        for a in aktiviteter[:20]:
            namn = a.get("activityName", "Okänt pass")
            distans = a.get("distance")
            distans_km = round(distans / 1000, 2) if distans else None
            kal = a.get("calories")
            datum = kort_datumtid(a.get("startTimeLocal", ""))
            rader.append(f"{datum}: {namn}, {distans_km if distans_km else '–'} km, {kal if kal else '–'} kcal")
        delar.append("\n".join(rader))

    if historik:
        kolumner = ["datum", "somn_min", "vilopuls", "sleep_score", "stress", "hrv", "steg", "batteri", "total_kalorier"]
        rader = ["Daglig historik (datum, sömn(min), vilopuls, sleep score, stress, hrv, steg, kroppsbatteri, kalorier):"]
        for r in historik[-60:]:
            rader.append(", ".join(str(r.get(k) or "–") for k in kolumner))
        delar.append("\n".join(rader))

    if kost_data:
        rader = [
            "Kostlogg per dag (kcal/protein/fett/kolhydrater, mål: "
            f"{KOST_MÅL_KCAL} kcal, {KOST_MÅL_PROTEIN}g protein, {KOST_MÅL_FETT}g fett, {KOST_MÅL_KOLHYDRATER}g kolhydrater):"
        ]
        for datum in sorted(kost_data.keys())[-30:]:
            f = kost_data[datum]
            rader.append(
                f"{datum}: {f.get('kcal') or '–'} kcal, {f.get('protein') or '–'}g protein, "
                f"{f.get('fett') or '–'}g fett, {f.get('kolhydrater') or '–'}g kolhydrater"
            )
        delar.append("\n".join(rader))

    if utmaning_data and utmaning_data.get("dagar"):
        dagar = utmaning_data["dagar"]
        rader = [f"30-dagars-utmaningen (sedan {utmaning_data.get('start_datum', '?')}), avklarade dagar per kategori:"]
        for nyckel, etikett in DAGLIGA_KATEGORIER:
            antal_klara = sum(1 for dag in dagar.values() if (dag.get(nyckel) or {}).get("klar"))
            rader.append(f"{etikett}: {antal_klara}/{len(dagar)} dagar avklarade")
        delar.append("\n".join(rader))

    return "\n\n".join(delar)


def svara_pa_fraga(fraga, garmin, historik, aktier, kost_data, utmaning_data, api_nyckel):
    """Skickar en fri fråga från användaren till Claude tillsammans med all tillgänglig data som kontext."""
    nyckeltal = extrahera_nyckeltal(garmin)
    aktiviteter = garmin.get("aktiviteter") or []
    data_text = fraga_data_sammanfattning(nyckeltal, historik, aktier, kost_data, utmaning_data, aktiviteter)
    prompt = (
        "Du är en personlig assistent med tillgång till Markus tränings-, hälso-, kost- och aktiedata "
        "nedan. Svara kort och konkret på frågan i slutet, referera gärna till faktiska siffror eller "
        "datum ur datan. Om datan inte räcker för att svara säkert, säg det ärligt istället för att "
        "gissa. Svara på svenska, ren löptext utan markdown-formatering — inga rubriker (#), ingen "
        "fetstil (**), inga listor.\n\n"
        f"DATA:\n{data_text}\n\nFRÅGA: {fraga}"
    )
    try:
        svar = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_nyckel,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": AI_MODELL, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        svar.raise_for_status()
        return svar.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  (fråga misslyckades: {e})")
        return None


def bygg_fraga_html():
    return """
    <div class="card" style="margin-bottom:1rem;">
        <h2>Fråga din hub</h2>
        <div class="fraga-box">
            <input type="text" id="fraga-input" class="fraga-input"
                   placeholder="T.ex. hur har min sömn varit senaste veckan?"
                   onkeydown="if(event.key==='Enter') stallFraga()">
            <button id="fraga-knapp" class="fraga-knapp" onclick="stallFraga()">Fråga</button>
        </div>
        <div id="fraga-svar" class="fraga-svar" style="display:none;"></div>
    </div>
    <script>
    function stallFraga() {
        var input = document.getElementById('fraga-input');
        var knapp = document.getElementById('fraga-knapp');
        var svarBox = document.getElementById('fraga-svar');
        var fraga = input.value.trim();
        if (!fraga) return;
        knapp.disabled = true;
        knapp.textContent = 'Tänker...';
        svarBox.style.display = 'none';
        fetch('/fraga', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({fraga: fraga})
        })
            .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
            .then(function(res) {
                svarBox.style.display = 'block';
                svarBox.textContent = res.ok ? res.data.svar : (res.data.fel || 'Något gick fel.');
            })
            .catch(function() {
                svarBox.style.display = 'block';
                svarBox.textContent = 'Kunde inte fråga. Öppna sidan via serverlänken (Tailscale) för att kunna göra detta.';
            })
            .finally(function() {
                knapp.disabled = false;
                knapp.textContent = 'Fråga';
            });
    }
    </script>"""


def hämta_utmaning_data():
    if os.path.exists(UTMANING_FIL):
        with open(UTMANING_FIL, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"start_datum": UTMANING_START, "dagar": {}}


def spara_utmaning_data(data):
    with open(UTMANING_FIL, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def generera_dagliga_förslag(api_nyckel, kategorier, tidigare_per_kategori):
    """Ber Claude om ett konkret förslag per given kategori, i ett enda anrop."""
    kategori_lista = "\n".join(f"{i + 1}) {etikett}" for i, (_, etikett) in enumerate(kategorier))
    undvik_delar = []
    for nyckel, etikett in kategorier:
        tidigare = tidigare_per_kategori.get(nyckel) or []
        if tidigare:
            undvik_delar.append(f"{etikett} — undvik att upprepa: " + "; ".join(tidigare[-5:]))
    undvik_text = "\n".join(undvik_delar) if undvik_delar else "Inga tidigare förslag ännu."

    prompt = (
        "Ge mig konkreta, korta (max en mening var) personliga uppgiftsförslag på svenska för "
        f"exakt dessa kategorier, i denna ordning:\n{kategori_lista}\n\n"
        f"Tidigare förslag att undvika att upprepa:\n{undvik_text}\n\n"
        f"Svara med exakt {len(kategorier)} rader, en per kategori i samma ordning, utan rubriker, "
        "numrering eller markdown — bara själva uppgiften på varje rad."
    )
    try:
        svar = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_nyckel,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": AI_MODELL, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        svar.raise_for_status()
        text = svar.json()["content"][0]["text"].strip()
        rader = [r.strip(" -•") for r in text.split("\n") if r.strip()]
        return {kategorier[i][0]: rader[i] for i in range(min(len(rader), len(kategorier)))}
    except Exception as e:
        print(f"  (kunde inte generera dagens förslag: {e})")
        return {}


def säkerställ_dagens_kategorier(data, datum):
    """Fyller i saknade kategorier för dagen med AI-förslag. Kategorier som redan satts
    (t.ex. via ett eget förslag skrivet i förväg) rörs inte."""
    rad = data["dagar"].get(datum, {})
    saknade = [(nyckel, etikett) for nyckel, etikett in DAGLIGA_KATEGORIER if nyckel not in rad]

    if saknade:
        api_nyckel = hämta_ai_nyckel()
        förslag = {}
        if api_nyckel:
            tidigare_per_kategori = {}
            for dag_data in data["dagar"].values():
                for nyckel, _ in DAGLIGA_KATEGORIER:
                    post = dag_data.get(nyckel)
                    if isinstance(post, dict) and post.get("text"):
                        tidigare_per_kategori.setdefault(nyckel, []).append(post["text"])
            förslag = generera_dagliga_förslag(api_nyckel, saknade, tidigare_per_kategori)

        for nyckel, etikett in saknade:
            rad[nyckel] = {
                "text": förslag.get(nyckel) or f"(inget förslag genererat för {etikett})",
                "klar": False,
            }

    data["dagar"][datum] = rad
    return data


def utmaning_statistik(data):
    start = datetime.date.fromisoformat(data["start_datum"])
    dag_nummer = (datetime.date.today() - start).days + 1

    def helt_klar(rad):
        return all((rad.get(nyckel) or {}).get("klar") for nyckel, _ in DAGLIGA_KATEGORIER)

    alla_dagar = sorted(d for d in data["dagar"].keys() if datetime.date.fromisoformat(d) >= start)
    längsta_streak = 0
    tillfällig = 0
    lyckade_dagar = 0
    for dag in alla_dagar:
        if helt_klar(data["dagar"][dag]):
            lyckade_dagar += 1
            tillfällig += 1
            längsta_streak = max(längsta_streak, tillfällig)
        else:
            tillfällig = 0

    aktuell_streak = 0
    for dag in reversed(alla_dagar):
        if helt_klar(data["dagar"][dag]):
            aktuell_streak += 1
        else:
            break

    return {
        "dag_nummer": dag_nummer,
        "total_dagar": UTMANING_LÄNGD,
        "lyckade_dagar": lyckade_dagar,
        "längsta_streak": längsta_streak,
        "aktuell_streak": aktuell_streak,
    }


def bygg_utmaning_html(utmaning_data, idag_str):
    statistik = utmaning_statistik(utmaning_data)
    dagens = utmaning_data["dagar"].get(idag_str, {})

    mål_rader = ""
    for nyckel, etikett in DAGLIGA_KATEGORIER:
        post = dagens.get(nyckel) or {}
        bockad = "checked" if post.get("klar") else ""
        mål_rader += f"""
        <label class="bock-rad" data-datum="{idag_str}" data-kategori="{nyckel}">
            <input type="checkbox" {bockad} onchange="bocka(this)">
            <span><strong>{etikett}:</strong> {post.get("text", "–")}</span>
        </label>"""

    if statistik["dag_nummer"] < 1:
        dag_etikett = f"Startar {utmaning_data['start_datum']}"
    else:
        dag_etikett = f"Dag {statistik['dag_nummer']}/{statistik['total_dagar']}"

    return f"""
    <div class="card" style="margin-bottom:1rem;">
        <h2>30 dagars-utmaning <span class="tile-sub">{dag_etikett}</span></h2>
        <div class="row-2">
            {tile("Nuvarande streak", statistik["aktuell_streak"], " dagar", accent="var(--green)")}
            {tile("Längsta streak", statistik["längsta_streak"], " dagar", accent="var(--purple)")}
        </div>
        <div style="margin-top:1rem;">
            {mål_rader}
        </div>
    </div>
    <script>
    function bocka(checkbox) {{
        var rad = checkbox.closest('.bock-rad');
        var datum = rad.dataset.datum;
        var kategori = rad.dataset.kategori;
        var klar = checkbox.checked;
        rad.classList.add('sparar');
        fetch('/bocka', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{datum: datum, kategori: kategori, klar: klar}})
        }}).then(function(svar) {{
            if (!svar.ok) throw new Error('fel');
            rad.classList.remove('sparar');
        }}).catch(function() {{
            checkbox.checked = !klar;
            rad.classList.remove('sparar');
            alert('Kunde inte spara. Öppna sidan via serverlänken (Tailscale) för att kunna bocka av.');
        }});
    }}
    </script>"""


def bygg_html(garmin, aktier, historik, utmaning_data=None):
    nyckeltal = extrahera_nyckeltal(garmin)
    aktiviteter = garmin.get("aktiviteter") or []

    strong_pass = läs_strong_pass()

    aktivitet_rader = ""
    if aktiviteter:
        for a in aktiviteter[:5]:
            namn = a.get("activityName", "Okänt pass")
            distans = a.get("distance")
            distans_km = round(distans / 1000, 2) if distans else None
            kal = a.get("calories")
            datum = kort_datumtid(a.get("startTimeLocal", ""))
            rutt = a.get("rutt")

            tempo = format_tempo(a.get("movingDuration") or a.get("duration"), distans)
            tid_rorelse = format_varaktighet(a.get("movingDuration") or a.get("duration"))
            höjdökning = a.get("elevationGain")
            snittpuls = a.get("averageHR")

            rad_innehåll = f"""
                <div class="list-icon">{ikon_för_aktivitet(namn)}</div>
                <div class="list-body">
                    <div class="list-title">{namn}</div>
                    <div class="list-sub">{datum} · {distans_km if distans_km else '–'} km</div>
                </div>
                <div class="list-value">{kal if kal else '–'} kcal</div>"""

            detalj_rutor = f"""
                <div class="mini-stats">
                    {tile("Distans", distans_km, " km", accent="var(--blue)")}
                    {tile("Tempo", tempo, accent="var(--teal)")}
                    {tile("Tid i rörelse", tid_rorelse, accent="var(--purple)")}
                    {tile("Höjdökning", höjdökning, " m", accent="var(--orange)")}
                    {tile("Kalorier", kal, " kcal", accent="var(--orange)")}
                    {tile("Snittpuls", snittpuls, " bpm", accent="var(--red)")}
                </div>"""

            strong_html = ""
            typ = (a.get("activityType") or {}).get("typeKey", "")
            if "strength" in typ:
                aktivitet_datum = (a.get("startTimeLocal") or "")[:10]
                sets_för_dagen = strong_pass.get(aktivitet_datum)
                if sets_för_dagen:
                    strong_html = f'<div class="strong-pass">{bygg_strong_html(sets_för_dagen)}</div>'
                else:
                    strong_html = (
                        '<div class="chart-empty">Ingen matchande Strong-export för det datumet ännu.</div>'
                    )

            karta_id = f"karta-{a.get('activityId')}"
            ontoggle = f" ontoggle=\"if(this.open) visaKarta('{karta_id}', {json.dumps(rutt)})\"" if rutt else ""
            karta_div = f'<div id="{karta_id}" class="karta"></div>' if rutt else ""

            aktivitet_rader += f"""
            <details class="karta-rad"{ontoggle}>
                <summary class="list-row">{rad_innehåll}</summary>
                <div class="detalj-panel">
                    {detalj_rutor}
                    {strong_html}
                    {karta_div}
                </div>
            </details>"""
    else:
        aktivitet_rader = '<div class="chart-empty">Ingen aktivitetsdata hittad</div>'

    # --- Historik / trenddiagram (senaste 30 gångerna) ---
    senaste_historik = historik[-30:]
    datum_lista = [r.get("datum", "") for r in senaste_historik]

    insikter_html = bygg_insikter(nyckeltal, senaste_historik, aktier)

    if strong_pass:
        senaste_strong_datum = max(strong_pass.keys())
        dagar_sedan_export = (datetime.date.today() - datetime.date.fromisoformat(senaste_strong_datum)).days
        if dagar_sedan_export > 7:
            insikter_html += insikt_rad(
                "🏋️",
                f"Strong-exporten är {dagar_sedan_export} dagar gammal — exportera igen från appen för att få in de senaste passen.",
            )
    else:
        insikter_html += insikt_rad(
            "🏋️", "Ingen Strong-export hittad än — exportera från appen för att få in dina styrkepass."
        )

    if utmaning_data:
        utmaning_html = bygg_utmaning_html(utmaning_data, garmin["datum"])
    else:
        utmaning_html = ""

    morgonrutin_html = bygg_morgonrutin_html(garmin)
    kost_html = bygg_kost_html(garmin["datum"])
    kost_historik_html = bygg_kost_historik_html()
    kaloribalans_html = bygg_kaloribalans_html()

    aktie_historik_färger = ["#a78bfa", "#3b82f6", "#f59e0b", "#22c55e", "#f43f5e"]
    färg_index = 0
    bransch_sektioner_html = ""
    for bransch, bolag in BRANSCHER:
        rader = ""
        grafer = ""
        for ticker, namn, enhet in bolag:
            info = aktier.get(ticker)
            if not info:
                continue
            positiv = (info["förändring_pct"] or 0) >= 0
            färg_status = "var(--green)" if positiv else "var(--red)"
            tecken = "+" if positiv else ""
            rader += f"""
            <div class="stock-row">
                <div class="stock-name">{namn}</div>
                <div>
                    <div class="stock-price">{info['pris']}{enhet}</div>
                    <div class="stock-change" style="color:{färg_status}">{tecken}{info['förändring_pct']}%</div>
                </div>
            </div>"""

            aktie_värden = [till_tal(r.get(ticker_kolumn(ticker))) for r in senaste_historik]
            grafer += svg_linjediagram(
                namn, datum_lista, aktie_värden, enhet,
                färg=aktie_historik_färger[färg_index % len(aktie_historik_färger)], fyllning=True,
            )
            färg_index += 1

        if not rader:
            continue

        bransch_sektioner_html += f"""
        <details class="bransch">
            <summary>{bransch} <span class="tile-sub">{len(bolag)} bolag</span></summary>
            <div class="bransch-innehall">
                {rader}
                <div class="stack" style="margin-top:0.8rem;">
                    {grafer}
                </div>
            </div>
        </details>"""

    sleep_score_värden = [till_tal(r.get("sleep_score")) for r in senaste_historik]
    somn_min_värden = [till_tal(r.get("somn_min")) for r in senaste_historik]
    somn_timmar_värden = [v / 60 if v is not None else None for v in somn_min_värden]
    senaste_somn_min = next((v for v in reversed(somn_min_värden) if v is not None), None)
    vilopuls_värden = [till_tal(r.get("vilopuls")) for r in senaste_historik]
    hrv_värden = [till_tal(r.get("hrv")) for r in senaste_historik]
    stress_hist_värden = [till_tal(r.get("stress")) for r in senaste_historik]
    batteri_värden = [till_tal(r.get("batteri")) for r in senaste_historik]

    historik_html = f"""
    <div class="card">
        <h2>Historik och trender <span class="tile-sub">senaste {len(senaste_historik)} dagarna</span></h2>
        <div class="chart-grid">
            {svg_linjediagram("Sleep score", datum_lista, sleep_score_värden, y_min=1, y_max=100, y_steg=25, färg="#22c55e", fyllning=True)}
            {svg_linjediagram("Stress", datum_lista, stress_hist_värden, y_min=1, y_max=100, y_steg=25, färg="#f43f5e", fyllning=True)}
            {svg_linjediagram("Sömn", datum_lista, somn_timmar_värden, y_min=0, y_max=10, y_steg=2, färg="#3b82f6", fyllning=True, senaste_text=tid_kort(senaste_somn_min))}
            {svg_dubbeldiagram("Sömn & Sleep score", datum_lista, somn_timmar_värden, "Sömn", "#3b82f6", sleep_score_värden, "Sleep score", "#22c55e", enhet1="h", senaste_text1=tid_kort(senaste_somn_min), y_min1=1, y_max1=10, y_steg1=1, y_min2=0, y_max2=100, y_steg2=10)}
            {svg_linjediagram("Vilopuls", datum_lista, vilopuls_värden, " bpm", y_min=40, y_max=100, y_steg=20, färg="#06b6d4", fyllning=True)}
            {svg_linjediagram("HRV", datum_lista, hrv_värden, " ms", y_min=0, y_max=150, y_steg=25, färg="#a78bfa", fyllning=True)}
            {svg_linjediagram("Body Battery", datum_lista, batteri_värden, y_min=0, y_max=100, y_steg=25, färg="#f59e0b", fyllning=True)}
        </div>
    </div>"""

    # --- Dagsvariation (puls & stress under dagen) ---
    puls_punkter = nedsampla(intraday_serie(garmin.get("puls_dagen"), "heartRateValues"), 200)
    stress_punkter = nedsampla(intraday_serie(garmin.get("stress_dagen"), "stressValuesArray"), 200)

    dagsvariation_html = f"""
    <div class="card">
        <h2>Dagens variation</h2>
        {svg_dagsdiagram("Puls under dagen", puls_punkter, " bpm", y_min=40, y_max=180, y_steg=35, färg="#06b6d4")}
        {svg_dagsdiagram("Stress under dagen", stress_punkter, y_min=0, y_max=100, y_steg=25, färg="#f43f5e")}
    </div>"""

    steg = nyckeltal["steg"] or 0
    steg_mål = 10000
    steg_procent = max(0, min(100, round(steg / steg_mål * 100)))

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Min Hub - {garmin['datum']}</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="{IKON_SVG}">
<link rel="apple-touch-icon" href="{IKON_SVG}">
<meta name="theme-color" content="#0b0e14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Min Hub">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
    :root {{
        --bg: #0b0e14;
        --card: #12161f;
        --card-2: #171c27;
        --border: rgba(255,255,255,0.07);
        --text: #e6e9ef;
        --muted: #7d8794;
        --green: #22c55e;
        --blue: #3b82f6;
        --red: #f43f5e;
        --orange: #f59e0b;
        --purple: #a78bfa;
        --teal: #06b6d4;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        font-family: -apple-system, "Segoe UI", sans-serif;
        background: var(--bg);
        color: var(--text);
        margin: 0;
        padding: 1.5rem;
    }}
    .topbar {{ display:flex; justify-content:space-between; align-items:center;
               margin-bottom:1.2rem; flex-wrap:wrap; gap:0.5rem; }}
    h1 {{ font-size:1.5rem; margin:0; font-weight:600; }}
    h2 {{ font-size:0.95rem; margin:0 0 0.9rem 0; font-weight:600; display:flex;
          justify-content:space-between; align-items:baseline; }}
    .pill {{ background:var(--card-2); border:1px solid var(--border); border-radius:999px;
             padding:0.35rem 0.9rem; font-size:0.75rem; color:var(--muted); }}
    .uppdatera-knapp {{ background:var(--blue); color:white; border:none; border-radius:999px;
                         padding:0.35rem 0.9rem; font-size:0.75rem; font-weight:600; cursor:pointer; }}
    .uppdatera-knapp:disabled {{ opacity:0.6; cursor:default; }}

    .layout {{ display:grid; grid-template-columns: 2fr 1fr; gap:1rem; align-items:start; }}
    .main, .aside {{ display:flex; flex-direction:column; gap:1rem; }}

    .card {{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:1.25rem; }}

    .row-3 {{ display:grid; grid-template-columns: 1.3fr 1fr; gap:1rem; }}
    .row-4 {{ display:grid; grid-template-columns: repeat(4, 1fr); gap:1rem; }}
    .row-2 {{ display:grid; grid-template-columns: 1fr 1fr; gap:1rem; }}
    .stack {{ display:flex; flex-direction:column; gap:1rem; }}

    .big-tile {{ display:flex; flex-direction:column; justify-content:space-between; }}
    .tile-label {{ font-size:0.7rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); }}
    .big-value {{ font-size:2.1rem; font-weight:700; margin-top:0.4rem; }}
    .big-value .tile-sub {{ font-size:1rem; color:var(--muted); font-weight:400; }}
    .progress {{ background:rgba(255,255,255,0.08); border-radius:999px; height:6px; margin-top:0.9rem; overflow:hidden; }}
    .progress-fill {{ background:var(--green); height:100%; border-radius:999px; }}
    .tile-footnote {{ font-size:0.75rem; color:var(--muted); margin-top:0.5rem; }}

    .medium-tile {{ border-left:3px solid var(--accent, var(--green)); }}
    .medium-value {{ font-size:1.6rem; font-weight:700; margin-top:0.3rem; }}
    .medium-tile .tile-sub {{ font-size:0.78rem; color:var(--muted); margin-top:0.4rem; display:block; }}

    .tile {{ background:var(--card-2); border:1px solid var(--border); border-bottom:3px solid var(--accent, var(--green));
             border-radius:14px; padding:0.9rem 1rem; }}
    .tile .tile-value {{ font-size:1.25rem; font-weight:700; margin-top:0.3rem; }}
    .tile-spark {{ margin-top:0.4rem; opacity:0.9; }}
    .medium-tile .tile-spark {{ margin-top:0.5rem; }}

    .list {{ display:flex; flex-direction:column; gap:0.6rem; }}
    .list-row {{ display:flex; align-items:center; gap:0.8rem; padding:0.6rem; border-radius:10px; background:var(--card-2); }}
    .list-icon {{ width:38px; height:38px; border-radius:10px; background:rgba(245,158,11,0.15);
                  display:flex; align-items:center; justify-content:center; font-size:1.1rem; flex-shrink:0; }}
    .list-body {{ flex:1; min-width:0; }}
    .list-title {{ font-size:0.85rem; font-weight:600; }}
    .list-sub {{ font-size:0.72rem; color:var(--muted); margin-top:0.15rem; }}
    .list-value {{ font-size:0.85rem; font-weight:600; white-space:nowrap; }}

    .karta-rad summary {{ list-style:none; cursor:pointer; }}
    .karta-rad summary::-webkit-details-marker {{ display:none; }}
    .karta {{ height:280px; border-radius:10px; margin-top:0.5rem; overflow:hidden; }}
    .detalj-panel {{ padding-top:0.6rem; }}
    .mini-stats {{ display:grid; grid-template-columns: repeat(3, 1fr); gap:0.5rem; }}
    .strong-pass {{ margin-top:0.8rem; display:flex; flex-direction:column; gap:0.5rem; }}
    .strong-ovning {{ background:var(--card-2); border-radius:10px; padding:0.6rem 0.8rem; }}
    .strong-namn {{ font-size:0.82rem; font-weight:600; }}
    .strong-sets {{ font-size:0.75rem; color:var(--muted); margin-top:0.2rem; }}

    .kost-grid {{ display:grid; grid-template-columns: repeat(2, 1fr); gap:0.8rem; }}
    .kost-rad {{ background:var(--card-2); border-radius:12px; padding:0.7rem 0.9rem; }}
    .kost-rad.sparar {{ opacity:0.6; }}
    .kost-topp {{ display:flex; justify-content:space-between; align-items:baseline;
                  font-size:0.7rem; text-transform:uppercase; letter-spacing:0.03em; margin-bottom:0.35rem; }}
    .kost-etikett {{ font-weight:600; color:var(--text); }}
    .kost-mal {{ color:var(--muted); }}
    .kost-input {{ width:100%; background:var(--card); border:1px solid var(--border); border-radius:8px;
                    color:var(--text); font-size:1.1rem; font-weight:700; padding:0.4rem 0.6rem; }}

    .fraga-box {{ display:flex; gap:0.6rem; }}
    .fraga-input {{ flex:1; background:var(--card-2); border:1px solid var(--border); border-radius:8px;
                     color:var(--text); font-size:0.9rem; padding:0.6rem 0.8rem; }}
    .fraga-knapp {{ background:var(--blue); color:white; border:none; border-radius:999px;
                     padding:0.6rem 1.1rem; font-size:0.85rem; font-weight:600; cursor:pointer; flex-shrink:0; }}
    .fraga-knapp:disabled {{ opacity:0.6; cursor:default; }}
    .fraga-svar {{ margin-top:0.9rem; font-size:0.88rem; line-height:1.5; background:var(--card-2);
                    border-radius:10px; padding:0.8rem 1rem; white-space:pre-wrap; }}

    .stock-row {{ display:flex; justify-content:space-between; align-items:center; padding:0.6rem 0; border-bottom:1px solid var(--border); }}
    .stock-row:last-child {{ border-bottom:none; }}
    .stock-name {{ font-weight:600; font-size:0.9rem; }}
    .stock-sub {{ font-size:0.72rem; color:var(--muted); margin-top:0.1rem; }}
    .stock-price {{ font-weight:600; font-size:0.9rem; text-align:right; }}
    .stock-change {{ font-size:0.75rem; text-align:right; margin-top:0.1rem; }}

    .bransch {{ border-bottom:1px solid var(--border); }}
    .bransch:last-child {{ border-bottom:none; }}
    .bransch summary {{ cursor:pointer; padding:0.7rem 0; font-size:0.85rem; font-weight:600;
                         list-style:none; display:flex; justify-content:space-between; align-items:baseline; }}
    .bransch summary::-webkit-details-marker {{ display:none; }}
    .bransch summary::before {{ content:'▸ '; color:var(--muted); }}
    .bransch[open] summary::before {{ content:'▾ '; }}
    .bransch-innehall {{ padding-bottom:0.8rem; }}

    .chart-grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:1rem 1.5rem; }}
    .chart-title {{ font-size:0.78rem; color:var(--muted); display:flex; justify-content:space-between; margin-bottom:0.2rem; }}
    .chart-latest {{ font-weight:700; color:var(--text); }}
    .chart-empty {{ font-size:0.78rem; color:var(--muted); padding:1rem 0; }}

    .insikt {{ display:flex; align-items:flex-start; gap:0.6rem; padding:0.55rem 0;
               border-bottom:1px solid var(--border); font-size:0.85rem; line-height:1.4; }}
    .insikt:last-child {{ border-bottom:none; }}

    .bock-rad {{ display:flex; align-items:flex-start; gap:0.6rem; padding:0.55rem 0;
                 border-bottom:1px solid var(--border); font-size:0.85rem; line-height:1.4; cursor:pointer; }}
    .bock-rad:last-child {{ border-bottom:none; }}
    .bock-rad input[type="checkbox"] {{ width:18px; height:18px; margin-top:0.15rem; accent-color:var(--green);
                                         cursor:pointer; flex-shrink:0; }}
    .bock-rad.sparar {{ opacity:0.5; }}

    @media (max-width: 860px) {{
        .layout {{ grid-template-columns: 1fr; }}
        .row-3 {{ grid-template-columns: 1fr; }}
        .row-4 {{ grid-template-columns: 1fr 1fr; }}
        .chart-grid {{ grid-template-columns: 1fr; }}
        .mini-stats {{ grid-template-columns: 1fr 1fr; }}
        body {{ padding:1rem; }}
    }}
</style>
</head>
<body>
    <script>
    function visaKarta(id, punkter) {{
        var container = document.getElementById(id);
        if (!container || container.dataset.klar) return;
        container.dataset.klar = "1";
        var karta = L.map(id);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap',
            maxZoom: 19
        }}).addTo(karta);
        var linje = L.polyline(punkter, {{color: '#06b6d4', weight: 3}}).addTo(karta);
        karta.fitBounds(linje.getBounds());
    }}
    </script>
    <div class="topbar">
        <h1>{hälsning()}</h1>
        <div style="display:flex; align-items:center; gap:0.6rem;">
            <div class="pill">Uppdaterad {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
            <button id="uppdatera-knapp" class="uppdatera-knapp" onclick="uppdateraNu()">🔄 Uppdatera nu</button>
        </div>
    </div>
    <script>
    function uppdateraNu() {{
        var knapp = document.getElementById('uppdatera-knapp');
        knapp.disabled = true;
        knapp.textContent = 'Uppdaterar...';
        fetch('/uppdatera', {{method: 'POST'}})
            .then(function(svar) {{
                if (!svar.ok) throw new Error('fel');
                location.reload();
            }})
            .catch(function() {{
                knapp.disabled = false;
                knapp.textContent = '🔄 Uppdatera nu';
                alert('Kunde inte uppdatera. Öppna sidan via serverlänken (Tailscale) för att kunna göra detta.');
            }});
    }}
    </script>

    <div class="card" style="margin-bottom:1rem;">
        <h2>Analys <span class="tile-sub">uppdateras varje körning</span></h2>
        {insikter_html}
    </div>

    {bygg_fraga_html()}

    {utmaning_html}

    {morgonrutin_html}

    {kost_html}

    {kost_historik_html}

    {kaloribalans_html}

    <div class="layout">
        <div class="main">
            <div class="row-3">
                <div class="card big-tile">
                    <div>
                        <div class="tile-label">Dagens steg</div>
                        <div class="big-value">{tal_sep(steg)}<span class="tile-sub"> / {tal_sep(steg_mål)}</span></div>
                    </div>
                    <div>
                        <div class="progress"><div class="progress-fill" style="width:{steg_procent}%"></div></div>
                        <div class="tile-footnote">{steg_procent}% av dagens mål</div>
                    </div>
                </div>
                <div class="stack">
                    <div class="card medium-tile" style="--accent:var(--teal)">
                        <div class="tile-label">Vilopuls</div>
                        <div class="medium-value">{nyckeltal['vilopuls'] if nyckeltal['vilopuls'] else '–'} bpm</div>
                        <span class="tile-sub">HRV-status: {nyckeltal['hrv_status'] or '–'}</span>
                        <div class="tile-spark">{sparkline(vilopuls_värden, färg="var(--teal)")}</div>
                    </div>
                    <div class="card medium-tile" style="--accent:var(--green)">
                        <div class="tile-label">Sömnkvalitet</div>
                        <div class="medium-value">{nyckeltal['sleep_score'] if nyckeltal['sleep_score'] else '–'}/100</div>
                        <span class="tile-sub">{nyckeltal['somn_tid'] or '–'} · Batteri: {nyckeltal['batteri'] if nyckeltal['batteri'] else '–'}</span>
                        <div class="tile-spark">{sparkline(sleep_score_värden, färg="var(--green)")}</div>
                    </div>
                </div>
            </div>

            <div class="row-4">
                {tile("Stress (snitt)", nyckeltal["stress"], accent="var(--red)", historik=stress_hist_värden)}
                {tile("VO2 max", nyckeltal["vo2max"], accent="var(--blue)")}
                {tile("Totala kalorier", nyckeltal["total_kalorier"], " kcal", accent="var(--orange)")}
                {tile("Aktiva kalorier", nyckeltal["aktiv_kalorier"], " kcal", accent="var(--green)")}
            </div>

            <div class="card">
                <h2>Senaste träning</h2>
                <div class="list">
                    {aktivitet_rader}
                </div>
            </div>

            {dagsvariation_html}

            {historik_html}
        </div>

        <div class="aside">
            <div class="card">
                <h2>Portföljen</h2>
                {bransch_sektioner_html}
            </div>

            <div class="card">
                <h2>Kroppssammansättning</h2>
                <div class="row-2">
                    {tile("Vikt", nyckeltal["vikt"], " kg", accent="var(--blue)")}
                    {tile("Kroppsfett", nyckeltal["kroppsfett"], " %", accent="var(--orange)")}
                </div>
            </div>

            <div class="card">
                <h2>Träning &amp; återhämtning</h2>
                <div class="row-2">
                    {tile("Training readiness", nyckeltal["training_readiness"], "/100", accent="var(--green)")}
                    {tile("Recovery", tid_kort(nyckeltal["recovery_tid"]), accent="var(--blue)")}
                </div>
                <div class="tile-sub" style="margin-top:0.8rem;">Training status: <strong style="color:var(--text)">{nyckeltal['training_status'] or '–'}</strong></div>
            </div>

            <div class="card">
                <h2>Historik</h2>
                <div class="tile-sub" style="margin-bottom:0.8rem;">{len(historik)} dag(ar) sparade i {HISTORIK_FIL}</div>
                <div class="row-2">
                    {tile("Kalorier", nyckeltal["total_kalorier"], " kcal", accent="var(--orange)")}
                    {tile("Batteri", nyckeltal["batteri"], accent="var(--orange)", historik=batteri_värden)}
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""
    return html


def push_to_github():
    repo_path = r"C:\Users\marku\Desktop\garmin-hub"

    try:
        os.chdir(repo_path)

        # Kolla om Git är initierat
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            check=True
        )

        if not result.stdout.strip():
            print("Inga ändringar att pusha.")
            return

        # Lägg till och commit
        subprocess.run(["git", "add", "raw_debug.json", "historik.csv", ".gitignore"], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"automatisk uppdatering {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            check=True
        )

        # Pusha till GitHub
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("GitHub uppdaterat!")

    except subprocess.CalledProcessError as e:
        print(f"Git-fel: {e}")
    except Exception as e:
        print(f"Något gick fel: {e}")


if __name__ == "__main__":
    garmin_data = hamta_garmin_data()
    aktiedata = hamta_aktiekurser(TICKERS)
    historik = spara_historik(garmin_data, aktiedata)

    with open("aktier_cache.json", "w", encoding="utf-8") as f:
        json.dump(aktiedata, f, ensure_ascii=False)

    print("Kollar dagens utmaning...")
    utmaning_data = hämta_utmaning_data()
    utmaning_data = säkerställ_dagens_kategorier(utmaning_data, garmin_data["datum"])
    spara_utmaning_data(utmaning_data)

    print(f"\nKlart! (Historik sparad i {HISTORIK_FIL} — {len(historik)} dag(ar) hittills). "
          "Sidan byggs live av server.py — inget att öppna härifrån.")

    push_to_github()

