"""Génère un .ics par source (+ tout.ics) dans public/ à partir de sources.yaml."""
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from icalendar import Calendar, Event

TZ = ZoneInfo("Europe/Paris")
OUT = Path("public")
HEADERS = {"User-Agent": "Mozilla/5.0 (agenda-lille perso; GitHub Actions)"}
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
        "aout", "septembre", "octobre", "novembre", "decembre"]
MOIS_RE = "|".join(MOIS)


def norm(s):
    """Minuscules sans accents, pour comparer « Siècle » et « Siecle »."""
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return s.lower()


# ---------- Parseur générique : événements schema.org (JSON-LD) ----------

def walk(node):
    if isinstance(node, list):
        for x in node:
            yield from walk(x)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from walk(v)


def is_event(obj):
    t = obj.get("@type", "")
    types = t if isinstance(t, list) else [t]
    return any(str(x).endswith("Event") for x in types) and "startDate" in obj


def parse_dt(value):
    """'2026-11-20' -> date (journée entière) ; avec heure -> datetime Europe/Paris."""
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value
    value = str(value).strip()
    if len(value) == 10:
        return date.fromisoformat(value)
    dt = dateparser.isoparse(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def text(v):
    if isinstance(v, dict):
        return v.get("name") or ""
    if isinstance(v, list):
        return ", ".join(text(x) for x in v if text(x))
    return str(v or "")


def city_of(loc):
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") if isinstance(loc, dict) else None
    return addr.get("addressLocality", "") if isinstance(addr, dict) else ""


def parse_jsonld(soup, page_url):
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for obj in walk(data):
            if not is_event(obj):
                continue
            start = parse_dt(obj.get("startDate"))
            if start is None:
                continue
            found.append({
                "name": text(obj.get("name")).strip() or "Événement",
                "start": start,
                "end": parse_dt(obj.get("endDate")),
                "location": text(obj.get("location")),
                "city": city_of(obj.get("location")),
                "url": text(obj.get("url")) or page_url,
            })
    return found


# ---------- Parseurs dédiés (dates écrites en français) ----------

def fr_datetime(day, month_name, year, hour, minute):
    month = MOIS.index(norm(month_name)) + 1
    if year is None:  # pas d'année affichée : on prend la prochaine occurrence
        today = date.today()
        year = today.year + (1 if month < today.month - 1 else 0)
    return datetime(int(year), month, int(day), int(hour), int(minute or 0), tzinfo=TZ)


def parse_spotlight(soup, page_url):
    """Cartes : <h2>Titre</h2> puis « 05 novembre - 19h » et un lien Mapado."""
    date_re = re.compile(rf"(\d{{1,2}})\s+({MOIS_RE})\s*-\s*(\d{{1,2}})h(\d{{2}})?")
    found = []
    for h2 in soup.find_all("h2"):
        buf, link = "", None
        for el in h2.next_elements:
            if getattr(el, "name", None) == "h2":
                break
            if getattr(el, "name", None) == "a" and link is None and "mapado" in el.get("href", ""):
                link = el["href"]
            if isinstance(el, str):
                buf += " " + el
        m = date_re.search(norm(buf))
        if not m:
            continue
        found.append({
            "name": h2.get_text(" ", strip=True),
            "start": fr_datetime(m[1], m[2], None, m[3], m[4]),
            "end": None,
            "location": "Le Spotlight, 100 rue Léon Gambetta, Lille",
            "city": "Lille",
            "url": link or page_url,
        })
    return found


def parse_lillarious(soup, page_url):
    """Liens /events/ : « 29 janvier 2027 - 19:00 Titre Lieu, Lille »."""
    date_re = re.compile(rf"^(\d{{1,2}})(?:er)?\s+({MOIS_RE})\s+(\d{{4}})\s*-\s*(\d{{1,2}}):(\d{{2}})")
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "/events/" not in a["href"] or a["href"] in seen:
            continue
        parts = list(a.stripped_strings)
        m = date_re.match(norm(" ".join(parts)))
        if not m:
            continue
        seen.add(a["href"])
        if len(parts) >= 3:  # [date, titre, lieu] quand le HTML les sépare
            name, location = parts[1], ", ".join(parts[2:])
        else:
            name = " ".join(parts)[m.end():].strip() if len(parts) == 1 else parts[1]
            location = ""
        found.append({
            "name": f"Lillarious – {name}",
            "start": fr_datetime(m[1], m[2], m[3], m[4], m[5]),
            "end": None,
            "location": location,
            "city": "Lille",
            "url": requests.compat.urljoin(page_url, a["href"]),
        })
    return found


MOIS_ACC = MOIS_RE + "|février|août|décembre"
AERO_DATE = re.compile(rf"(\d{{1,2}})\s*({MOIS_ACC})\D*?(\d{{4}})(?:\s+(\d{{1,2}}):(\d{{2}}))?", re.I)
AERO_TAIL = re.compile(r"\s*(Bientôt complet\s*!?|Complet|Annulé|Reporté)?\s*(Billets|Plus d'infos)?\s*Go\s*!?\s*$", re.I)


def parse_aeronef(soup, page_url):
    """Liens /agenda/<slug> : « Club dimanche Dim. 08 novembre Nov. 2026 18:30 TITRE Billets Go ! »."""
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(page_url, a["href"])
        if not re.search(r"aeronef\.fr/agenda/[^/?#]+$", href) or href in seen:
            continue
        txt = " ".join(a.stripped_strings)
        dates = list(AERO_DATE.finditer(txt))
        if not dates:
            continue
        seen.add(href)
        first, last = dates[0], dates[-1]
        if first[4]:  # date + heure
            start, end = fr_datetime(first[1], first[2], first[3], first[4], first[5]), None
        else:  # exposition sur plusieurs jours : journées entières
            start = fr_datetime(first[1], first[2], first[3], 0, 0).date()
            end = fr_datetime(last[1], last[2], last[3], 0, 0).date()
        name = AERO_TAIL.sub("", txt[last.end():]).split(" Avec :")[0].strip() or "Événement"
        hors_murs = "hors les murs" in norm(txt[:first.start()])
        found.append({
            "name": name, "start": start, "end": end,
            "location": "Hors les murs (Aéronef)" if hors_murs else "L'Aéronef, Lille",
            "city": "Lille", "url": href,
        })
    return found


PARSERS = {"jsonld": parse_jsonld, "aeronef": parse_aeronef, "spotlight": parse_spotlight, "lillarious": parse_lillarious}


@lru_cache(maxsize=None)  # une page partagée par plusieurs sources n'est lue qu'une fois
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def keep(ev, src):
    """Filtres optionnels : venues (lieu contient…), exclude_venues, cities."""
    loc = norm(ev.get("location"))
    if src.get("venues") and not any(norm(v) in loc for v in src["venues"]):
        return False
    if any(norm(v) in loc for v in src.get("exclude_venues") or []):
        return False
    if src.get("cities") and norm(ev.get("city")) not in {norm(c) for c in src["cities"]}:
        return False
    return True


# ---------- Écriture iCalendar ----------

def to_ical_event(src_id, ev):
    e = Event()
    start, end = ev["start"], ev["end"]
    if isinstance(start, datetime):
        if not isinstance(end, datetime) or end <= start:
            end = start + timedelta(hours=2)
    else:  # journée(s) entière(s) : DTEND exclusif en iCal
        end_day = end.date() if isinstance(end, datetime) else (end or start)
        end = end_day + timedelta(days=1)
    key = f"{src_id}|{norm(ev['name'])}|{start.isoformat()}"
    e.add("uid", hashlib.sha1(key.encode()).hexdigest() + "@agenda-lille")
    e.add("summary", ev["name"])
    e.add("dtstart", start)
    e.add("dtend", end)
    e.add("dtstamp", datetime.now(TZ))
    if ev.get("location"):
        e.add("location", ev["location"])
    if ev.get("url"):
        e.add("url", ev["url"])
        e.add("description", ev["url"])
    return key, e


def calendar(name):
    cal = Calendar()
    cal.add("prodid", "-//agenda-lille//FR")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "Europe/Paris")
    return cal


def source_urls(src):
    urls = []
    for u in src.get("urls") or []:  # aplatit les listes partagées (*metro)
        urls += u if isinstance(u, list) else [u]
    pages = src.get("pages")  # {"url": ".../page/{n}/", "max": 15}
    if pages:
        urls += [pages["url"].format(n=n) for n in range(pages.get("start", 1), pages.get("max", 10) + 1)]
    return urls


def main():
    cfg = yaml.safe_load(Path("sources.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    today = date.today()
    everything = calendar("Sorties Lille")
    total = 0

    for src in cfg["sources"]:
        parse = PARSERS[src.get("parser", "jsonld")]
        events = []
        for url in source_urls(src):
            try:
                found = parse(BeautifulSoup(fetch(url), "html.parser"), url)
            except Exception as exc:  # une page en panne ne bloque pas le reste
                print(f"[{src['id']}] ERREUR {str(exc)[:80]}  <- {url}")
                if src.get("pages"):
                    break  # fin de la pagination
                continue
            kept = [e for e in found if keep(e, src)]
            print(f"[{src['id']}] {len(kept):3d}/{len(found):3d} gardés  <- {url}")
            if src.get("pages") and not found:
                break
            events += kept
        for m in cfg.get("manual") or []:
            if m["source"] == src["id"]:
                events.append({
                    "name": m["name"], "start": parse_dt(m["start"]),
                    "end": parse_dt(m.get("end")), "location": m.get("location", ""),
                    "url": m.get("url", ""),
                })

        cal, seen = calendar(src["name"]), set()
        for ev in events:
            day = ev["start"].date() if isinstance(ev["start"], datetime) else ev["start"]
            if day < today - timedelta(days=30):
                continue
            key, ical_ev = to_ical_event(src["id"], ev)
            if key in seen:
                continue
            seen.add(key)
            cal.add_component(ical_ev)
            everything.add_component(ical_ev)
        cal.add_missing_timezones()
        (OUT / f"{src['id']}.ics").write_bytes(cal.to_ical())
        print(f"[{src['id']}] => {len(seen)} dans {src['id']}.ics")
        total += len(seen)

    everything.add_missing_timezones()
    (OUT / "sorties-lille.ics").write_bytes(everything.to_ical())
    links = "".join(
        f'<li><a href="{s["id"]}.ics">{s["name"]}</a></li>' for s in cfg["sources"]
    )
    (OUT / "index.html").write_text(
        f'<meta charset="utf-8"><h1>Agenda Lille</h1><ul>{links}'
        f'<li><a href="sorties-lille.ics">Sorties Lille (tout)</a></li></ul>',
        encoding="utf-8",
    )
    print(f"Total : {total} événements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
