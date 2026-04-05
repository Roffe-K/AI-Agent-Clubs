"""
PartyPrep Nightclub Agent
=========================
Pipeline:
  1. Scrapa listningssajter (Thatsup, VisitStockholm) för förhandsdata
  2. Google Places (New API) – hitta klubbar, adress, öppetider, bilder
  3. Scrapa varje klubbs hemsida – Instagram, åldersgräns, säsong
  4. Claude Haiku – strukturera och jämför alla källor
  5. Serper – dubbelkolla öppetider, hitta åldersgräns, hemsida, Instagram
  6. Spara till nightclubs.json

Förbättringar:
  - Serper dubbelkollar öppetider mot Google Maps
  - Endast högupplösta editorial-bilder (ej Google Reviews-bilder)
  - Strukturerad seasonal med faktiska datum
  - Google rating + reviews sparas

Krav (GitHub Secrets):
  GOOGLE_API_KEY  – Google Cloud API-nyckel (Places API aktiverat)
  CLAUDE_API_KEY  – Anthropic API-nyckel
  SERPER_API_KEY  – Serper.dev API-nyckel
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

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "DIN_GOOGLE_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "DIN_CLAUDE_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "DIN_SERPER_KEY")

OUTPUT_FILE = "nightclubs.json"
CITIES      = ["Stockholm"]

LISTING_SITES = [
    {"url": "https://www.thatsup.se/stockholm/noje/nattliv/",    "city": "Stockholm", "name": "Thatsup Stockholm"},
    {"url": "https://www.visitstockholm.com/see--do/nightlife/", "city": "Stockholm", "name": "Visit Stockholm"},
    {"url": "https://www.thatsup.se/goteborg/noje/nattliv/",     "city": "Göteborg",  "name": "Thatsup Göteborg"},
    {"url": "https://www.goteborg.com/en/nightlife/",            "city": "Göteborg",  "name": "Visit Göteborg"},
    {"url": "https://www.thatsup.se/malmo/noje/nattliv/",        "city": "Malmö",     "name": "Thatsup Malmö"},
    {"url": "https://www.visitmalmö.se/uppleva/nojesliv/",       "city": "Malmö",     "name": "Visit Malmö"},
]

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── Google Places (New) ────────────────────
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAIL_URL = "https://places.googleapis.com/v1/places/{place_id}"
PLACES_PHOTO_URL  = "https://places.googleapis.com/v1/{photo_name}/media"

SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.location,places.regularOpeningHours,places.websiteUri,"
    "places.photos,places.nationalPhoneNumber,places.googleMapsUri,"
    "places.rating,places.userRatingCount"
)
DETAIL_FIELD_MASK = (
    "id,displayName,formattedAddress,location,regularOpeningHours,"
    "websiteUri,photos,nationalPhoneNumber,googleMapsUri,rating,userRatingCount"
)

# ─────────────────────────────────────────────
# KLIENTER
# ─────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


# ─────────────────────────────────────────────
# SERPER – HJÄLPFUNKTION
# ─────────────────────────────────────────────

def serper_search(query: str, num: int = 8) -> list:
    """Gör en Google-sökning via Serper och returnerar organiska resultat."""
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
        print(f"    ⚠️ Serper-fel för '{query}': {e}")
        return []


# ─────────────────────────────────────────────
# SERPER – DUBBELKOLLA ÖPPETIDER
# ─────────────────────────────────────────────

def verify_opening_hours(club_name: str, city: str, google_hours: list) -> dict:
    """
    Dubbelkollar Google Maps öppetider mot andra källor via Serper.
    Returnerar öppetider + confidence baserat på om källorna stämmer överens.
    """
    queries = [
        f"{club_name} öppettider {city}",
        f"{club_name} opening hours {city}",
        f"{club_name} {city} när öppnar",
    ]

    all_snippets = []
    urls_to_check = []

    for query in queries:
        results = serper_search(query, num=5)
        for r in results:
            snippet = r.get("snippet", "")
            title   = r.get("title", "")
            url     = r.get("link", "")
            if snippet:
                all_snippets.append(f"{title}: {snippet}")
            hour_keywords = ["öppet", "open", "stängt", "closed", "22:", "23:", "00:", "01:", "02:", "03:", "04:", "05:"]
            if url and any(kw in (snippet + title).lower() for kw in hour_keywords):
                skip = ["google.", "facebook.", "instagram.", "youtube."]
                if not any(s in url for s in skip):
                    urls_to_check.append(url)
        if all_snippets:
            break

    if not all_snippets:
        return {"verified": False, "source": "google_only"}

    google_hours_str = ", ".join(google_hours) if google_hours else "saknas"
    snippets_str     = "\n".join(list(dict.fromkeys(all_snippets))[:10])

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"""Jämför öppettiderna för '{club_name}' i {city}.

Google Maps säger: {google_hours_str}

Information från andra källor:
{snippets_str}

Svara ENBART med JSON (inga backticks):
{{
  "opening_hours": {{
    "monday":    "stängt",
    "tuesday":   "stängt",
    "wednesday": "stängt",
    "thursday":  "22:00-03:00",
    "friday":    "22:00-05:00",
    "saturday":  "22:00-05:00",
    "sunday":    "stängt"
  }},
  "confidence": "high",
  "sources_agree": true,
  "note": "Om källorna skiljer sig, beskriv kort"
}}

confidence: "high" om flera källor stämmer överens, "medium" om bara en källa, "low" om de skiljer sig.
Välj den mest troliga/uppdaterade informationen om källorna skiljer sig.
Sätt null för dagar utan info.
"""}]
    )

    try:
        result = json.loads(_clean_json(response.content[0].text.strip()))
        result["verified"] = True
        result["source"]   = "google + serper"
        return result
    except:
        return {"verified": False, "source": "google_only"}


# ─────────────────────────────────────────────
# GOOGLE PLACES – BILDER (ENDAST HÖGUPPLÖSTA)
# ─────────────────────────────────────────────

def get_place_images(photos: list, max_images: int = 5) -> list:
    """
    Hämtar endast högupplösta editorial-bilder från Google Places.
    Filtrerar bort bilder från Google Reviews (user-uploaded).
    Google Places (New) returnerar foton med attributions – vi väljer
    bara de utan 'user' i attribution för att undvika review-bilder.
    """
    images = []
    for photo in photos:
        # Kolla attribution – hoppa över användarfoton från reviews
        attributions = photo.get("authorAttributions", [])
        is_user_photo = any(
            "maps.google" in a.get("uri", "") or
            a.get("photoUri", "").startswith("//lh")
            for a in attributions
        )
        if is_user_photo and len(images) > 0:
            continue  # Skippa review-bilder om vi redan har editorial

        name = photo.get("name")
        if name:
            # Begär högsta upplösning – 4800px
            images.append(
                f"{PLACES_PHOTO_URL.format(photo_name=name)}"
                f"?maxWidthPx=4800&key={GOOGLE_API_KEY}"
            )

        if len(images) >= max_images:
            break

    return images


# ─────────────────────────────────────────────
# SERPER – HITTA HEMSIDA
# ─────────────────────────────────────────────

def search_website(club_name: str, city: str) -> str | None:
    """Söker upp klubbens hemsida via Serper när Google Places inte har den."""
    results = serper_search(f"{club_name} {city} officiell hemsida nattklubb", num=5)

    candidates = []
    for r in results:
        url     = r.get("link", "")
        title   = r.get("title", "").lower()
        snippet = r.get("snippet", "").lower()
        skip    = ["tripadvisor", "yelp", "facebook", "instagram",
                   "google", "wikipedia", "thatsup", "visitstockholm"]
        if any(d in url for d in skip):
            continue
        candidates.append(f"{url} – {title}")

    if not candidates:
        return None

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"""Which URL is the official website for '{club_name}' in {city}?
The club might be owned by a restaurant or bar with a different name.
Reply with ONLY the URL, or null.
Candidates:\n{chr(10).join(candidates[:5])}"""}]
    )

    answer = response.content[0].text.strip()
    if answer.startswith("http") and "." in answer:
        return answer
    return None


# ─────────────────────────────────────────────
# SERPER – HITTA INSTAGRAM
# ─────────────────────────────────────────────

def search_instagram(club_name: str, city: str) -> str | None:
    """Söker upp klubbens Instagram-handle via Serper."""
    results = serper_search(f"{club_name} {city} site:instagram.com", num=5)
    for r in results:
        url = r.get("link", "")
        if "instagram.com/" in url:
            handle = (
                url.rstrip("/")
                .split("instagram.com/")[-1]
                .split("/")[0]
                .split("?")[0]
            )
            if handle and handle not in ("p", "stories", "reel", "explore", ""):
                return handle
    return None


# ─────────────────────────────────────────────
# SERPER – HITTA ÅLDERSGRÄNS
# ─────────────────────────────────────────────

def search_age_limit(club_name: str, city: str) -> int | None:
    """
    Aggressiv åldersgräns-sökning via Serper.
    1. Söker med många fraser på svenska och engelska
    2. Samlar snippets från alla queries
    3. Scrapar de mest lovande sidorna för djupare analys
    4. Claude analyserar allt och extraherar siffran
    """
    queries = [
        f"{club_name} åldersgräns {city}",
        f"{club_name} ålder {city} nattklubb",
        f"{club_name} åldersgräns",
        f"{club_name} inträde ålder {city}",
        f"{club_name} age limit {city}",
        f"{club_name} age limit nightclub",
        f"{club_name} {city} how old to enter",
    ]

    all_snippets   = []
    urls_to_scrape = []

    for query in queries:
        results = serper_search(query, num=8)
        for r in results:
            snippet = r.get("snippet", "")
            title   = r.get("title", "")
            url     = r.get("link", "")

            if snippet:
                all_snippets.append(f"{title}: {snippet}")

            age_kw = ["ålder", "age", "limit", "gräns", "inträde", "entry", "18", "20", "21", "23"]
            skip   = ["google.", "facebook.", "instagram.", "youtube.", "twitter."]
            if url and any(kw in (snippet + title).lower() for kw in age_kw):
                if not any(s in url for s in skip):
                    urls_to_scrape.append(url)

    # Försök extrahera direkt från snippets
    if all_snippets:
        snippets_str = "\n".join(list(dict.fromkeys(all_snippets))[:15])
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": f"""Official minimum age to enter '{club_name}' in {city}?
Reply ONLY with: 18, 20, 21, 23 or null. Never guess.
Text:\n{snippets_str}"""}]
        )
        answer = response.content[0].text.strip()
        if answer.isdigit() and int(answer) in (18, 20, 21, 23):
            return int(answer)

    # Scrapar de 3 mest lovande sidorna direkt
    scraped_texts = []
    for url in list(dict.fromkeys(urls_to_scrape))[:3]:
        try:
            resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=8)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text  = soup.get_text(separator=" ", strip=True)
            words = text.split()

            # Plocka ut stycken runt ålder-nyckelord
            relevant = []
            for i, word in enumerate(words):
                if any(kw in word.lower() for kw in ["ålder", "age", "gräns", "limit", "inträde", "18", "20", "21", "23"]):
                    start = max(0, i - 20)
                    end   = min(len(words), i + 20)
                    relevant.append(" ".join(words[start:end]))

            if relevant:
                scraped_texts.append(f"Från {url}:\n" + " | ".join(relevant[:5]))
        except Exception:
            continue

    if scraped_texts:
        full_text = "\n\n".join(scraped_texts)
        response  = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": f"""Official minimum age to enter '{club_name}' in {city}?
Reply ONLY with: 18, 20, 21, 23 or null. Never guess.
Text:\n{full_text[:3000]}"""}]
        )
        answer = response.content[0].text.strip()
        if answer.isdigit() and int(answer) in (18, 20, 21, 23):
            return int(answer)

    return None


# ─────────────────────────────────────────────
# TICKETING – SCRAPA EVENTS FRÅN BILJETTAJTER
# ─────────────────────────────────────────────

# Svenska/nordiska ticketingsajter
TICKETING_SITES = [
    {
        "name":    "Tickster",
        "search":  "https://tickster.com/sv/search?q={query}",
        "domain":  "tickster.com",
    },
    {
        "name":    "Dice",
        "search":  "https://dice.fm/search?q={query}",
        "domain":  "dice.fm",
    },
    {
        "name":    "Billetto",
        "search":  "https://billetto.se/search?q={query}",
        "domain":  "billetto.se",
    },
    {
        "name":    "Resident Advisor",
        "search":  "https://ra.co/search?q={query}",
        "domain":  "ra.co",
    },
    {
        "name":    "Ticketmaster",
        "search":  "https://www.ticketmaster.se/search?q={query}",
        "domain":  "ticketmaster.se",
    },
]


def scrape_ticketing_events(club_name: str, city: str) -> list:
    """
    Söker efter kommande events för en klubb på:
    Tickster, Dice, Billetto, Resident Advisor, Ticketmaster.

    Strategi:
    1. Serper söker på varje ticketingsajt med site:-operator
    2. Scrapar de hittade event-sidorna direkt
    3. Claude extraherar strukturerad event-info

    Returnerar lista med events från ALLA källor kombinerat.
    """
    all_events  = []
    seen_titles = set()

    # Bygg sökfrågor per sajt
    site_queries = [
        f"{club_name} {city} site:tickster.com",
        f"{club_name} {city} site:dice.fm",
        f"{club_name} {city} site:billetto.se",
        f"{club_name} site:ra.co",
        f"{club_name} {city} site:ticketmaster.se",
        # Generell sökning som fångar flera sajter
        f"{club_name} {city} biljetter kommande event 2025 2026",
    ]

    event_pages = []  # URL:er att scrapa

    for query in site_queries:
        results = serper_search(query, num=5)
        for r in results:
            url     = r.get("link", "")
            title   = r.get("title", "")
            snippet = r.get("snippet", "")

            # Kontrollera att det verkar vara en event-sida
            event_kw = ["event", "ticket", "biljett", "concert", "konsert",
                        "spelning", "show", "club night", "dj"]
            if any(kw in (title + snippet).lower() for kw in event_kw):
                event_pages.append({
                    "url":     url,
                    "title":   title,
                    "snippet": snippet,
                })

        time.sleep(0.2)

    if not event_pages:
        return []

    # Scrapa de 5 mest lovande event-sidorna
    scraped_events = []
    for page in event_pages[:5]:
        try:
            resp = requests.get(
                page["url"], headers=SCRAPE_HEADERS, timeout=10
            )
            soup = BeautifulSoup(resp.text, "html.parser")

            # ── Hitta bild via Open Graph (finns på nästan alla sidor) ──
            og_image = None
            og_tag = soup.find("meta", property="og:image")
            if og_tag and og_tag.get("content"):
                og_image = og_tag["content"]
            # Twitter-kort som fallback
            if not og_image:
                tw_tag = soup.find("meta", attrs={"name": "twitter:image"})
                if tw_tag and tw_tag.get("content"):
                    og_image = tw_tag["content"]

            # ── Försök hitta strukturerad event-data (JSON-LD) ──
            json_ld_events = []
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, list):
                        items = data
                    else:
                        items = [data]
                    for item in items:
                        if item.get("@type") in ("Event", "MusicEvent", "DanceEvent"):
                            # Hämta bild från JSON-LD, annars använd OG-bild
                            ld_image = item.get("image")
                            if isinstance(ld_image, list):
                                ld_image = ld_image[0]
                            if isinstance(ld_image, dict):
                                ld_image = ld_image.get("url")
                            event_image = ld_image or og_image

                            json_ld_events.append({
                                "title":      item.get("name"),
                                "date":       item.get("startDate", "")[:10],
                                "artists":    [p.get("name") for p in item.get("performer", [])] if isinstance(item.get("performer"), list) else [],
                                "ticket_url": item.get("url") or page["url"],
                                "image":      event_image,
                                "source":     page["url"],
                            })
                except Exception:
                    continue

            if json_ld_events:
                # Lägg till OG-bild på events som saknar bild
                for e in json_ld_events:
                    if not e.get("image") and og_image:
                        e["image"] = og_image
                scraped_events.extend(json_ld_events)
                continue  # JSON-LD var perfekt

            # ── Fallback: läs vanlig text + spara OG-bild ──
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)[:3000]
            if text:
                scraped_events.append({
                    "raw_text":   text,
                    "source_url": page["url"],
                    "og_image":   og_image,
                    "source":     page["url"],
                })

        except Exception as e:
            continue

    if not scraped_events:
        return []

    # Dela upp i strukturerade (JSON-LD) och råtext
    structured = [e for e in scraped_events if "title" in e]
    raw_texts  = [e for e in scraped_events if "raw_text" in e]

    final_events = []

    # Lägg till strukturerade events direkt
    for e in structured:
        if e.get("title") and e["title"].lower() not in seen_titles:
            seen_titles.add(e["title"].lower())
            final_events.append(e)

    # Extrahera events från råtext med Claude
    if raw_texts:
        combined = "\n\n---\n\n".join(
            f"Källa: {e['source_url']}\nOG-bild: {e.get('og_image', 'ingen')}\n{e['raw_text']}"
            for e in raw_texts[:3]
        )
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": f"""Extract ALL upcoming events for '{club_name}' from these ticketing pages.

{combined[:4000]}

Reply ONLY with JSON array (no backticks, no explanation):
[{{
  "title": "Event name or DJ/artist name",
  "date": "2025-06-14",
  "artists": ["Artist 1", "Artist 2"],
  "ticket_url": "https://...",
  "image": "https://... (event poster/image URL if visible in text)",
  "source": "tickster.com"
}}]

Rules:
- Only future events (today or later)
- Use null for unknown fields
- ticket_url must be the direct purchase URL
- image should be a full https:// URL to the event poster if found
- If no events found, return []
"""}]
        )
        try:
            extracted = json.loads(_clean_json(response.content[0].text.strip()))
            if isinstance(extracted, list):
                for e in extracted:
                    title = (e.get("title") or "").lower()
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        final_events.append(e)
        except Exception:
            pass

    # Filtrera bort gamla events – bara framtida datum
    today = datetime.now().strftime("%Y-%m-%d")
    final_events = [
        e for e in final_events
        if e.get("date") and e["date"] >= today
    ]
    final_events.sort(key=lambda e: e.get("date") or "9999")
    return final_events[:10]



# ─────────────────────────────────────────────
# SNABB EVENT-SÖKNING – ALLA KLUBBAR
# ─────────────────────────────────────────────

def quick_search_events(club_name: str, city: str) -> list:
    """
    Snabb event-sökning för ALLA klubbar (inte bara eventbaserade).
    Tar ~2-3 sekunder per klubb istället för 10-20s.

    Strategi:
    1. En Serper-sökning med site: mot alla ticketingsajter samtidigt
    2. Scrapar bara sidor som har JSON-LD eller OG-bild (snabbt)
    3. Max 3 sidor per klubb

    Returnerar max 5 kommande events.
    """
    # En sökning som täcker alla ticketingsajter
    query = (
        f"{club_name} {city} "
        f"(site:tickster.com OR site:dice.fm OR site:billetto.se "
        f"OR site:ra.co OR site:ticketmaster.se)"
    )
    results = serper_search(query, num=8)

    # Komplettera med generell sökning om inga ticketingsajter hittades
    ticket_domains = ["tickster.com", "dice.fm", "billetto.se", "ra.co", "ticketmaster.se"]
    has_ticket_results = any(
        any(d in r.get("link", "") for d in ticket_domains)
        for r in results
    )
    if not has_ticket_results:
        results += serper_search(
            f"{club_name} {city} biljetter event 2025 2026", num=5
        )

    events     = []
    seen       = set()
    urls_tried = 0

    for r in results:
        if urls_tried >= 3:
            break

        url     = r.get("link", "")
        title   = r.get("title", "")
        snippet = r.get("snippet", "")

        # Skippa irrelevanta sidor
        skip = ["google.", "facebook.", "instagram.", "youtube.", "twitter.", "wikipedia."]
        if any(s in url for s in skip):
            continue

        # Snabb koll – verkar det vara en event-sida?
        event_kw = ["event", "ticket", "biljett", "concert", "konsert",
                    "spelning", "show", "club night", "dj", "köp"]
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

            # JSON-LD – snabbaste och mest pålitliga källan
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("@type") in ("Event", "MusicEvent", "DanceEvent"):
                            ld_img = item.get("image")
                            if isinstance(ld_img, list): ld_img = ld_img[0]
                            if isinstance(ld_img, dict): ld_img = ld_img.get("url")

                            event_title = item.get("name", "").strip()
                            if not event_title or event_title.lower() in seen:
                                continue
                            seen.add(event_title.lower())

                            events.append({
                                "title":      event_title,
                                "date":       (item.get("startDate") or "")[:10],
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

            # Om JSON-LD inte fanns – använd snippet + OG-bild som snabb fallback
            if not events and og_image and snippet:
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

    # Filtrera bort gamla events – bara framtida datum
    today = datetime.now().strftime("%Y-%m-%d")
    events = [
        e for e in events
        if e.get("date") and e["date"] >= today  # Kräv datum OCH att det är i framtiden
    ]
    events.sort(key=lambda e: e.get("date") or "9999")
    return events[:5]

# ─────────────────────────────────────────────
# SERPER – HITTA KOMMANDE EVENTS
# ─────────────────────────────────────────────

def detect_event_based_and_scrape(club_name: str, city: str, website_text: str, website_url: str) -> dict:
    """
    Detekterar om en klubb är eventbaserad (öppnar bara vid spelningar/events)
    och hämtar kommande events om så är fallet.
    
    Returnerar:
    {
      "is_event_based": true/false,
      "next_events": [{"title": ..., "date": ..., "url": ...}]
    }
    """
    # Steg 1: Kolla om hemsidtexten antyder eventbaserad klubb
    is_event_based = False

    if website_text:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": f"""Is '{club_name}' an event-based venue that ONLY opens for special events (no regular weekly schedule)?

TRUE only if: no fixed weekly hours, only opens for specific concerts/shows, website only lists upcoming events with no regular schedule.

FALSE if: has regular weekly opening hours (even if they also host events), opens every Friday/Saturday, is a regular nightclub that also promotes events.

A regular nightclub that hosts DJ nights or events is NOT event-based.
Reply ONLY with: true or false.
Text: {website_text[:2000]}"""}]
        )
        answer = response.content[0].text.strip().lower()
        if answer == "true":
            is_event_based = True

    # Steg 2: Om eventbaserad – hitta kommande events
    next_events = []
    if is_event_based:
        # Scrapa hemsidan extra noga efter event-info
        if website_url:
            try:
                resp = requests.get(website_url, headers=SCRAPE_HEADERS, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                event_text = soup.get_text(separator="\n", strip=True)[:4000]
            except:
                event_text = website_text
        else:
            event_text = website_text

        # Hämta events från ticketingsajter
        print("    🎟️  Söker events på Tickster, Dice, Billetto, RA, Ticketmaster...")
        ticketing_events = scrape_ticketing_events(club_name, city)

        # Sök också via Serper för hemsidan
        serper_results = serper_search(f"{club_name} {city} kommande event spelning 2025 2026", num=5)
        serper_snippets = "\n".join(
            f"{r.get('title','')}: {r.get('snippet','')}"
            for r in serper_results
        )

        # Claude extraherar events från hemsidan
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": f"""Extract upcoming events for the venue '{club_name}' in {city}.

Website text:
{event_text[:2000]}

Search results:
{serper_snippets[:1000]}

Reply ONLY with JSON array (no backticks). Max 5 events:
[{{"title": "Event name", "date": "2025-06-14", "artists": ["Artist 1"], "ticket_url": null, "source": "website"}}]
Use null for unknown fields. Only include future events. If no events found, return [].
"""}]
        )

        website_events = []
        try:
            events_raw = _clean_json(response.content[0].text.strip())
            website_events = json.loads(events_raw)
            if not isinstance(website_events, list):
                website_events = []
        except:
            website_events = []

        # Kombinera events från alla källor, ta bort dubletter
        seen = set()
        next_events = []
        for e in (ticketing_events + website_events):
            key = (e.get("title") or "").lower().strip()
            if key and key not in seen:
                seen.add(key)
                next_events.append(e)

        next_events.sort(key=lambda e: e.get("date") or "9999")
        next_events = next_events[:10]

    return {
        "is_event_based": is_event_based,
        "next_events":    next_events,
    }


# ─────────────────────────────────────────────
# GOOGLE PLACES (NEW API)
# ─────────────────────────────────────────────

def _gplaces_headers(field_mask: str) -> dict:
    return {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask,
    }


def search_google_places(city: str) -> list:
    all_results = []
    seen_ids    = set()

    for query in [f"nattklubb {city}", f"nightclub {city}", f"bar club {city}"]:
        try:
            resp = requests.post(
                PLACES_SEARCH_URL,
                headers=_gplaces_headers(SEARCH_FIELD_MASK),
                json={
                    "textQuery":      query,
                    "includedType":   "night_club",
                    "languageCode":   "sv",
                    "maxResultCount": 20,
                },
                timeout=15,
            )
            resp.raise_for_status()
            for place in resp.json().get("places", []):
                pid = place.get("id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_results.append(place)
            time.sleep(0.5)
        except Exception as e:
            print(f"    ⚠️ Google Places-fel för '{query}': {e}")

    return all_results


def get_place_details(place_id: str) -> dict:
    try:
        resp = requests.get(
            PLACES_DETAIL_URL.format(place_id=place_id),
            headers=_gplaces_headers(DETAIL_FIELD_MASK),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    ⚠️ Detaljer misslyckades: {e}")
        return {}


def parse_opening_hours(details: dict) -> list:
    return details.get("regularOpeningHours", {}).get("weekdayDescriptions", [])


# ─────────────────────────────────────────────
# LISTNINGSSAJTER
# ─────────────────────────────────────────────

def scrape_listing_site(site: dict) -> list:
    try:
        resp = requests.get(site["url"], headers=SCRAPE_HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["nav", "footer", "script", "style", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:6000]

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"""Du läser en lista med nattklubbar från '{site["name"]}'.
Extrahera ALLA nattklubbar. Svara ENBART med JSON-array (inga backticks):
[{{"name": "Klubbnamn", "age_limit": 20, "opening_hours": "Fre-Lör 22-05", "notes": "info"}}]
Sätt null om info saknas. Text:\n{text}"""}]
        )
        return json.loads(_clean_json(response.content[0].text.strip()))
    except Exception as e:
        print(f"    ⚠️ Kunde inte scrapa {site['name']}: {e}")
        return []


# ─────────────────────────────────────────────
# HEMSIDA-SCRAPING
# ─────────────────────────────────────────────

def scrape_website(url: str) -> dict:
    if not url:
        return {}
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")

        instagram_handle = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "instagram.com/" in href:
                handle = (
                    href.rstrip("/")
                    .split("instagram.com/")[-1]
                    .split("/")[0]
                    .split("?")[0]
                )
                if handle and handle not in ("", "p", "stories", "reel"):
                    instagram_handle = handle
                    break

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:5000]

        return {"instagram_handle": instagram_handle, "raw_text": text}
    except Exception as e:
        print(f"    ⚠️ Hemsida misslyckades: {e}")
        return {}


# ─────────────────────────────────────────────
# CLAUDE AI-EXTRAKTION
# ─────────────────────────────────────────────

def extract_with_claude(
    website_text: str,
    club_name: str,
    city: str = "",
    listing_info: str = "",
    google_hours: list = None,
) -> dict:
    if not website_text and not listing_info:
        return {}

    current_year = datetime.now().year

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": f"""Extrahera och generera info om nattklubben/venuen '{club_name}' i {city}.
OBS: Klubben kan vara restaurang/bar på dagtid med nattklubb på kvällen.

Åldersgräns: "18 år", "20-årsgräns", "åldersgräns 23", "minimum age", "entry age".
Vanliga värden: 18, 20, 21, 23. Gissa ALDRIG – sätt null om osäker.

Google öppetider: {', '.join(google_hours) if google_hours else 'saknas'}
Listningssajt-info: {listing_info or 'ingen'}
Hemsidetext: {website_text[:3000] or 'saknas'}

Svara ENBART med JSON (inga backticks):
{{
  "age_limit": 23,
  "opening_hours": {{
    "monday":    "stängt",
    "tuesday":   "stängt",
    "wednesday": "stängt",
    "thursday":  "22:00-03:00",
    "friday":    "22:00-05:00",
    "saturday":  "22:00-05:00",
    "sunday":    "stängt"
  }},
  "seasonal": {{
    "is_seasonal": true,
    "open_from":   "{current_year}-06-01",
    "open_to":     "{current_year}-08-31",
    "closed_from": "{current_year}-09-01",
    "closed_to":   "{current_year + 1}-05-31",
    "note":        "Endast öppet sommarsäsong juni-aug"
  }},
  "short_description": "1-2 meningar max. Snappy och lockande. Visas i listvy bland alla klubbar. Ex: Sveriges mest ikoniska nattklubb med fokus på house och techno i lyxig miljö.",
  "full_description": "3-5 meningar. Detaljerad och engagerande text för klubbens egna sida. Beskriv atmosfär, musikstil, historia om känd, målgrupp och vad som gör klubben unik.",
  "music_genre": "House, Techno",
  "dress_code": "Ingen speciell",
  "hours_confidence": "high",
  "is_event_based": false
}}

VIKTIGT för varje fält:
- short_description: MAX 2 meningar, alltid på svenska, aldrig null
- full_description: 3-5 meningar, alltid på svenska, aldrig null. Basera på all tillgänglig info – om lite info finns, skriv en trovärdig text baserad på klubbens namn och stad.
- music_genre: Kommaseparerade genrer som "House, Techno" eller "Hip-Hop, R&B". Om okänt, gissa utifrån klubbens namn/stad/typ. Sätt ALDRIG null – använd "Varierande" om helt okänt.
- dress_code: Exakt klädkod om nämnd, annars ALLTID "Ingen speciell"
- seasonal: Sätt is_seasonal: true ENDAST om klubben tydligt har säsongsvariationer
- is_event_based: true om klubben ENDAST öppnar vid speciella events

Åldersgräns måste vara officiell. Sätt null bara för age_limit och opening_hours om info saknas.
"""}]
    )

    try:
        return json.loads(_clean_json(response.content[0].text.strip()))
    except:
        return {}


# ─────────────────────────────────────────────
# MERGE & CONFIDENCE
# ─────────────────────────────────────────────

def merge_sources(
    details: dict,
    website_data: dict,
    ai_data: dict,
    listing_match: dict,
    city: str,
    serper_age: int | None = None,
    serper_instagram: str | None = None,
    serper_website: str | None = None,
    verified_hours: dict | None = None,
    is_event_based: bool = False,
    next_events: list | None = None,
) -> dict:
    google_hours = parse_opening_hours(details)

    # Öppetider – prioritera verifierade timmar från Serper-jämförelse
    if verified_hours and verified_hours.get("opening_hours"):
        opening_hours    = verified_hours["opening_hours"]
        hours_confidence = verified_hours.get("confidence", "medium")
        hours_source     = verified_hours.get("source", "serper verified")
    elif ai_data.get("opening_hours") and google_hours:
        opening_hours    = ai_data["opening_hours"]
        hours_confidence = "high"
        hours_source     = "website + google"
    elif ai_data.get("opening_hours"):
        opening_hours    = ai_data["opening_hours"]
        hours_confidence = "medium"
        hours_source     = "website"
    elif google_hours:
        opening_hours    = {"google_format": google_hours}
        hours_confidence = "medium"
        hours_source     = "google places"
    else:
        opening_hours    = None
        hours_confidence = "low"
        hours_source     = "saknas"

    # Åldersgräns – prioritetsordning
    age_limit = (
        ai_data.get("age_limit")
        or listing_match.get("age_limit")
        or serper_age
    )
    age_source = (
        "website"  if ai_data.get("age_limit")       else
        "listing"  if listing_match.get("age_limit") else
        "serper"   if serper_age                     else
        "saknas"
    )

    # Instagram
    instagram_handle = (
        website_data.get("instagram_handle") or serper_instagram
    )
    instagram_source = (
        "website" if website_data.get("instagram_handle") else
        "serper"  if serper_instagram else
        "saknas"
    )

    # Hemsida
    website = details.get("websiteUri") or serper_website

    # Seasonal
    seasonal = ai_data.get("seasonal")

    handle = instagram_handle

    return {
        # ─── Identitet ───
        "name":              details.get("displayName", {}).get("text", "Okänd"),
        "city":              city,
        "short_description": ai_data.get("short_description"),
        "full_description":  ai_data.get("full_description"),
        "music_genre":       ai_data.get("music_genre") or "Varierande",
        "description":       ai_data.get("short_description"),  # bakåtkompatibilitet

        # ─── Plats ───
        "address":         details.get("formattedAddress"),
        "lat":             details.get("location", {}).get("latitude"),
        "lng":             details.get("location", {}).get("longitude"),
        "google_maps_url": details.get("googleMapsUri"),

        # ─── Tider ───
        "opening_hours": opening_hours,
        "seasonal":      seasonal,

        # ─── Tillträde ───
        "age_limit":  age_limit,
        "dress_code": ai_data.get("dress_code") or "Ingen speciell",

        # ─── Kontakt & media ───
        "website":         website,
        "instagram":       f"https://instagram.com/{handle}" if handle else None,
        "phone":           details.get("nationalPhoneNumber"),
        "images":          get_place_images(details.get("photos", [])),

        # ─── Google-metadata ───
        "google_rating":  details.get("rating"),
        "google_reviews": details.get("userRatingCount"),

        # ─── Datakvalitet ───
        "confidence": {
            "opening_hours":        hours_confidence,
            "opening_hours_source": hours_source,
            "hours_verified":       bool(verified_hours and verified_hours.get("verified")),
            "age_limit":            "high" if age_limit else "unknown",
            "age_limit_source":     age_source,
            "instagram_source":     instagram_source,
        },
        "sources_used": {
            "google_places":    True,
            "website_scraped":  bool(website_data.get("raw_text")),
            "listing_site":     bool(listing_match),
            "serper":           any([serper_age, serper_instagram, serper_website, verified_hours]),
        },
        "last_scraped": datetime.now().isoformat(),
        "data_fresh":   True,
        "is_event_based": is_event_based,
        "next_events":    next_events,
    }


# ─────────────────────────────────────────────
# HJÄLPFUNKTIONER
# ─────────────────────────────────────────────

def _clean_json(text: str) -> str:
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith(("[", "{")):
                return part
    return text


def _find_listing_match(club_name: str, listing_clubs: dict) -> dict:
    words = set(club_name.lower().split())
    for listed_name, data in listing_clubs.items():
        if words & set(listed_name.lower().split()):
            return data
    return {}


# ─────────────────────────────────────────────
# HUVUD-AGENT
# ─────────────────────────────────────────────

def run_agent(cities: list = None):
    if cities is None:
        cities = CITIES

    all_clubs      = []
    seen_place_ids = set()
    listing_clubs  = {}

    print("\n" + "═" * 55)
    print("  🎉 PartyPrep Nightclub Agent – Startar")
    print("═" * 55)
    print(f"  Städer: {', '.join(cities)}")
    print(f"  Utfil:  {OUTPUT_FILE}")
    print("═" * 55)

    # ── STEG 1: Listningssajter ─────────────────────────
    print("\n📋 STEG 1 – Scrapar listningssajter...")
    for site in LISTING_SITES:
        if site["city"] not in cities:
            continue
        print(f"  → {site['name']}")
        clubs = scrape_listing_site(site)
        for club in clubs:
            name = club.get("name", "").lower().strip()
            if name:
                listing_clubs[name] = {
                    "age_limit":     club.get("age_limit"),
                    "opening_hours": club.get("opening_hours"),
                    "notes":         club.get("notes", ""),
                    "source":        site["name"],
                }
        print(f"  ✅ {len(clubs)} klubbar från {site['name']}")
        time.sleep(1)

    print(f"\n  📊 {len(listing_clubs)} unika klubbar i förhandsdata")

    # ── STEG 2: Google Places per stad ──────────────────
    for city in cities:
        print(f"\n🔍 STEG 2 – Google Places: {city}...")
        places = search_google_places(city)
        print(f"  ✅ {len(places)} resultat")

        for i, place in enumerate(places):
            pid = place.get("id")
            if not pid or pid in seen_place_ids:
                continue
            seen_place_ids.add(pid)

            name = place.get("displayName", {}).get("text", "Okänd")
            print(f"\n  [{i+1}/{len(places)}] 🎯 {name}")

            print("    → Google Places detaljer...")
            details = get_place_details(pid)
            time.sleep(0.5)

            listing_match = _find_listing_match(name, listing_clubs)
            if listing_match:
                print(f"    ✅ Match i listningssajt: {listing_match.get('source')}")

            # ── Hemsida ──────────────────────────────────
            website_url    = details.get("websiteUri")
            website_data   = {}
            serper_website = None

            if website_url:
                print("    → Scrapar hemsida...")
                website_data = scrape_website(website_url)
                time.sleep(1)
            else:
                print("    → Ingen hemsida i Google – söker via Serper...")
                serper_website = search_website(name, city)
                if serper_website:
                    print(f"    ✅ Hemsida via Serper: {serper_website}")
                    website_data = scrape_website(serper_website)
                    time.sleep(1)

            # ── Claude extraherar info ────────────────────
            print("    → Claude Haiku extraherar info...")
            google_hours = parse_opening_hours(details)
            ai_data = extract_with_claude(
                website_text=website_data.get("raw_text", ""),
                club_name=name,
                listing_info=json.dumps(listing_match, ensure_ascii=False) if listing_match else "",
                google_hours=google_hours,
            )
            time.sleep(0.3)

            # ── Verifiera öppetider via Serper ───────────
            print("    → Verifierar öppetider via Serper...")
            verified_hours = verify_opening_hours(name, city, google_hours)
            if verified_hours.get("verified"):
                conf = verified_hours.get("confidence", "?")
                agree = "✅ överens" if verified_hours.get("sources_agree") else "⚠️ skiljer sig"
                print(f"    📅 Öppetider: {conf} confidence, {agree}")
            time.sleep(0.5)

            # ── Instagram fallback ────────────────────────
            serper_instagram = None
            if not website_data.get("instagram_handle"):
                print("    → Instagram saknas – söker via Serper...")
                serper_instagram = search_instagram(name, city)
                if serper_instagram:
                    print(f"    ✅ Instagram: @{serper_instagram}")
                time.sleep(0.3)

            # ── Åldersgräns fallback ──────────────────────
            serper_age = None
            has_age    = ai_data.get("age_limit") or listing_match.get("age_limit")
            if not has_age:
                print("    → Åldersgräns saknas – söker via Serper...")
                serper_age = search_age_limit(name, city)
                if serper_age:
                    print(f"    ✅ Åldersgräns: {serper_age} år")
                else:
                    print("    ⚠️  Åldersgräns ej hittad")
                time.sleep(0.3)

            # ── Snabb event-sökning för ALLA klubbar ─────
            print("    → Snabb event-sökning (Tickster, Dice, RA...)...")
            quick_events = quick_search_events(name, city)
            if quick_events:
                print(f"    🎫 Hittade {len(quick_events)} events snabbt")
            time.sleep(0.3)

            # ── Eventbaserad detektering + djup scraping ──
            print("    → Kollar om eventbaserad klubb...")
            event_info = detect_event_based_and_scrape(
                name, city,
                website_data.get("raw_text", ""),
                details.get("websiteUri") or serper_website or ""
            )

            # Kombinera snabba events + djupare events, ta bort dubletter och gamla
            today_str   = datetime.now().strftime("%Y-%m-%d")
            seen_events = set()
            all_events  = []
            for e in (quick_events + event_info.get("next_events", [])):
                key  = (e.get("title") or "").lower().strip()
                date = e.get("date") or ""
                # Kräv datum och att det är i framtiden
                if not key or key in seen_events:
                    continue
                if not date or date < today_str:
                    continue
                seen_events.add(key)
                all_events.append(e)
            all_events.sort(key=lambda e: e.get("date") or "9999")
            all_events = all_events[:10]

            if event_info["is_event_based"]:
                print(f"    🎯 Eventbaserad! Totalt {len(all_events)} events")
            elif all_events:
                print(f"    📅 {len(all_events)} events hittade")
            time.sleep(0.3)

            # ── Slå ihop alla källor ──────────────────────
            club = merge_sources(
                details, website_data, ai_data, listing_match, city,
                serper_age, serper_instagram, serper_website, verified_hours,
                event_info["is_event_based"], all_events
            )

            seasonal_str = "ja" if club.get("seasonal") and club["seasonal"].get("is_seasonal") else "nej"
            print(
                f"    ✅ Klar | "
                f"Ålder: {club.get('age_limit', '?')} | "
                f"Öppet: {club['confidence']['opening_hours']} | "
                f"Säsong: {seasonal_str} | "
                f"Bilder: {len(club.get('images', []))} | "
                f"⭐ {club.get('google_rating', '?')} ({club.get('google_reviews', 0)} reviews)"
            )
            all_clubs.append(club)

    # ── STEG 3: Spara ───────────────────────────────────
    print(f"\n💾 STEG 3 – Sparar {len(all_clubs)} klubbar...")

    high_conf     = sum(1 for c in all_clubs if c["confidence"]["opening_hours"] == "high")
    has_age       = sum(1 for c in all_clubs if c.get("age_limit"))
    has_instagram = sum(1 for c in all_clubs if c.get("instagram"))
    has_images    = sum(1 for c in all_clubs if c.get("images"))
    has_website   = sum(1 for c in all_clubs if c.get("website"))
    has_seasonal   = sum(1 for c in all_clubs if c.get("seasonal") and c["seasonal"].get("is_seasonal"))
    verified       = sum(1 for c in all_clubs if c["confidence"].get("hours_verified"))
    event_based    = sum(1 for c in all_clubs if c.get("is_event_based"))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated_at": datetime.now().isoformat(),
                "total_clubs":  len(all_clubs),
                "cities":       cities,
                "stats": {
                    "high_confidence_hours":    high_conf,
                    "hours_serper_verified":    verified,
                    "has_age_limit":            has_age,
                    "has_instagram":            has_instagram,
                    "has_images":               has_images,
                    "has_website":              has_website,
                    "has_seasonal":             has_seasonal,
                    "event_based":              event_based,
                },
            },
            "clubs": all_clubs,
        }, f, ensure_ascii=False, indent=2)

    print("\n" + "═" * 55)
    print("  ✅ KLART!")
    print("═" * 55)
    print(f"  📁 {OUTPUT_FILE}")
    print(f"  🎯 Klubbar:           {len(all_clubs)}")
    print(f"  🕐 Hög säkerhet:      {high_conf}/{len(all_clubs)}")
    print(f"  ✅ Serper-verifierade: {verified}/{len(all_clubs)}")
    print(f"  🔞 Åldersgräns:       {has_age}/{len(all_clubs)}")
    print(f"  📸 Bilder:            {has_images}/{len(all_clubs)}")
    print(f"  📷 Instagram:         {has_instagram}/{len(all_clubs)}")
    print(f"  🌐 Hemsida:           {has_website}/{len(all_clubs)}")
    print(f"  🌊 Säsongsklubbar:    {has_seasonal}/{len(all_clubs)}")
    print(f"  🎫 Eventbaserade:     {event_based}/{len(all_clubs)}")
    print("═" * 55)

    return all_clubs


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # Ta städer från kommandoradsargument: "Stockholm,Malmö,Göteborg"
        cities = [c.strip() for c in sys.argv[1].split(",")]
        print(f"  Kör med städer från argument: {cities}")
    else:
        cities = CITIES
    run_agent(cities=cities)
