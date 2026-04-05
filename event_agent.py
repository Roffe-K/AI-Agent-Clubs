"""
PartyPrep Event Agent
=====================
Uppdaterar BARA events för klubbar som redan finns i nightclubs.json.
Mycket snabbare och billigare än att köra hela nightclub_agent.py.

Pipeline:
  1. Läser nightclubs.json (redan scrapad klubbdata)
  2. För varje klubb – söker kommande events via Tickster, Dice, Billetto, RA
  3. Filtrerar bort gamla events
  4. Sparar uppdaterad nightclubs.json

Kör varje måndag via GitHub Actions.

Krav (GitHub Secrets):
  SERPER_API_KEY  – Serper.dev API-nyckel
  CLAUDE_API_KEY  – Anthropic API-nyckel
"""

import os
import json
import time
import requests
from datetime import datetime
from bs4 import BeautifulSoup
import anthropic

# ─────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "DIN_SERPER_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "DIN_CLAUDE_API_KEY")

MASTER_FILE = "nightclubs_master.json"  # Alla klubbar
IMPORT_FILE = "nightclubs_events.json"   # Import-fil med uppdaterade events

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

TODAY = datetime.now().strftime("%Y-%m-%d")

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


# ─────────────────────────────────────────────
# SERPER
# ─────────────────────────────────────────────

def serper_search(query: str, num: int = 8) -> list:
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY":    SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num, "gl": "se", "hl": "sv"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as e:
        print(f"    ⚠️ Serper-fel: {e}")
        return []


# ─────────────────────────────────────────────
# EVENT-SÖKNING
# ─────────────────────────────────────────────

def fetch_events(club_name: str, city: str) -> list:
    """
    Söker kommande events för en specifik klubb.
    Kollar Tickster, Dice, Billetto, RA och Ticketmaster.
    """
    query = (
        f"{club_name} {city} "
        f"(site:tickster.com OR site:dice.fm OR site:billetto.se "
        f"OR site:ra.co OR site:ticketmaster.se)"
    )
    results = serper_search(query, num=8)

    # Komplettera med generell sökning om inga ticketingsajter hittades
    ticket_domains = ["tickster.com", "dice.fm", "billetto.se", "ra.co", "ticketmaster.se"]
    has_ticket = any(
        any(d in r.get("link", "") for d in ticket_domains)
        for r in results
    )
    if not has_ticket:
        results += serper_search(
            f"{club_name} {city} biljetter event kommande", num=5
        )

    events     = []
    seen       = set()
    urls_tried = 0

    for r in results:
        if urls_tried >= 4:
            break

        url     = r.get("link", "")
        title   = r.get("title", "")
        snippet = r.get("snippet", "")

        skip = ["google.", "facebook.", "instagram.", "youtube.", "twitter.", "wikipedia."]
        if any(s in url for s in skip):
            continue

        event_kw = ["event", "ticket", "biljett", "concert", "konsert",
                    "spelning", "show", "club night", "dj"]
        if not any(kw in (title + snippet).lower() for kw in event_kw):
            continue

        urls_tried += 1

        try:
            resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=6)
            soup = BeautifulSoup(resp.text, "html.parser")

            # OG-bild
            og_image = None
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                og_image = og["content"]
            if not og_image:
                tw = soup.find("meta", attrs={"name": "twitter:image"})
                if tw and tw.get("content"):
                    og_image = tw["content"]

            # JSON-LD – bästa källan
            found_ld = False
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data  = json.loads(script.string)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("@type") in ("Event", "MusicEvent", "DanceEvent"):
                            date = (item.get("startDate") or "")[:10]
                            if not date or date < TODAY:
                                continue

                            ld_img = item.get("image")
                            if isinstance(ld_img, list): ld_img = ld_img[0]
                            if isinstance(ld_img, dict): ld_img = ld_img.get("url")

                            event_title = (item.get("name") or "").strip()
                            if not event_title or event_title.lower() in seen:
                                continue
                            seen.add(event_title.lower())
                            found_ld = True

                            events.append({
                                "title":      event_title,
                                "date":       date,
                                "artists":    [
                                    p.get("name") for p in item.get("performer", [])
                                    if isinstance(p, dict)
                                ],
                                "ticket_url": item.get("url") or url,
                                "image":      ld_img or og_image,
                                "source":     url,
                            })
                except Exception:
                    continue

            # Fallback – OG-bild + snippet om JSON-LD saknades
            if not found_ld and og_image and snippet:
                event_title = title.split("|")[0].split("-")[0].strip()
                if event_title.lower() not in seen:
                    seen.add(event_title.lower())
                    events.append({
                        "title":      event_title,
                        "date":       None,
                        "artists":    [],
                        "ticket_url": url,
                        "image":      og_image,
                        "source":     url,
                    })

        except Exception:
            continue

        time.sleep(0.3)

    # Filtrera och sortera
    events = [e for e in events if e.get("date") and e["date"] >= TODAY]
    events.sort(key=lambda e: e.get("date") or "9999")
    return events[:10]


# ─────────────────────────────────────────────
# HUVUD-AGENT
# ─────────────────────────────────────────────

def run_event_agent():
    print("\n" + "═" * 55)
    print("  🎫 PartyPrep Event Agent – Startar")
    print("═" * 55)
    print(f"  Dagens datum: {TODAY}")
    print(f"  Fil: {INPUT_FILE}")
    print("═" * 55)

    # Läs master-filen med alla klubbar
    # Faller tillbaka på nightclubs.json om master saknas
    read_file = MASTER_FILE if os.path.exists(MASTER_FILE) else "nightclubs.json"
    print(f"  Läser från: {read_file}")
    with open(read_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    clubs = data.get("clubs", [])
    print(f"\n  📋 {len(clubs)} klubbar att uppdatera events för\n")

    total_events  = 0
    clubs_updated = 0

    for i, club in enumerate(clubs):
        name = club.get("name", "Okänd")
        city = club.get("city", "")

        print(f"  [{i+1}/{len(clubs)}] 🎯 {name} ({city})")

        events = fetch_events(name, city)

        if events:
            print(f"    ✅ {len(events)} kommande events hittade")
            for e in events:
                print(f"       → {e.get('date')} | {e.get('title')} | bild: {'ja' if e.get('image') else 'nej'}")
            club["next_events"] = events
            total_events  += len(events)
            clubs_updated += 1
        else:
            print("    ⚠️  Inga kommande events hittade")
            # Rensa gamla events
            club["next_events"] = []

        club["events_last_updated"] = datetime.now().isoformat()
        time.sleep(0.5)

    # ── Uppdatera master-filen ───────────────────────
    data["meta"]["events_updated_at"] = datetime.now().isoformat()
    data["meta"]["stats"]["total_events"] = total_events

    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ── Spara import-fil med bara de med events ───────
    clubs_with_events = [c for c in clubs if c.get("next_events")]
    with open(IMPORT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated_at":    datetime.now().isoformat(),
                "total_clubs":     len(clubs_with_events),
                "total_events":    total_events,
                "description":     "Importera denna – klubbar med uppdaterade events",
            },
            "clubs": clubs_with_events,
        }, f, ensure_ascii=False, indent=2)

    print("\n" + "═" * 55)
    print("  ✅ KLART!")
    print("═" * 55)
    print(f"  🎫 Totalt events:      {total_events}")
    print(f"  🎯 Klubbar med events: {clubs_updated}/{len(clubs)}")
    print(f"  📚 Master uppdaterad:  {MASTER_FILE}")
    print(f"  📁 Import (ladda ner): {IMPORT_FILE}")
    print("═" * 55)


if __name__ == "__main__":
    run_event_agent()
