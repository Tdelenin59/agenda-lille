"""Génère un .ics par source (+ tout.ics) dans public/ à partir de sources.yaml."""
import hashlib
import json
import sys
from datetime import date, datetime, timedelta
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


def walk(node):
    """Parcourt un JSON-LD (dicts, listes, @graph) et renvoie tous les objets."""
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


def events_from_html(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
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
                "url": text(obj.get("url")) or page_url,
            })
    return found


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def to_ical_event(src_id, ev):
    e = Event()
    start, end = ev["start"], ev["end"]
    if isinstance(start, datetime):
        if not isinstance(end, datetime) or end <= start:
            end = start + timedelta(hours=2)
    else:  # journée(s) entière(s) : DTEND exclusif en iCal
        end_day = end.date() if isinstance(end, datetime) else (end or start)
        end = end_day + timedelta(days=1)
    key = f"{src_id}|{ev['name'].lower()}|{start.isoformat()}"
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


def main():
    cfg = yaml.safe_load(Path("sources.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    today = date.today()
    everything = calendar("Sorties Lille (tout)")
    total = 0

    for src in cfg["sources"]:
        events = []
        for url in src.get("urls") or []:
            try:
                found = events_from_html(fetch(url), url)
                print(f"[{src['id']}] {len(found):3d} événements  <- {url}")
                events += found
            except Exception as exc:  # une source en panne ne bloque pas les autres
                print(f"[{src['id']}] ERREUR {exc}  <- {url}")
        for m in cfg.get("manual") or []:
            if m["source"] == src["id"]:
                events.append({
                    "name": m["name"],
                    "start": parse_dt(m["start"]),
                    "end": parse_dt(m.get("end")),
                    "location": m.get("location", ""),
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
    (OUT / "tout.ics").write_bytes(everything.to_ical())
    links = "".join(
        f'<li><a href="{s["id"]}.ics">{s["name"]}</a></li>' for s in cfg["sources"]
    )
    (OUT / "index.html").write_text(
        f'<meta charset="utf-8"><h1>Agenda Lille</h1><ul>{links}'
        f'<li><a href="tout.ics">Tout</a></li></ul>',
        encoding="utf-8",
    )
    print(f"Total : {total} événements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
