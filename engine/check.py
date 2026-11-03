"""NK Tegenwindfietsen: kansberekening en pushmeldingen.

Draait in GitHub Actions. Haalt ensemble-windverwachtingen op voor de
Oosterscheldekering, berekent per dag de weerkans en de kans dat het NK
doorgaat, schrijft docs/data.json en stuurt bij 50% of meer een pushmelding.
"""
import json
import math
import os
import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data.json")
STATE = os.path.join(ROOT, "engine", "state.json")
TZ = ZoneInfo("Europe/Amsterdam")

LAT, LON = 51.65, 3.69          # Oosterscheldekering, Neeltje Jans
WINDOW = range(8, 14)           # 08.00 t/m 13.00 uur
MIN_MEAN = 12.0                 # m/s modelwind, gekalibreerd op edities 2018-2024
MAX_GUST = 34.7                 # m/s = 125 km/u, bovengrens uit vergunning
SW_SECTOR = (165, 265)          # zuid tot west, waar alle edities vielen
NE_WEIGHT = 0.3                 # noord/noordoost mag, maar kwam nooit voor
WEEKDAY_FACTOR = 0.6            # 4 van 5 edities in het weekend
NOTIFY_AT = 50                  # procent kans dat het NK doorgaat
STORM_MODE_AT = 25              # weerkans waarbij we 3x per dag rekenen
MODELS = "ecmwf_ifs025"           # alleen ECMWF: het beste model, 51 varianten, 15 dagen vooruit


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def in_season(d: date) -> bool:
    return d.month >= 10 or d.month <= 2


def circ_mean(degrees):
    sx = sum(math.sin(math.radians(v)) for v in degrees)
    cx = sum(math.cos(math.radians(v)) for v in degrees)
    a = math.degrees(math.atan2(sx, cx))
    return a + 360 if a < 0 else a


def direction_weight(deg):
    if SW_SECTOR[0] <= deg <= SW_SECTOR[1]:
        return 1.0
    if deg >= 345 or deg <= 60:
        return NE_WEIGHT
    return 0.0


def fetch():
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    params = dict(latitude=LAT, longitude=LON, models=MODELS, forecast_days=15,
                  hourly="wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                  wind_speed_unit="ms", timezone="Europe/Amsterdam")
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["hourly"]


def analyse(h):
    """Geeft per dag weerkans, doorgaanskans en een paar kerngetallen."""
    times = h["time"]
    members = [k[len("wind_speed_10m"):] for k in h if k.startswith("wind_speed_10m")]
    days = []
    for start in range(0, len(times), 24):
        d = date.fromisoformat(times[start][:10])
        per_model = {}
        means = []
        for suf in members:
            idx = [start + hr for hr in WINDOW]
            ws = [h["wind_speed_10m" + suf][i] for i in idx]
            wd = [h["wind_direction_10m" + suf][i] for i in idx]
            wg = [h["wind_gusts_10m" + suf][i] for i in idx]
            if any(v is None for v in ws + wd + wg):
                continue
            model = suf.split("_", 2)[-1] if "member" in suf else suf.lstrip("_")
            mean = sum(ws) / len(ws)
            means.append(mean)
            hit = 0.0
            if mean >= MIN_MEAN and max(wg) < MAX_GUST:
                hit = direction_weight(circ_mean(wd))
            per_model.setdefault(model, []).append(hit)
        if not per_model:
            continue
        weer = sum(sum(v) / len(v) for v in per_model.values()) / len(per_model)
        factor = 1.0 if d.weekday() >= 5 else WEEKDAY_FACTOR
        nk = weer * factor if in_season(d) else 0.0
        means.sort()
        days.append({
            "date": d.isoformat(),
            "weer": round(weer * 100),
            "nk": round(nk * 100),
            "wind_med": round(means[len(means) // 2], 1),
            "wind_p90": round(means[int(len(means) * 0.9) - 1], 1),
            "leden": sum(len(v) for v in per_model.values()),
        })
    return days


def send_push(title, body, tag):
    subs = json.loads(os.environ.get("PUSH_SUBSCRIPTIONS", "[]") or "[]")
    if isinstance(subs, dict):
        subs = [subs]
    key = os.environ.get("VAPID_PRIVATE_KEY")
    if not subs or not key:
        print("Geen abonnement of sleutel, melding overgeslagen:", title)
        return
    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": title, "body": body, "tag": tag})
    for sub in subs:
        try:
            webpush(sub, payload, vapid_private_key=key,
                    vapid_claims={"sub": "mailto:paul@paulmusters.nl"},
                    ttl=6 * 3600, headers={"Urgency": "high"})
        except WebPushException as e:
            print("Push mislukt:", e)


def nl_date(iso):
    d = date.fromisoformat(iso)
    dagen = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    maanden = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
               "augustus", "september", "oktober", "november", "december"]
    return f"{dagen[d.weekday()]} {d.day} {maanden[d.month - 1]}"


def main():
    now = datetime.now(TZ)
    state = load(STATE, {"storm_mode": False, "notified": {}, "held_season": None})
    mode = sys.argv[1] if len(sys.argv) > 1 else "scheduled"

    if mode == "test":
        send_push("NK Tegenwind", "Testmelding: je meldingen werken.", "test")
        return

    # Normaal 1x per dag (ochtendrun); de andere runs alleen in stormstand.
    if mode == "scheduled" and now.hour > 9 and not state.get("storm_mode"):
        print("Geen stormstand, run overgeslagen.")
        return

    season = f"{now.year}-{now.year + 1}" if now.month >= 7 else f"{now.year - 1}-{now.year}"
    days = analyse(fetch())
    if state.get("held_season") == season:
        for d in days:
            d["nk"] = 0

    state["storm_mode"] = any(d["weer"] >= STORM_MODE_AT for d in days)

    # Meldingen: eenmalig per dag zodra de kans 50% haalt, en bij een flinke daling.
    notified = {k: v for k, v in state.get("notified", {}).items() if k >= now.date().isoformat()}
    for d in days:
        prev = notified.get(d["date"])
        if d["nk"] >= NOTIFY_AT and prev is None:
            dagen_vooruit = (date.fromisoformat(d["date"]) - now.date()).days
            send_push(f"NK Tegenwind: {d['nk']}% kans",
                      f"{nl_date(d['date']).capitalize()}, over {dagen_vooruit} dagen. "
                      f"Wind rond {str(d['wind_med']).replace('.', ',')} m/s. De inschrijving opent meestal 2 tot 3 dagen vooraf.",
                      d["date"])
            notified[d["date"]] = d["nk"]
        elif prev is not None and d["nk"] < 30 and prev >= NOTIFY_AT:
            send_push("NK Tegenwind: kans gezakt",
                      f"{nl_date(d['date']).capitalize()} nu {d['nk']}% kans.", d["date"])
            notified[d["date"]] = -1
    state["notified"] = notified

    # Maandelijkse controlemelding op de eerste ochtend van de maand, alleen in het seizoen.
    if now.day == 1 and in_season(now.date()) and state.get("heartbeat") != now.strftime("%Y-%m"):
        top = max(days, key=lambda x: x["nk"]) if days else None
        send_push("NK Tegenwind werkt nog",
                  f"Hoogste kans nu: {top['nk']}% op {nl_date(top['date'])}." if top else "Geen data.",
                  "heartbeat")
        state["heartbeat"] = now.strftime("%Y-%m")

    save(DATA, {"updated": now.isoformat(timespec="minutes"), "storm_mode": state["storm_mode"],
                "in_season": in_season(now.date()), "held": state.get("held_season") == season,
                "days": days})
    save(STATE, state)
    print(json.dumps(days[:5]))


if __name__ == "__main__":
    main()
