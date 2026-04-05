"""
PartyPrep Nightclub Agent
=========================
Pipeline:
  1. Scrapa listningssajter (Thatsup, VisitStockholm) för förhandsdata
  2. Google Places (New API) – hitta klubbar, adress, öppetider, bilder
  3. Scrapa varje klubbs hemsida – Instagram, åldersgräns, säsong
  4. Claude Haiku – strukturera och jämför alla källor
  5. Serper (Google) – fallback för hemsida, Instagram och åldersgräns
  6. Spara till nightclubs.json

Krav (GitHub Secrets):
  GOOGLE_API_KEY  – Google Cloud API-nyckel (Places API aktiverat)
  CLAUDE_API_KEY  – Anthropic API-nyckel
  SERPER_API_KEY  – Serper.dev API-nyckel (google.serper.dev)
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
CITIES      = ["Stockholm"]  # Lägg till "Göteborg", "Malmö" vid behov

LISTING_SITES = [
    {"url": "https://www.thatsup.se/stockholm/noje/nattliv/",    "city": "Stockholm", "name": "Thatsup Stockholm"},
    {"url": "https://www.visitstockholm.com/see--do/nightlife/", "city": "Stockholm", "name": "Visit Stockholm"},
    {"url": "https://www.goteborg.com/en/nightlife/",            "city": "Göteborg",  "name": "Visit Göteborg"},
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

def serper_search(query: str, num: int = 5) -> list:
    """Gör en Google-sökning via Serper och returnerar organiska resultat."""
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY":    SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as e:
        print(f"    ⚠️ Serper-fel för '{query}': {e}")
        return []


# ─────────────────────────────────────────────
# SERPER – HITTA HEMSIDA
# ─────────────────────────────────────────────

def search_website(club_name: str, city: str) -> str | None:
    """
    Söker upp klubbens hemsida via Serper när Google Places inte har den.
    Hanterar även klubbar som drivs av restauranger eller venues med annat namn.
    """
    results = serper_search(f"{club_name} {city} officiell hemsida nightclub", num=5)

    if not results:
        return None

    # Bygg en lista av kandidat-URLs
    candidates = []
    for r in results:
        url = r.get("link", "")
        title = r.get("title", "").lower()
        snippet = r.get("snippet", "").lower()

        # Skippa uppenbart irrelevanta sidor
        skip_domains = ["tripadvisor", "yelp", "facebook", "instagram",
                        "google", "wikipedia", "thatsup", "visitstockholm"]
        if any(d in url for d in skip_domains):
            continue

        candidates.append({
            "url":     url,
            "title":   title,
            "snippet": snippet,
        })

    if not candidates:
        return None

    # Claude väljer den mest troliga hemsidan
    candidates_str = "\n".join(
        f"{i+1}. {c['url']} – {c['title']}"
        for i, c in enumerate(candidates[:5])
    )

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"""Which URL is the official website for the nightclub/venue '{club_name}' in {city}?
Note: the club might be owned by a restaurant or bar with a different name.
Reply with ONLY the URL, or null if none match.

Candidates:
{candidates_str}"""}]
    )

    answer = response.content[0].text.strip()
    if answer.startswith("http") and "." in answer:
        return answer
    return None


# ─────────────────────────────────────────────
# SERPER – HITTA INSTAGRAM
# ─────────────────────────────────────────────

def search_instagram(club_name: str, city: str) -> str | None:
    """
    Söker upp klubbens Instagram-handle via Serper.
    Söker både på klubbnamnet och eventuellt ägarföretag.
    """
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
    Söker åldersgräns via Serper med flera olika sökfraser.
    Claude tolkar resultaten och extraherar siffran.
    """
    # Prova flera sökfraser för bästa täckning
    queries = [
        f"{club_name} åldersgräns {city}",
        f"{club_name} age limit {city} nightclub",
        f"{club_name} {city} minimum age entry",
    ]

    all_snippets = []
    for query in queries:
        results = serper_search(query, num=5)
        for r in results:
            snippet = r.get("snippet", "")
            title   = r.get("title", "")
            if snippet:
                all_snippets.append(f"{title}: {snippet}")
        if all_snippets:
            break  # Räcker med bra resultat från första query

    if not all_snippets:
        return None

    snippets_str = "\n".join(all_snippets[:8])

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": f"""What is the official minimum age to enter '{club_name}' in {city}?
Reply ONLY with a number: 18, 20, 21, 23 — or null if not found. Never guess.

Text:
{snippets_str}"""}]
    )

    answer = response.content[0].text.strip()
    if answer.isdigit() and int(answer) in (18, 20, 21, 23):
        return int(answer)
    return None


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


def get_place_images(photos: list, max_images: int = 4) -> list:
    images = []
    for photo in photos[:max_images]:
        name = photo.get("name")
        if name:
            images.append(
                f"{PLACES_PHOTO_URL.format(photo_name=name)}"
                f"?maxWidthPx=1200&key={GOOGLE_API_KEY}"
            )
    return images


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
Extrahera ALLA nattklubbar som nämns. Svara ENBART med JSON-array (inga backticks):
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
    listing_info: str = "",
    google_hours: list = None,
) -> dict:
    if not website_text and not listing_info:
        return {}

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": f"""Extrahera info om nattklubben/venuen '{club_name}'.
OBS: Klubben kan vara en restaurang eller bar på dagtid med nattklubb på kvällen.

Åldersgräns hittas ofta som: "18 år", "20-årsgräns", "åldersgräns 23",
"minimum age", "du måste vara minst X år", "entry age".
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
  "seasonal_info": null,
  "description": "Kort beskrivning av klubbens karaktär och musikstil",
  "dress_code": null,
  "hours_confidence": "high"
}}
Sätt null om info saknas. Åldersgräns måste vara officiell.
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
) -> dict:
    google_hours = parse_opening_hours(details)
    ai_hours     = ai_data.get("opening_hours")

    if ai_hours and google_hours:
        opening_hours    = ai_hours
        hours_confidence = "high"
        hours_source     = "website + google"
    elif ai_hours:
        opening_hours    = ai_hours
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

    # Instagram – prioritetsordning
    instagram_handle = (
        website_data.get("instagram_handle")
        or serper_instagram
    )
    instagram_source = (
        "website" if website_data.get("instagram_handle") else
        "serper"  if serper_instagram                     else
        "saknas"
    )

    # Hemsida – prioritetsordning
    website = (
        details.get("websiteUri")
        or serper_website
    )

    return {
        "name":            details.get("displayName", {}).get("text", "Okänd"),
        "city":            city,
        "description":     ai_data.get("description"),
        "address":         details.get("formattedAddress"),
        "lat":             details.get("location", {}).get("latitude"),
        "lng":             details.get("location", {}).get("longitude"),
        "google_maps_url": details.get("googleMapsUri"),
        "opening_hours":   opening_hours,
        "seasonal_info":   ai_data.get("seasonal_info"),
        "age_limit":       age_limit,
        "dress_code":      ai_data.get("dress_code"),
        "website":         website,
        "instagram":       f"https://instagram.com/{instagram_handle}" if instagram_handle else None,
        "phone":           details.get("nationalPhoneNumber"),
        "images":          get_place_images(details.get("photos", [])),
        "google_rating":   details.get("rating"),
        "google_reviews":  details.get("userRatingCount"),
        "confidence": {
            "opening_hours":        hours_confidence,
            "opening_hours_source": hours_source,
            "age_limit":            "high" if age_limit else "unknown",
            "age_limit_source":     age_source,
            "instagram_source":     instagram_source,
        },
        "sources_used": {
            "google_places":   True,
            "website_scraped": bool(website_data.get("raw_text")),
            "listing_site":    bool(listing_match),
            "serper":          any([serper_age, serper_instagram, serper_website]),
        },
        "last_scraped": datetime.now().isoformat(),
        "data_fresh":   True,
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
            website_url  = details.get("websiteUri")
            website_data = {}
            serper_website = None

            if website_url:
                print("    → Scrapar hemsida...")
                website_data = scrape_website(website_url)
                time.sleep(1)
            else:
                print("    → Ingen hemsida i Google Places – söker via Serper...")
                serper_website = search_website(name, city)
                if serper_website:
                    print(f"    ✅ Hittade hemsida via Serper: {serper_website}")
                    website_data = scrape_website(serper_website)
                    time.sleep(1)
                else:
                    print("    ⚠️  Ingen hemsida hittad")

            # ── Claude extraherar info ────────────────────
            print("    → Claude Haiku extraherar info...")
            ai_data = extract_with_claude(
                website_text=website_data.get("raw_text", ""),
                club_name=name,
                listing_info=json.dumps(listing_match, ensure_ascii=False) if listing_match else "",
                google_hours=parse_opening_hours(details),
            )
            time.sleep(0.3)

            # ── Instagram fallback ────────────────────────
            serper_instagram = None
            if not website_data.get("instagram_handle"):
                print("    → Instagram saknas – söker via Serper...")
                serper_instagram = search_instagram(name, city)
                if serper_instagram:
                    print(f"    ✅ Hittade Instagram via Serper: @{serper_instagram}")
                time.sleep(0.3)

            # ── Åldersgräns fallback ──────────────────────
            serper_age = None
            has_age = ai_data.get("age_limit") or listing_match.get("age_limit")
            if not has_age:
                print("    → Åldersgräns saknas – söker via Serper...")
                serper_age = search_age_limit(name, city)
                if serper_age:
                    print(f"    ✅ Hittade åldersgräns via Serper: {serper_age} år")
                else:
                    print("    ⚠️  Åldersgräns ej hittad")
                time.sleep(0.3)

            # ── Slå ihop alla källor ──────────────────────
            club = merge_sources(
                details, website_data, ai_data, listing_match, city,
                serper_age, serper_instagram, serper_website
            )

            print(
                f"    ✅ Klar | "
                f"Ålder: {club.get('age_limit', '?')} ({club['confidence']['age_limit_source']}) | "
                f"Öppet: {club['confidence']['opening_hours']} | "
                f"Instagram: {'ja (' + club['confidence']['instagram_source'] + ')' if club.get('instagram') else 'nej'}"
            )
            all_clubs.append(club)

    # ── STEG 3: Spara ───────────────────────────────────
    print(f"\n💾 STEG 3 – Sparar {len(all_clubs)} klubbar...")

    high_conf     = sum(1 for c in all_clubs if c["confidence"]["opening_hours"] == "high")
    has_age       = sum(1 for c in all_clubs if c.get("age_limit"))
    has_instagram = sum(1 for c in all_clubs if c.get("instagram"))
    has_images    = sum(1 for c in all_clubs if c.get("images"))
    has_website   = sum(1 for c in all_clubs if c.get("website"))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated_at": datetime.now().isoformat(),
                "total_clubs":  len(all_clubs),
                "cities":       cities,
                "stats": {
                    "high_confidence_hours": high_conf,
                    "has_age_limit":         has_age,
                    "has_instagram":         has_instagram,
                    "has_images":            has_images,
                    "has_website":           has_website,
                },
            },
            "clubs": all_clubs,
        }, f, ensure_ascii=False, indent=2)

    print("\n" + "═" * 55)
    print("  ✅ KLART!")
    print("═" * 55)
    print(f"  📁 {OUTPUT_FILE}")
    print(f"  🎯 Klubbar:      {len(all_clubs)}")
    print(f"  🕐 Hög säkerhet: {high_conf}/{len(all_clubs)}")
    print(f"  🔞 Åldersgräns:  {has_age}/{len(all_clubs)}")
    print(f"  📸 Bilder:       {has_images}/{len(all_clubs)}")
    print(f"  📷 Instagram:    {has_instagram}/{len(all_clubs)}")
    print(f"  🌐 Hemsida:      {has_website}/{len(all_clubs)}")
    print("═" * 55)

    return all_clubs


if __name__ == "__main__":
    run_agent(cities=["Stockholm"])
