"""NK Tegenwindfietsen: nieuws en inschrijving in de gaten houden.

Draait elke 15 minuten in GitHub Actions, maar doet alleen echt werk als er
storm in zicht is (state.json -> watch) of om 07.00 uur voor de dagelijkse ronde.
Bronnen: het nieuws via Google News en de officiele site, zodra die weer werkt.
"""
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check import send_push, load, save, nl_date  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "engine", "state.json")
NSTATE = os.path.join(ROOT, "engine", "nieuws_state.json")
OUT = os.path.join(ROOT, "docs", "nieuws.json")
TZ = ZoneInfo("Europe/Amsterdam")
UA = {"User-Agent": "Mozilla/5.0 (NK Tegenwind melder; prive gebruik)"}

NEWS_URL = ("https://news.google.com/rss/search?q=%22NK+Tegenwindfietsen%22+when:7d"
            "&hl=nl&gl=NL&ceid=NL:nl")
SITE_URL = "https://www.nktegenwindfietsen.nl/"

MAANDEN = {m: i + 1 for i, m in enumerate(["januari", "februari", "maart", "april", "mei", "juni", "juli",
                                           "augustus", "september", "oktober", "november", "december"])}
DAGEN = {d: i for i, d in enumerate(["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"])}

RE_OPEN = re.compile(r"inschrijving\w* (is |zijn )?(nu )?(geopend|open)|schrijf (je|jij) (nu )?in|kriehen sturm|"
                     r"startbewij[sz]|gaat (dit weekend |zondag |zaterdag |morgen )?door|we gaan fietsen", re.I)
RE_AANK = re.compile(r"inschrijving|startbewij[sz]|aanmelden|tickets?", re.I)
RE_AFGELAST = re.compile(r"afgelast|gaat niet door|uitgesteld|geannuleerd", re.I)
RE_TIJD = re.compile(r"(?:\b(?:om|vanaf|start om)\s+(\d{1,2})[.:](\d{2}))|(?:\b(\d{1,2})[.:](\d{2})\s*uur)", re.I)
RE_DATUM = re.compile(r"(\d{1,2})\s+(" + "|".join(MAANDEN) + r")", re.I)
RE_DAG = re.compile(r"\b(" + "|".join(DAGEN) + r"|morgen|vandaag)\b", re.I)


def parse_moment(text, now):
    """Zoekt een datum plus tijd in een tekst, zoals 'donderdag om 12.00 uur'."""
    t = RE_TIJD.search(text)
    if not t:
        return None
    hh, mm = (int(t.group(1)), int(t.group(2))) if t.group(1) else (int(t.group(3)), int(t.group(4)))
    if hh > 23 or mm > 59:
        return None
    day = None
    d = RE_DATUM.search(text)
    if d:
        maand = MAANDEN[d.group(2).lower()]
        jaar = now.year + (1 if maand < now.month - 6 else 0)
        try:
            day = date(jaar, maand, int(d.group(1)))
        except ValueError:
            day = None
    if day is None:
        voor = [m for m in RE_DAG.finditer(text) if m.start() < t.start()]
        na = [m for m in RE_DAG.finditer(text) if m.start() >= t.start()]
        w = voor[-1] if voor else (na[0] if na else None)
        if w:
            woord = w.group(1).lower()
            if woord == "vandaag":
                day = now.date()
            elif woord == "morgen":
                day = now.date() + timedelta(days=1)
            else:
                ahead = (DAGEN[woord] - now.weekday()) % 7
                day = now.date() + timedelta(days=ahead)
    if day is None:
        day = now.date()
    moment = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
    if moment < now - timedelta(hours=12):
        return None
    return moment


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_news():
    r = requests.get(NEWS_URL, headers=UA, timeout=30)
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title = clean(it.findtext("title"))
        link = it.findtext("link") or ""
        src = clean(it.findtext("source")) or ""
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ)
        except Exception:
            pub = None
        desc = clean(it.findtext("description"))
        items.append({"id": hashlib.sha1(link.encode()).hexdigest()[:12], "title": title, "link": link,
                      "bron": src, "pub": pub, "tekst": f"{title} {desc}"})
    return items


def fetch_site():
    r = requests.get(SITE_URL, headers=UA, timeout=20)
    r.raise_for_status()
    text = clean(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I))
    links = sorted(set(re.findall(r'https?://(?:www\.)?inschrijven\.nl/[^"\'\s<>]+', r.text)))
    return text, links


def classify(text):
    if RE_AFGELAST.search(text):
        return "afgelast"
    if RE_OPEN.search(text):
        return "open"
    if RE_AANK.search(text):
        return "aankondiging"
    return "nieuws"


def main():
    now = datetime.now(TZ)
    mode = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    st = load(STATE, {})
    ns = load(NSTATE, {"seen": [], "items": [], "site_hash": None, "site_ok": None,
                       "opening_at": None, "reminded": None, "opened_sent": None})
    watch = st.get("watch") or {}
    watching = bool(watch) and watch.get("until", "") >= now.date().isoformat()
    morning = now.hour == 7 and now.minute < 15
    if mode == "scheduled" and not watching and not morning and not ns.get("opening_at"):
        print("Geen storm in zicht, niets te doen.")
        return

    changed = False
    eerste = not ns["seen"]  # eerste run: bestaande artikelen alleen onthouden, niet melden

    # 1. Nieuws
    try:
        for it in fetch_news():
            if it["id"] in ns["seen"]:
                continue
            ns["seen"] = (ns["seen"] + [it["id"]])[-200:]
            changed = True
            if eerste:
                continue
            if it["pub"] and it["pub"] < now - timedelta(days=3):
                continue
            soort = classify(it["tekst"])
            moment = parse_moment(it["tekst"], now)
            entry = {"title": it["title"], "link": it["link"], "bron": it["bron"],
                     "pub": it["pub"].isoformat(timespec="minutes") if it["pub"] else None, "soort": soort}
            ns["items"] = ([entry] + ns["items"])[:10]
            if moment and soort != "afgelast" and moment > now:
                ns["opening_at"] = moment.isoformat(timespec="minutes")
                ns["reminded"] = None
                send_push("NK Tegenwind: inschrijving opent " + nl_date(moment.date().isoformat()),
                          f"Om {moment:%H.%M} uur. Bron: {it['bron'] or 'nieuws'}.", "inschrijving", it["link"])
            elif soort == "open":
                send_push("NK Tegenwind gaat door: schrijf je in",
                          f"{it['title']} ({it['bron']}). Startbewijzen zijn binnen uren weg.", "inschrijving", it["link"])
            elif soort == "afgelast":
                send_push("NK Tegenwind: afgelast of uitgesteld", it["title"], "inschrijving", it["link"])
            elif soort == "aankondiging" or watching:
                send_push("NK Tegenwind: nieuws", f"{it['title']} ({it['bron']})", "nieuws", it["link"])
    except Exception as e:
        print("Nieuws niet opgehaald:", e)

    # 2. Officiele site
    try:
        text, links = fetch_site()
        h = hashlib.sha1(text.encode()).hexdigest()
        if ns.get("site_ok") is not True:
            print("Officiele site is weer bereikbaar.")
        if ns.get("site_hash") and h != ns["site_hash"]:
            soort = classify(text)
            moment = parse_moment(text, now)
            link = links[0] if links else SITE_URL
            if links:
                send_push("NK Tegenwind: inschrijving staat online",
                          "Op de officiele site staat een inschrijflink. Tik om te openen.", "inschrijving", link)
            elif moment and moment > now:
                ns["opening_at"] = moment.isoformat(timespec="minutes")
                ns["reminded"] = None
                send_push("NK Tegenwind: inschrijving opent " + nl_date(moment.date().isoformat()),
                          f"Om {moment:%H.%M} uur, volgens de officiele site.", "inschrijving", SITE_URL)
            elif soort in ("open", "aankondiging", "afgelast"):
                send_push("NK Tegenwind: nieuws op de officiele site", "De site is aangepast. Tik om te kijken.",
                          "nieuws", SITE_URL)
        ns["site_hash"], ns["site_ok"], ns["site_links"] = h, True, links
        changed = True
    except Exception as e:
        if ns.get("site_ok") is not False:
            changed = True
        ns["site_ok"] = False
        print("Officiele site niet bereikbaar:", type(e).__name__)

    # 3. Herinnering voor een aangekondigd moment
    if ns.get("opening_at"):
        at = datetime.fromisoformat(ns["opening_at"])
        if now >= at - timedelta(minutes=75) and now < at and ns.get("reminded") != ns["opening_at"]:
            send_push("NK Tegenwind: inschrijving opent zo", f"Om {at:%H.%M} uur. Zet je gegevens klaar.",
                      "inschrijving", ns.get("site_links", [SITE_URL])[0] if ns.get("site_links") else SITE_URL)
            ns["reminded"] = ns["opening_at"]
            changed = True
        if now >= at and ns.get("opened_sent") != ns["opening_at"]:
            send_push("NK Tegenwind: de inschrijving is nu open", "Snel zijn: vorige keer was het binnen uren vol.",
                      "inschrijving", ns.get("site_links", [SITE_URL])[0] if ns.get("site_links") else SITE_URL)
            ns["opened_sent"] = ns["opening_at"]
            changed = True
        if now > at + timedelta(days=1):
            ns["opening_at"] = None
            changed = True

    save(NSTATE, ns)
    save(OUT, {"updated": now.isoformat(timespec="minutes"), "site_ok": ns.get("site_ok"),
               "opening_at": ns.get("opening_at"), "watch": watch if watching else None,
               "items": ns["items"][:5]})
    print("klaar", "gewijzigd" if changed else "")


if __name__ == "__main__":
    main()
