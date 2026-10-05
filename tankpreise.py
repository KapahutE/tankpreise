#!/usr/bin/env python3
"""
Tankpreis-Monitor für Uhingen (73066) und Umgebung.

Datenquelle: Tankerkönig-API (Daten der Markttransparenzstelle für Kraftstoffe, MTS-K).
Kostenloser API-Key: https://onboarding.tankerkoenig.de/

Befehle:
  python3 tankpreise.py fetch            # einmal Preise abrufen und speichern
  python3 tankpreise.py report [--days 7] [--end YYYY-MM-DD]
                                         # Wochenauswertung (HTML + Markdown) erzeugen
  python3 tankpreise.py export           # Daten für die iPhone-Web-App (docs/) aktualisieren
  python3 tankpreise.py run              # Dauerbetrieb: stündlich abrufen, montags Report
  python3 tankpreise.py demo             # synthetische Testdaten erzeugen (ohne API)

Gespeichert wird als CSV (eine Datei pro Monat unter data/), damit die Daten auch in
einem Git-Repository (GitHub Actions) sauber versioniert werden können.

Nur Python-Standardbibliothek, keine zusätzlichen Pakete nötig.
"""

import argparse
import configparser
import csv
import glob
import html
import json
import logging
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = "https://creativecommons.tankerkoenig.de/json"
FUELS = ("e5", "e10", "diesel")
FUEL_NAMES = {"e5": "Super E5", "e10": "Super E10", "diesel": "Diesel"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

log = logging.getLogger("tankpreise")


# --------------------------------------------------------------------------- Konfiguration

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"key": "", "lat": "48.7047", "lng": "9.5892", "radius_km": "10"},
        "storage": {"data": "data", "docs": "docs"},
        "schedule": {"report_weekday": "0", "report_hour": "6"},
    })
    cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")
    # Umgebungsvariable hat Vorrang (praktisch für Docker/cron)
    if os.environ.get("TANKERKOENIG_API_KEY"):
        cfg["api"]["key"] = os.environ["TANKERKOENIG_API_KEY"]
    return cfg


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def load_bases(cfg):
    """Feste Orte ("Bases") aus bases.json; ohne Datei gilt der Ort aus config.ini."""
    try:
        with open(resolve("bases.json"), encoding="utf-8") as fh:
            bases = json.load(fh)
    except FileNotFoundError:
        bases = [{"id": "A", "name": "Base", "place": "Uhingen", "lat": float(cfg["api"]["lat"]),
                  "lng": float(cfg["api"]["lng"]), "radius_km": float(cfg["api"]["radius_km"])}]
    for b in bases:
        b["radius_km"] = min(float(b["radius_km"]), 25.0)  # API-Maximum: 25 km
    return bases


def distance_km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def base_dist(base, info):
    if info.get("lat") is None or info.get("lng") is None:
        return None
    return distance_km(base["lat"], base["lng"], info["lat"], info["lng"])


# --------------------------------------------------------------------------- Speicherung (CSV)

CSV_FIELDS = ["ts", "station_id", "e5", "e10", "diesel", "is_open"]


class Store:
    """Monatliche CSV-Dateien data/prices-YYYY-MM.csv plus data/stations.json."""

    def __init__(self, cfg):
        self.dir = resolve(cfg["storage"]["data"])
        os.makedirs(self.dir, exist_ok=True)
        self.stations_file = os.path.join(self.dir, "stations.json")
        self.hours_file = os.path.join(self.dir, "opening_times.json")

    def hours(self):
        try:
            with open(self.hours_file, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}

    def save_hours(self, hours):
        with open(self.hours_file, "w", encoding="utf-8") as fh:
            json.dump(hours, fh, ensure_ascii=False, indent=1, sort_keys=True)

    def stations(self):
        try:
            with open(self.stations_file, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}

    def month_file(self, d):
        return os.path.join(self.dir, f"prices-{d:%Y-%m}.csv")

    def save_snapshot(self, ts, stations):
        """Speichert eine Liste von Tankerkönig-Station-Dicts zum Zeitpunkt ts."""
        ts_s = ts.strftime("%Y-%m-%dT%H:%M:00")
        known = self.stations()
        rows = []
        for s in stations:
            known[s["id"]] = {
                "name": s.get("name"), "brand": s.get("brand"), "street": s.get("street"),
                "house_number": s.get("houseNumber"), "post_code": str(s.get("postCode") or ""),
                "place": s.get("place"), "lat": s.get("lat"), "lng": s.get("lng"),
                "dist_km": s.get("dist"), "last_seen": ts_s,
            }
            p = {f: _price(s.get(f)) for f in FUELS}
            # Geschlossene Stationen liefern i.d.R. keine Preise -> trotzdem Status speichern
            rows.append([ts_s, s["id"]] + ["" if p[f] is None else f"{p[f]:.3f}" for f in FUELS]
                        + [1 if s.get("isOpen") else 0])
        path = self.month_file(ts)
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(CSV_FIELDS)
            w.writerows(rows)
        with open(self.stations_file, "w", encoding="utf-8") as fh:
            json.dump(known, fh, ensure_ascii=False, indent=1, sort_keys=True)
        return len(rows)

    def rows(self, start=None, end=None):
        """Liefert alle Preiszeilen im Zeitraum [start, end) als Dicts."""
        out = []
        for path in sorted(glob.glob(os.path.join(self.dir, "prices-*.csv"))):
            month = os.path.basename(path)[7:14]
            if start and month < f"{start:%Y-%m}" or end and month > f"{end:%Y-%m}":
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    ts = datetime.fromisoformat(r["ts"])
                    if (start and ts < start) or (end and ts >= end):
                        continue
                    row = {"ts": ts, "sid": r["station_id"], "is_open": r["is_open"] == "1"}
                    for f in FUELS:
                        row[f] = float(r[f]) if r[f] else None
                    out.append(row)
        return out


BRAND_FIX = {"ARAL": "Aral", "ESSO": "Esso", "AVIA": "Avia", "AVIA XPRESS": "Avia XPress",
             "AGIP ENI": "Eni", "TS AM E-CENTER": "Tankstelle am E-Center"}
PLACE_FIX = {"goeppingen": "Göppingen", "ebersbach a. d. f.": "Ebersbach a. d. Fils"}


def _tidy(text):
    """GROSSSCHRIFT -> Normalschrift, 'Strasse' -> 'Straße'."""
    text = (text or "").strip()
    if sum(c.isupper() for c in text) > max(3, sum(c.islower() for c in text)):
        text = text.title()
    return re.sub(r"(?i)strasse\b", lambda m: "Straße" if m.group()[0] == "S" else "straße", text)


def nice_station(info):
    """Einheitliche Schreibweise für Marke, Straße und Ort (Rohdaten bleiben unverändert)."""
    info = info or {}
    brand = (info.get("brand") or info.get("name") or "Tankstelle").strip()
    if brand.lower().startswith("freie tankstelle"):
        brand = "Freie Tankstelle"
    brand = BRAND_FIX.get(brand.upper(), brand)
    house = re.sub(r"\s*\([^)]*$", "", (info.get("house_number") or "").strip())
    street = " ".join(x for x in (_tidy(info.get("street")), house) if x)
    place = _tidy(info.get("place"))
    place = PLACE_FIX.get(place.lower(), place)
    return {"brand": brand, "street": street, "place": place}


def station_label(st):
    if not st:
        return "Unbekannte Tankstelle"
    n = nice_station(st)
    return f"{n['brand']} {n['street']}, {n['place']}".strip()


def _price(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# --------------------------------------------------------------------------- Abruf

def _api_key(cfg):
    key = cfg["api"]["key"].strip()
    if not key:
        raise SystemExit(
            "Kein API-Key konfiguriert. Kostenlos registrieren unter "
            "https://onboarding.tankerkoenig.de/ und in config.ini eintragen "
            "oder TANKERKOENIG_API_KEY setzen.")
    return key


def _api_get(endpoint, params, retries=4):
    url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tankpreise-uhingen/1.0"})
    delay = 5
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            if not data.get("ok"):
                raise RuntimeError(data.get("message", "API meldet Fehler"))
            return data
        except Exception as e:  # Netzwerk- oder API-Fehler -> erneut versuchen
            log.warning("Abruf %s fehlgeschlagen (Versuch %d/%d): %s", endpoint, attempt, retries, e)
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2


def fetch(cfg):
    """Ruft alle Bases ab; Tankstellen im Überlappungsbereich zählen nur einmal."""
    merged = {}
    for b in load_bases(cfg):
        params = {"lat": b["lat"], "lng": b["lng"], "rad": b["radius_km"],
                  "sort": "dist", "type": "all", "apikey": _api_key(cfg)}
        for s in _api_get("list.php", params).get("stations", []):
            merged.setdefault(s["id"], s)
    return list(merged.values())


# --------------------------------------------------------------------------- Öffnungszeiten

HOURS_MAX_AGE = timedelta(days=7)   # Öffnungszeiten ändern sich selten
HOURS_PER_RUN = 100                 # Obergrenze an Detail-Abfragen pro Lauf

_DAY_TOKENS = [
    ("montag", 0), ("dienstag", 1), ("mittwoch", 2), ("donnerstag", 3), ("freitag", 4),
    ("samstag", 5), ("sonnabend", 5), ("sonntag", 6), ("sonn", 6),
    ("mo", 0), ("di", 1), ("mi", 2), ("do", 3), ("fr", 4), ("sa", 5), ("so", 6),
]


def parse_days(text):
    """'Mo-Fr' -> [0..4], 'Sa, So' -> [5, 6], 'täglich' -> alle. None, wenn unklar."""
    t = (text or "").lower().replace("ä", "ae")
    if "taeglich" in t or "durchgehend" in t or "mo-so" in t.replace(" ", "").replace(".", ""):
        days = set(range(7))
        if "aus" in t and "sonn" in t:      # "täglich ausser Sonn- und Feiertagen"
            days.discard(6)
        return sorted(days)
    if "werktag" in t:
        return list(range(6))
    found = []  # (position, tag)
    for word, day in _DAY_TOKENS:
        for m in re.finditer(rf"\b{word}", t):
            if not any(a <= m.start() < b for a, b, _ in found):
                found.append((m.start(), m.start() + len(word), day))
    if not found:
        return None
    found.sort()
    days = set()
    for i, (a, b, d) in enumerate(found):
        days.add(d)
        # "Mo-Fr" / "Mo bis Fr": alle Tage dazwischen ergänzen
        if i + 1 < len(found) and re.fullmatch(r"[\s.]*(-|–|bis)[\s.]*", t[b:found[i + 1][0]]):
            nxt = found[i + 1][2]
            k = d
            while k != nxt:
                days.add(k)
                k = (k + 1) % 7
    return sorted(days)


def _hhmm(t):
    return (t or "")[:5]


def normalize_hours(st):
    """Tankerkönig-Detaildaten -> kompaktes Format für App und Speicherung."""
    slots = []
    for o in st.get("openingTimes") or []:
        slots.append({"text": (o.get("text") or "").strip(), "start": _hhmm(o.get("start")),
                      "end": _hhmm(o.get("end")), "days": parse_days(o.get("text"))})
    return {"wholeDay": bool(st.get("wholeDay")), "slots": slots,
            "overrides": [str(x) for x in (st.get("overrides") or [])][:5]}


def update_opening_times(cfg, store, station_ids, now=None):
    """Holt Öffnungszeiten für neue Tankstellen und frischt alte wöchentlich auf."""
    now = now or datetime.now()
    hours = store.hours()
    due = [sid for sid in station_ids
           if sid not in hours or now - datetime.fromisoformat(hours[sid]["fetched"]) > HOURS_MAX_AGE]
    # Älteste zuerst, damit sich die Abfragen über die Woche verteilen
    due.sort(key=lambda sid: hours.get(sid, {}).get("fetched", ""))
    done = 0
    for sid in due[:HOURS_PER_RUN]:
        try:
            st = _api_get("detail.php", {"id": sid, "apikey": _api_key(cfg)}, retries=2).get("station") or {}
        except Exception as e:
            log.warning("Öffnungszeiten für %s nicht abrufbar: %s", sid, e)
            continue
        hours[sid] = {"fetched": now.isoformat(timespec="minutes"), **normalize_hours(st)}
        done += 1
        time.sleep(0.5)
    if done:
        store.save_hours(hours)
        log.info("Öffnungszeiten für %d Tankstellen aktualisiert.", done)


def cmd_fetch(cfg):
    stations = fetch(cfg)
    store = Store(cfg)
    n = store.save_snapshot(datetime.now(), stations)
    log.info("%d Tankstellen gespeichert.", n)
    # Fehler bei den Öffnungszeiten dürfen den Preisabruf nie scheitern lassen
    try:
        update_opening_times(cfg, store, [s["id"] for s in stations])
    except Exception as e:
        log.warning("Öffnungszeiten übersprungen: %s", e)


# --------------------------------------------------------------------------- Auswertung

def load_week(store, start, end, base=None):
    """Preiszeilen im Zeitraum; mit base nur Tankstellen im Umkreis dieser Base."""
    st = store.stations()
    out = []
    for r in store.rows(start, end):
        info = st.get(r["sid"], {})
        r["dist"] = base_dist(base, info) if base else info.get("dist_km")
        if base and (r["dist"] is None or r["dist"] > base["radius_km"] + 0.05):
            continue
        r["label"] = station_label(info)
        out.append(r)
    return out


def analyse(rows):
    """Berechnet alle Kennzahlen je Kraftstoff."""
    res = {}
    for f in FUELS:
        vals = [r for r in rows if r[f] is not None]
        if not vals:
            continue
        prices = [r[f] for r in vals]

        by_station = defaultdict(list)
        by_hour = defaultdict(list)
        by_wd = defaultdict(list)
        by_day = defaultdict(list)
        ts_mean = defaultdict(list)
        for r in vals:
            by_station[r["sid"]].append(r)
            by_day[r["ts"].date()].append(r[f])
            ts_mean[r["ts"]].append(r[f])

        # Tageszeit/Wochentag relativ zum jeweiligen Tagesmittel der Station bewerten,
        # damit Stationen mit generell hohem Preisniveau das Ergebnis nicht verzerren.
        st_day_mean = {}
        for sid, rs in by_station.items():
            d = defaultdict(list)
            for r in rs:
                d[r["ts"].date()].append(r[f])
            st_day_mean[sid] = {k: statistics.mean(v) for k, v in d.items()}
        st_mean = {sid: statistics.mean(r[f] for r in rs) for sid, rs in by_station.items()}
        for r in vals:
            by_hour[r["ts"].hour].append(r[f] - st_day_mean[r["sid"]][r["ts"].date()])
            by_wd[r["ts"].weekday()].append(r[f] - st_mean[r["sid"]])

        stations = []
        for sid, rs in by_station.items():
            ps = [r[f] for r in rs]
            changes = sum(1 for a, b in zip(ps, ps[1:]) if abs(a - b) > 1e-9)
            stations.append({
                "sid": sid, "label": rs[0]["label"], "dist": rs[0]["dist"],
                "mean": statistics.mean(ps), "min": min(ps), "max": max(ps),
                "n": len(ps), "changes": changes,
            })
        stations.sort(key=lambda s: s["mean"])

        cheapest = min(vals, key=lambda r: r[f])
        priciest = max(vals, key=lambda r: r[f])
        hour_avg = {h: statistics.mean(v) for h, v in sorted(by_hour.items())}
        wd_avg = {w: statistics.mean(v) for w, v in sorted(by_wd.items())}

        res[f] = {
            "mean": statistics.mean(prices),
            "median": statistics.median(prices),
            "min": cheapest[f], "min_at": cheapest["ts"], "min_station": cheapest["label"],
            "max": priciest[f], "max_at": priciest["ts"], "max_station": priciest["label"],
            "stations": stations,
            "hour_avg": hour_avg,
            "best_hour": min(hour_avg, key=hour_avg.get),
            "worst_hour": max(hour_avg, key=hour_avg.get),
            "wd_avg": wd_avg,
            "best_wd": min(wd_avg, key=wd_avg.get) if len(wd_avg) > 1 else None,
            "day_avg": {d: statistics.mean(v) for d, v in sorted(by_day.items())},
            "series": [(t, statistics.mean(v)) for t, v in sorted(ts_mean.items())],
            "n": len(prices),
        }
    return res


# --------------------------------------------------------------------------- Report-Ausgabe

def eur(v, sign=False):
    if v is None:
        return "–"
    s = f"{v:+.3f}" if sign else f"{v:.3f}"
    return s.replace(".", ",") + " €"


def ct(v):
    return f"{v * 100:+.1f} ct".replace(".", ",")


def svg_line(series, width=720, height=220):
    if len(series) < 2:
        return "<p><em>Zu wenige Datenpunkte für einen Verlauf.</em></p>"
    pad_l, pad_r, pad_t, pad_b = 52, 12, 12, 28
    t0, t1 = series[0][0].timestamp(), series[-1][0].timestamp()
    vs = [v for _, v in series]
    lo, hi = min(vs), max(vs)
    if hi - lo < 0.01:
        lo, hi = lo - 0.005, hi + 0.005
    X = lambda t: pad_l + (t.timestamp() - t0) / (t1 - t0 or 1) * (width - pad_l - pad_r)
    Y = lambda v: pad_t + (hi - v) / (hi - lo) * (height - pad_t - pad_b)
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in series)
    grid = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = Y(v)
        grid.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                    f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{v:.3f}</text>')
    day = series[0][0].replace(hour=0, minute=0, second=0)
    while day <= series[-1][0]:
        if day >= series[0][0]:
            x = X(day)
            grid.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{pad_t}" y2="{height - pad_b}" class="grid"/>'
                        f'<text x="{x + 3:.1f}" y="{height - 10}">{WEEKDAYS[day.weekday()]} {day:%d.%m.}</text>')
        day += timedelta(days=1)
    return (f'<svg viewBox="0 0 {width} {height}" class="chart">{"".join(grid)}'
            f'<polyline points="{pts}" class="line"/></svg>')


def svg_bars(values, labels, width=720, height=200):
    """Balken für Abweichungen (positiv = teurer, negativ = günstiger)."""
    if not values:
        return ""
    pad_l, pad_r, pad_t, pad_b = 52, 12, 12, 24
    m = max(abs(v) for v in values) or 0.001
    zero = pad_t + (height - pad_t - pad_b) / 2
    scale = (height - pad_t - pad_b) / 2 / m
    bw = (width - pad_l - pad_r) / len(values)
    parts = [f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{zero}" y2="{zero}" class="axis"/>',
             f'<text x="{pad_l - 6}" y="{pad_t + 8}" text-anchor="end">{ct(m)}</text>',
             f'<text x="{pad_l - 6}" y="{height - pad_b}" text-anchor="end">{ct(-m)}</text>']
    vmin = min(values)
    for i, (v, lab) in enumerate(zip(values, labels)):
        x = pad_l + i * bw + bw * 0.15
        h = abs(v) * scale
        y = zero - h if v > 0 else zero
        cls = "best" if v == vmin else ("up" if v > 0 else "down")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw * 0.7:.1f}" height="{max(h, 0.5):.1f}" '
                     f'class="{cls}"><title>{lab}: {ct(v)}</title></rect>'
                     f'<text x="{x + bw * 0.35:.1f}" y="{height - 6}" text-anchor="middle">{lab}</text>')
    return f'<svg viewBox="0 0 {width} {height}" class="chart">{"".join(parts)}</svg>'


CSS = """
:root{--bg:#fff;--fg:#1d2329;--muted:#66707a;--card:#f5f7f9;--line:#d9dee3;--acc:#2563eb;--good:#16a34a;--bad:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#121619;--fg:#e6e9ec;--muted:#9aa4ad;--card:#1b2126;--line:#2d353c;--acc:#60a5fa;--good:#4ade80;--bad:#f87171}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:920px;margin:0 auto;padding:16px}
h1{margin-bottom:0} .sub{color:var(--muted);margin-top:4px}
section{background:var(--card);border-radius:10px;padding:16px;margin:20px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi b{display:block;font-size:22px} .kpi span{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px} th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.chart{width:100%;height:auto} .chart text{fill:var(--muted);font-size:11px}
.grid{stroke:var(--line);stroke-width:1} .axis{stroke:var(--muted)}
.line{fill:none;stroke:var(--acc);stroke-width:2}
.up{fill:var(--bad);opacity:.6} .down{fill:var(--good);opacity:.6} .best{fill:var(--good)}
.good{color:var(--good)} .bad{color:var(--bad)}
.tablewrap{overflow-x:auto}
"""


def render_report(res, prev, start, end, base):
    title = f"Tankpreise {base['name']} ({base['place']}) – KW {start.isocalendar()[1]}/{start.year}"
    period = f"{start:%d.%m.%Y} – {(end - timedelta(seconds=1)):%d.%m.%Y}"
    area = f"Umkreis {base['radius_km']:g} km um {base['place']}"
    md = [f"# {title}", "", f"Zeitraum: {period} · {area}", ""]
    h = [f"<!doctype html><html lang='de'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
         f"<h1>{html.escape(title)}</h1><p class='sub'>Zeitraum: {period} · {html.escape(area)}</p>"]

    if not res:
        h.append("<p>Keine Daten im Zeitraum vorhanden.</p></body></html>")
        md.append("Keine Daten im Zeitraum vorhanden.")
        return "".join(h), "\n".join(md)

    for f, r in res.items():
        name = FUEL_NAMES[f]
        p = prev.get(f)
        trend = ""
        trend_md = ""
        if p:
            d = r["mean"] - p["mean"]
            cls = "bad" if d > 0 else "good"
            trend = f"<span class='{cls}'>{ct(d)} ggü. Vorwoche</span>"
            trend_md = f" ({ct(d)} ggü. Vorwoche)"
        best_st = r["stations"][0]
        hour_vals = [r["hour_avg"].get(hh) for hh in range(24)]
        hours_present = [hh for hh in range(24) if hour_vals[hh] is not None]

        h.append(f"<section><h2>{name}</h2><div class='kpis'>"
                 f"<div class='kpi'><b>{eur(r['mean'])}</b><span>Ø Preis {trend}</span></div>"
                 f"<div class='kpi'><b class='good'>{eur(r['min'])}</b><span>Tiefstpreis: "
                 f"{html.escape(r['min_station'])}, {WEEKDAYS[r['min_at'].weekday()]} {r['min_at']:%d.%m. %H:%M}</span></div>"
                 f"<div class='kpi'><b class='bad'>{eur(r['max'])}</b><span>Höchstpreis: "
                 f"{html.escape(r['max_station'])}, {WEEKDAYS[r['max_at'].weekday()]} {r['max_at']:%d.%m. %H:%M}</span></div>"
                 f"<div class='kpi'><b>{r['best_hour']:02d}–{r['best_hour'] + 1:02d} Uhr</b>"
                 f"<span>Günstigste Tageszeit (Ø {ct(r['hour_avg'][r['best_hour']])} ggü. Tagesmittel)</span></div>"
                 f"</div>")
        h.append("<h3>Durchschnittspreis im Wochenverlauf</h3>" + svg_line(r["series"]))
        h.append("<h3>Abweichung nach Tageszeit</h3>"
                 + svg_bars([r["hour_avg"][hh] for hh in hours_present], [str(hh) for hh in hours_present]))
        if r["best_wd"] is not None:
            wds = sorted(r["wd_avg"])
            h.append("<h3>Abweichung nach Wochentag</h3>"
                     + svg_bars([r["wd_avg"][w] for w in wds], [WEEKDAYS[w] for w in wds], height=160))
        h.append("<h3>Tankstellen-Ranking (nach Ø Preis)</h3><div class='tablewrap'><table><tr><th>#</th>"
                 "<th>Tankstelle</th><th class='n'>km</th><th class='n'>Ø</th><th class='n'>Min</th>"
                 "<th class='n'>Max</th><th class='n'>Änderungen*</th></tr>")
        for i, s in enumerate(r["stations"], 1):
            h.append(f"<tr><td>{i}</td><td>{html.escape(s['label'])}</td>"
                     f"<td class='n'>{(s['dist'] or 0):.1f}</td><td class='n'>{eur(s['mean'])}</td>"
                     f"<td class='n'>{eur(s['min'])}</td><td class='n'>{eur(s['max'])}</td>"
                     f"<td class='n'>{s['changes']}</td></tr>")
        h.append("</table></div><p class='sub'>* beobachtete Preisänderungen zwischen stündlichen "
                 "Abrufen (tatsächliche Anzahl ist meist höher).</p></section>")

        md += [f"## {name}", "",
               f"- Ø Preis: **{eur(r['mean'])}**{trend_md}, Median {eur(r['median'])}",
               f"- Tiefstpreis: **{eur(r['min'])}** bei {r['min_station']} "
               f"({WEEKDAYS[r['min_at'].weekday()]} {r['min_at']:%d.%m. %H:%M})",
               f"- Höchstpreis: {eur(r['max'])} bei {r['max_station']} "
               f"({WEEKDAYS[r['max_at'].weekday()]} {r['max_at']:%d.%m. %H:%M})",
               f"- Günstigste Tageszeit: **{r['best_hour']:02d}–{r['best_hour'] + 1:02d} Uhr** "
               f"({ct(r['hour_avg'][r['best_hour']])}), teuerste: {r['worst_hour']:02d}–{r['worst_hour'] + 1:02d} Uhr "
               f"({ct(r['hour_avg'][r['worst_hour']])})"]
        if r["best_wd"] is not None:
            md.append(f"- Günstigster Wochentag: {WEEKDAYS[r['best_wd']]} ({ct(r['wd_avg'][r['best_wd']])})")
        md.append(f"- Günstigste Tankstelle im Schnitt: **{best_st['label']}** ({eur(best_st['mean'])}, "
                  f"{(best_st['dist'] or 0):.1f} km)")
        md += ["", "| # | Tankstelle | km | Ø | Min | Max |", "|---|---|---:|---:|---:|---:|"]
        for i, s in enumerate(r["stations"][:10], 1):
            md.append(f"| {i} | {s['label']} | {(s['dist'] or 0):.1f} | {eur(s['mean'])} | "
                      f"{eur(s['min'])} | {eur(s['max'])} |")
        md.append("")

    h.append("<p class='sub'>Datenquelle: Tankerkönig / MTS-K (CC BY 4.0). Erstellt am "
             f"{datetime.now():%d.%m.%Y %H:%M}.</p></body></html>")
    md.append("Datenquelle: Tankerkönig / MTS-K (CC BY 4.0)")
    return "".join(h), "\n".join(md)


def cmd_report(cfg, days=7, end=None):
    """Wochenreport je Base unter docs/reports/<Base-ID>/."""
    if end is None:  # Standard: letzte vollständige Woche bis heute 00:00
        end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    store = Store(cfg)
    y, w, _ = start.isocalendar()
    for b in load_bases(cfg):
        res = analyse(load_week(store, start, end, b))
        prev = analyse(load_week(store, start - timedelta(days=days), start, b))
        html_s, md_s = render_report(res, prev, start, end, b)
        out_dir = os.path.join(resolve(cfg["storage"]["docs"]), "reports", b["id"])
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"report_{y}-KW{w:02d}")
        with open(path + ".html", "w", encoding="utf-8") as fh:
            fh.write(html_s)
        with open(path + ".md", "w", encoding="utf-8") as fh:
            fh.write(md_s)
        log.info("Report geschrieben: %s.html / .md", path)
        print(md_s, "\n")
    cmd_export(cfg)


# --------------------------------------------------------------------------- Export für die Web-App

def _cents(v):
    """2.109 -> 2109 (spart in der App-Datei gut ein Drittel Platz)."""
    return None if v is None else round(v * 1000)


def cmd_export(cfg):
    """Schreibt die Daten für die Web-App: docs/data/bases.json und je Base
    docs/data/<ID>/latest.json, history.json und reports.json."""
    store = Store(cfg)
    docs = resolve(cfg["storage"]["docs"])
    st = store.stations()
    hours = store.hours()
    now = datetime.now()
    bases = load_bases(cfg)

    rows14 = store.rows(now - timedelta(days=14))
    last_ts = max((r["ts"] for r in rows14), default=None)
    week_start = now - timedelta(days=7)
    times = sorted({r["ts"] for r in rows14 if r["ts"] >= week_start})
    idx = {t: i for i, t in enumerate(times)}

    index = []
    for b in bases:
        inside = {sid for sid, info in st.items()
                  if (d := base_dist(b, info)) is not None and d <= b["radius_km"] + 0.05}
        out_dir = os.path.join(docs, "data", b["id"])
        os.makedirs(out_dir, exist_ok=True)

        # Aktuelle Preise: letzter Abruf
        latest = {"updated": last_ts.isoformat() if last_ts else None, "stations": []}
        for r in rows14:
            if r["ts"] != last_ts or r["sid"] not in inside:
                continue
            info = st[r["sid"]]
            latest["stations"].append({
                "id": r["sid"], **nice_station(info),
                "lat": info.get("lat"), "lng": info.get("lng"),
                "open": r["is_open"], **{f: r[f] for f in FUELS},
                "hours": {k: v for k, v in hours[r["sid"]].items() if k != "fetched"}
                if r["sid"] in hours else None,
            })

        # Verlauf der letzten 7 Tage je Tankstelle (Preise in 1/1000 €) + Ø der Vorwoche
        series, prev_sum = {}, {}
        for r in rows14:
            if r["sid"] not in inside:
                continue
            if r["ts"] >= week_start:
                d = series.setdefault(r["sid"], {f: [None] * len(times) for f in FUELS})
                for f in FUELS:
                    if r["is_open"] and r[f] is not None:
                        d[f][idx[r["ts"]]] = _cents(r[f])
            else:
                p = prev_sum.setdefault(r["sid"], {f: [] for f in FUELS})
                for f in FUELS:
                    if r["is_open"] and r[f] is not None:
                        p[f].append(r[f])
        history = {
            "updated": latest["updated"],
            "ts": [t.isoformat(timespec="minutes") for t in times],
            "stations": series,
            "prev": {sid: {f: _cents(statistics.mean(v)) for f, v in p.items() if v}
                     for sid, p in prev_sum.items()},
        }

        reports = []
        for path in sorted(glob.glob(os.path.join(docs, "reports", b["id"], "report_*.html")), reverse=True):
            name = os.path.basename(path)
            reports.append({"file": f"reports/{b['id']}/{name}",
                            "title": name[7:-5].replace("-KW", " · KW ")})

        for fname, obj in (("latest.json", latest), ("history.json", history),
                           ("reports.json", reports)):
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        index.append({**b, "stations": len(latest["stations"]), "updated": latest["updated"]})
        log.info("%s (%s): %d Tankstellen exportiert.", b["name"], b["place"], len(latest["stations"]))

    with open(os.path.join(docs, "data", "bases.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    # Dateien des alten Ein-Ort-Formats entfernen
    for old in ("latest.json", "summary.json", "history.json", "reports.json"):
        try:
            os.remove(os.path.join(docs, "data", old))
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- Dauerbetrieb

def cmd_run(cfg):
    """Einfacher Scheduler: jede volle Stunde abrufen, wöchentlich Report erstellen."""
    wd = int(cfg["schedule"]["report_weekday"])
    rh = int(cfg["schedule"]["report_hour"])
    log.info("Dauerbetrieb gestartet (Abruf stündlich, Report %s %02d:00 Uhr).", WEEKDAYS[wd], rh)
    while True:
        now = datetime.now()
        try:
            cmd_fetch(cfg)
            cmd_export(cfg)
        except Exception as e:
            log.error("Abruf fehlgeschlagen: %s", e)
        if now.weekday() == wd and now.hour == rh:
            try:
                cmd_report(cfg)
            except Exception as e:
                log.error("Report fehlgeschlagen: %s", e)
        nxt = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=30, microsecond=0)
        time.sleep(max(1, (nxt - datetime.now()).total_seconds()))


# --------------------------------------------------------------------------- Demo-Daten

def cmd_demo(cfg, weeks=2):
    """Erzeugt realistisch wirkende Testdaten, um die Auswertung ohne API-Key zu prüfen."""
    rnd = random.Random(42)
    brands = ["Aral", "Shell", "JET", "Esso", "Avia", "TotalEnergies", "bft", "Freie Tankstelle", "Eni", "MTB"]
    stations = []
    for b in load_bases(cfg):
        for i in range(14):
            ang, r = rnd.uniform(0, 2 * math.pi), b["radius_km"] * math.sqrt(rnd.random())
            lat = b["lat"] + r * math.cos(ang) / 111.0
            lng = b["lng"] + r * math.sin(ang) / (111.0 * math.cos(b["lat"] * math.pi / 180))
            brand = rnd.choice(brands)
            stations.append({"id": f"demo-{b['id']}-{i}", "name": f"{brand} {b['place']}", "brand": brand,
                             "street": rnd.choice(["Hauptstr.", "Stuttgarter Str.", "Ulmer Str.", "Bahnhofstr."]),
                             "houseNumber": str(rnd.randint(1, 120)), "postCode": "70000",
                             "place": f"{b['place']}-Umgebung", "lat": lat, "lng": lng, "dist": r,
                             "off": rnd.choice([-0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.04])})
    store = Store(cfg)
    end = datetime.now().replace(minute=0, second=0, microsecond=0)
    t = end - timedelta(weeks=weeks)
    base = {"e5": 1.78, "e10": 1.72, "diesel": 1.66}
    while t < end:
        # Typisches Muster: morgens teuer, abends (18–22 Uhr) günstig, leichter Wochentrend
        h = t.hour
        daily = 0.06 * math.exp(-((h - 7) ** 2) / 6) - 0.05 * math.exp(-((h - 20) ** 2) / 5)
        trend = 0.01 * math.sin((t - end).total_seconds() / 86400 / 4)
        snap = []
        for s in stations:
            st = dict(s)
            st["isOpen"] = 6 <= h <= 21 or s["brand"] in ("Aral", "Shell")
            for f in FUELS:
                p = base[f] + daily + trend + s["off"] + rnd.choice([0, 0, 0.01, -0.01])
                st[f] = round(p, 2) + 0.009 if st["isOpen"] else None
            snap.append(st)
        store.save_snapshot(t, snap)
        t += timedelta(hours=1)
    log.info("Demo-Daten für %d Wochen erzeugt.", weeks)


# --------------------------------------------------------------------------- CLI

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Tankpreis-Monitor Uhingen (73066)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="Preise einmalig abrufen und speichern")
    rp = sub.add_parser("report", help="Wochenauswertung erstellen")
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--end", help="Ende des Zeitraums (exklusiv), YYYY-MM-DD; Standard: heute")
    sub.add_parser("export", help="Daten für die Web-App (docs/data) aktualisieren")
    sub.add_parser("run", help="Dauerbetrieb (stündlicher Abruf + Wochenreport)")
    dp = sub.add_parser("demo", help="Testdaten erzeugen")
    dp.add_argument("--weeks", type=int, default=2)
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "fetch":
        cmd_fetch(cfg)
    elif a.cmd == "report":
        end = datetime.strptime(a.end, "%Y-%m-%d") if a.end else None
        cmd_report(cfg, a.days, end)
    elif a.cmd == "export":
        cmd_export(cfg)
    elif a.cmd == "run":
        cmd_run(cfg)
    elif a.cmd == "demo":
        cmd_demo(cfg, a.weeks)


if __name__ == "__main__":
    sys.exit(main())
