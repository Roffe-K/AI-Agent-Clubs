"""
PartyPrep Nightclub Agent
=========================
Söker automatiskt efter nattklubbar via Google Places + listningssajter,
scrapar deras hemsidor, extraherar info med Claude Haiku,
och sparar allt till nightclubs.json

Krav: GOOGLE_API_KEY och CLAUDE_API_KEY i miljövariabler
"""

import os
import json
import time
import requests
from datetime import datetime
from bs4 import BeautifulSoup
import googlemaps
import anthropic

# ─────────────────────────────────────────────
# KONFIGURATION – ändra dessa efter behov
# ─────────────────────────────────────────────

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "DIN_GOOGLE_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "DIN_CLAUDE_API_KEY")

OUTPUT_FILE = "nightclubs.json"

CITIES = ["Stockholm", "Göteborg", "Malmö"]

# Listningssajter att scrapa för att hitta och verifiera klubbar
LISTING_SITES = [
    {
        "url": "https://www.thatsup.se/stockholm/noje/nattliv/",
        "city": "Stockholm",
        "name": "Thatsup Stockholm"
    },
    {
        "url": "https://www.visitstockholm.com/see--do/nightlife/",
        "city": "Stockholm",
        "name": "Visit Stockholm"
    },
    {
        "url": "https://www.goteborg.com/en/nightlife/",
        "city": "Göteborg",
        "name": "Visit Göteborg"
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Max 12 månader gammal data – annars markeras som stale
MAX_DATA_AGE_MONTHS = 12

# ─────────────────────────────────────────────
# INITIERING AV KLIENTER
# ─────────────────────────────────────────────

gmaps = googlemaps.Client(key=GOOGLE_API_KEY)
claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


# ─────────────────────────────────────────────
# GOOGLE PLACES
# ─────────────────────────────────────────────

def search_google_places(city: str) -> list:
    """Söker nattklubbar i en stad via Google Places API"""
    all_results = []
    seen_ids = set()

    queries = [f"nattklubb {city}", f"nightclub {city}", f"club {city}"]

    for query in queries:
        try:
            response = gmaps.places(query, type="night_club")
            results = response.get("results", [])
            all_results.extend(
                r for r in results if r["place_id"] not in seen_ids
            )
            seen_ids.update(r["place_id"] for r in results)

            # Hämta fler sidor
            while "next_page_token" in response:
                time.sleep(2)  # Google kräver delay
                response = gmaps.places(query, page_token=response["next_page_token"])
                new = [r for r in response.get("results", []) if r["place_id"] not in seen_ids]
                all_results.extend(new)
                seen_ids.update(r["place_id"] for r in new)

        except Exception as e:
            print(f"    ⚠️ Google Places-fel för '{query}': {e}")

    return all_results


def get_place_details(place_id: str) -> dict:
    """Hämtar detaljerad info för ett ställe från Google Places"""
    try:
        result = gmaps.place(
            place_id,
            fields=[
                "name",
                "formatted_address",
                "geometry",
                "opening_hours",
                "website",
                "photos",
                "formatted_phone_number",
                "url",
                "rating",
                "user_ratings_total",
            ]
        )
        return result.get("result", {})
    except Exception as e:
        print(f"    ⚠️ Kunde inte hämta detaljer: {e}")
        return {}


def get_place_images(photos: list, api_key: str, max_images: int = 4) -> list:
    """Bygger bild-URL:er från Google Places photo references"""
    images = []
    for photo in photos[:max_images]:
        ref = photo.get("photo_reference")
        if ref:
            url = (
                f"https://maps.googleapis.com/maps/api/place/photo"
                f"?maxwidth=1200&photoreference={ref}&key={api_key}"
            )
            images.append(url)
    return images


# ─────────────────────────────────────────────
# LISTNINGSSAJTER
# ─────────────────────────────────────────────

def scrape_listing_site(site: dict) -> list:
    """Scrapar en listningssajt och extraherar klubbnamn med Claude"""
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Ta bort navigation/footer-brus
        for tag in soup(["nav", "footer", "script", "style", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)[:6000]

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": f"""Du läser en lista med nattklubbar från sajten '{site["name"]}'.
Extrahera ALLA nattklubbar/barer/nattklubbsliknande ställen som nämns.

Svara ENBART med JSON-array (inga förklaringar, inga backticks):
[{{"name": "Klubbnamn", "age_limit": 20, "opening_hours": "Fre-Lör 22-05", "notes": "Eventuell extra info"}}]

Om du inte hittar åldersgräns eller öppetider, sätt null.

Text:
{text}"""
            }]
        )

        raw = response.content[0].text.strip()
        raw = _clean_json_response(raw)
        return json.loads(raw)

    except Exception as e:
        print(f"    ⚠️ Kunde inte scrapa {site['name']}: {e}")
        return []


# ─────────────────────────────────────────────
# HEMSIDA-SCRAPING
# ─────────────────────────────────────────────

def scrape_website(url: str) -> dict:
    """Scrapar en klubbs hemsida efter info och Instagram-länk"""
    if not url:
        return {}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Hitta Instagram-länk
        instagram_handle = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "instagram.com/" in href:
                handle = href.rstrip("/").split("instagram.com/")[-1].split("/")[0].split("?")[0]
                if handle and handle not in ("", "p", "stories", "reel"):
                    instagram_handle = handle
                    break

        # Rensa texten
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:5000]

        return {
            "instagram_handle": instagram_handle,
            "raw_text": text
        }

    except Exception as e:
        print(f"    ⚠️ Kunde inte scrapa hemsida: {e}")
        return {}


# ─────────────────────────────────────────────
# CLAUDE AI-EXTRAKTION
# ─────────────────────────────────────────────

def extract_with_claude(
    website_text: str,
    club_name: str,
    listing_info: str = "",
    google_hours: list = None
) -> dict:
    """Använder Claude Haiku för att extrahera strukturerad info från hemsidetext"""

    if not website_text and not listing_info:
        return {}

    google_hours_str = ", ".join(google_hours) if google_hours else "ej tillgänglig"

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{
            "role": "user",
            "content": f"""Du är en expert på att extrahera information om nattklubbar.
Extrahera info för: '{club_name}'

Google Places öppetider (för jämförelse): {google_hours_str}
Info från listningssajter: {listing_info if listing_info else 'ingen'}
Hemsidetext: {website_text[:3000] if website_text else 'ej tillgänglig'}

Svara ENBART med JSON (inga backticks, inga förklaringar):
{{
  "age_limit": 23,
  "opening_hours": {{
    "monday": "stängt",
    "tuesday": "stängt",
    "wednesday": "22:00-03:00",
    "thursday": "22:00-03:00",
    "friday": "22:00-05:00",
    "saturday": "22:00-05:00",
    "sunday": "stängt"
  }},
  "seasonal_info": "Öppet sommarsäsong juni-aug, stängt övrig tid",
  "description": "Kort beskrivning av klubbens karaktär och musikstil",
  "dress_code": "Klädkod om nämnd, annars null",
  "hours_confidence": "high/medium/low baserat på hur tydlig info var"
}}

Sätt null för fält du inte hittar information om.
Åldersgränsen MÅSTE vara officiell (ej uppskattning). Vanliga värden: 18, 20, 23.
"""
        }]
    )

    raw = response.content[0].text.strip()
    raw = _clean_json_response(raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ─────────────────────────────────────────────
# MERGE & CONFIDENCE
# ─────────────────────────────────────────────

def merge_sources(
    details: dict,
    website_data: dict,
    ai_data: dict,
    listing_match: dict,
    city: str
) -> dict:
    """Slår ihop alla datakällor med confidence-scoring"""

    google_hours = details.get("opening_hours", {}).get("weekday_text", [])
    ai_hours = ai_data.get("opening_hours")

    # Öppetider – välj källa
    if ai_hours and google_hours:
        opening_hours = ai_hours
        hours_confidence = "high"
        hours_source = "website + google (jämförda)"
    elif ai_hours:
        opening_hours = ai_hours
        hours_confidence = "medium"
        hours_source = "website"
    elif google_hours:
        opening_hours = {"google_format": google_hours}
        hours_confidence = "medium"
        hours_source = "google places"
    else:
        opening_hours = None
        hours_confidence = "low"
        hours_source = "saknas"

    # Åldersgräns – kombinera listning + AI
    age_limit = ai_data.get("age_limit") or listing_match.get("age_limit")
    age_confidence = "high" if age_limit else "unknown"

    # Instagram
    instagram_handle = website_data.get("instagram_handle")
    instagram_url = f"https://instagram.com/{instagram_handle}" if instagram_handle else None

    # Bilder
    images = get_place_images(
        details.get("photos", []),
        api_key=GOOGLE_API_KEY
    )

    return {
        # ─── Identitet ───
        "name": details.get("name"),
        "city": city,
        "description": ai_data.get("description"),

        # ─── Plats ───
        "address": details.get("formatted_address"),
        "lat": details.get("geometry", {}).get("location", {}).get("lat"),
        "lng": details.get("geometry", {}).get("location", {}).get("lng"),
        "google_maps_url": details.get("url"),

        # ─── Tider ───
        "opening_hours": opening_hours,
        "seasonal_info": ai_data.get("seasonal_info"),

        # ─── Tillträde ───
        "age_limit": age_limit,
        "dress_code": ai_data.get("dress_code"),

        # ─── Kontakt & media ───
        "website": details.get("website"),
        "instagram": instagram_url,
        "phone": details.get("formatted_phone_number"),
        "images": images,

        # ─── Google-metadata ───
        "google_rating": details.get("rating"),
        "google_reviews": details.get("user_ratings_total"),

        # ─── Datakvalitet ───
        "confidence": {
            "opening_hours": hours_confidence,
            "opening_hours_source": hours_source,
            "age_limit": age_confidence,
        },
        "sources_used": {
            "google_places": True,
            "website_scraped": bool(website_data.get("raw_text")),
            "listing_site": bool(listing_match),
        },
        "last_scraped": datetime.now().isoformat(),
        "data_fresh": True,  # Alltid True när den just scrapats
    }


# ─────────────────────────────────────────────
# HJÄLPFUNKTIONER
# ─────────────────────────────────────────────

def _clean_json_response(text: str) -> str:
    """Rensar Claude-svar från eventuella markdown-backticks"""
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("[") or part.startswith("{"):
                return part
    return text


def _find_listing_match(club_name: str, listing_clubs: dict) -> dict:
    """Matchar ett klubbnamn mot data från listningssajter (fuzzy)"""
    name_lower = club_name.lower()
    for listed_name, data in listing_clubs.items():
        # Enkel fuzzy match: kolla om namnen delar ord
        words = set(name_lower.split())
        listed_words = set(listed_name.lower().split())
        if words & listed_words:  # Gemensamma ord
            return data
    return {}


# ─────────────────────────────────────────────
# HUVUD-AGENT
# ─────────────────────────────────────────────

def run_agent(cities: list = None):
    """Kör hela pipelinen och sparar resultat till JSON"""

    if cities is None:
        cities = CITIES

    all_clubs = []
    seen_place_ids = set()
    listing_clubs = {}  # {namn: {age_limit, opening_hours, notes}}

    print("\n" + "═" * 55)
    print("  🎉 PartyPrep Nightclub Agent – Startar")
    print("═" * 55)
    print(f"  Städer: {', '.join(cities)}")
    print(f"  Utfil:  {OUTPUT_FILE}")
    print("═" * 55)

    # ── STEG 1: Listningssajter ──────────────────────────
    print("\n📋 STEG 1 – Scrapar listningssajter för förhandsdata...")
    for site in LISTING_SITES:
        if site["city"] not in cities:
            continue
        print(f"  → {site['name']} ({site['url'][:50]}...)")
        clubs = scrape_listing_site(site)
        for club in clubs:
            name = club.get("name", "").lower().strip()
            if name:
                listing_clubs[name] = {
                    "age_limit": club.get("age_limit"),
                    "opening_hours": club.get("opening_hours"),
                    "notes": club.get("notes", ""),
                    "source": site["name"],
                }
        print(f"  ✅ Hittade {len(clubs)} klubbar från {site['name']}")
        time.sleep(1)

    print(f"\n  📊 Totalt {len(listing_clubs)} unika klubbar från listningssajter")

    # ── STEG 2: Google Places per stad ───────────────────
    for city in cities:
        print(f"\n🔍 STEG 2 – Google Places: söker i {city}...")
        places = search_google_places(city)
        print(f"  ✅ Hittade {len(places)} resultat")

        for i, place in enumerate(places):
            place_id = place["place_id"]
            if place_id in seen_place_ids:
                continue
            seen_place_ids.add(place_id)

            name = place.get("name", "Okänd")
            print(f"\n  [{i+1}/{len(places)}] 🎯 {name}")

            # Google Places-detaljer
            print("    → Google Places detaljer...")
            details = get_place_details(place_id)
            time.sleep(0.5)

            # Kolla listningssajt-match
            listing_match = _find_listing_match(name, listing_clubs)
            if listing_match:
                print(f"    ✅ Matchar listningssajt: {listing_match.get('source', '?')}")

            # Scrapa hemsida
            website_url = details.get("website")
            website_data = {}
            if website_url:
                print(f"    → Scrapar hemsida...")
                website_data = scrape_website(website_url)
                time.sleep(1)
            else:
                print("    ⚠️  Ingen hemsida hittad i Google Places")

            # Claude extraherar strukturerad info
            google_hours = details.get("opening_hours", {}).get("weekday_text", [])
            listing_info_str = json.dumps(listing_match, ensure_ascii=False) if listing_match else ""

            print("    → Claude Haiku extraherar info...")
            ai_data = extract_with_claude(
                website_text=website_data.get("raw_text", ""),
                club_name=name,
                listing_info=listing_info_str,
                google_hours=google_hours
            )
            time.sleep(0.3)

            # Slå ihop alla källor
            club = merge_sources(details, website_data, ai_data, listing_match, city)

            # Logg
            age_str = str(club.get("age_limit")) + " år" if club.get("age_limit") else "okänd"
            hours_conf = club["confidence"]["opening_hours"]
            ig = f"@{website_data.get('instagram_handle')}" if website_data.get("instagram_handle") else "saknas"
            print(f"    ✅ Klar | Ålder: {age_str} | Öppet (confidence: {hours_conf}) | Instagram: {ig}")

            all_clubs.append(club)

    # ── STEG 3: Spara till fil ───────────────────────────
    print(f"\n💾 STEG 3 – Sparar {len(all_clubs)} klubbar till {OUTPUT_FILE}...")

    # Statistik
    high_conf = sum(1 for c in all_clubs if c["confidence"]["opening_hours"] == "high")
    has_age = sum(1 for c in all_clubs if c.get("age_limit"))
    has_instagram = sum(1 for c in all_clubs if c.get("instagram"))
    has_images = sum(1 for c in all_clubs if c.get("images"))

    output = {
        "meta": {
            "generated_at": datetime.now().isoformat(),
            "total_clubs": len(all_clubs),
            "cities": cities,
            "stats": {
                "opening_hours_high_confidence": high_conf,
                "has_age_limit": has_age,
                "has_instagram": has_instagram,
                "has_images": has_images,
            }
        },
        "clubs": all_clubs
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n" + "═" * 55)
    print("  ✅ KLART!")
    print("═" * 55)
    print(f"  📁 Fil:              {OUTPUT_FILE}")
    print(f"  🎯 Totalt klubbar:   {len(all_clubs)}")
    print(f"  🕐 Hög öppet-säkerhet: {high_conf}/{len(all_clubs)}")
    print(f"  🔞 Har åldersgräns:  {has_age}/{len(all_clubs)}")
    print(f"  📸 Har bilder:       {has_images}/{len(all_clubs)}")
    print(f"  📷 Har Instagram:    {has_instagram}/{len(all_clubs)}")
    print("═" * 55)

    return all_clubs


# ─────────────────────────────────────────────
# KÖR
# ─────────────────────────────────────────────

if __name__ == "__main__":
    run_agent(cities=["Stockholm"])  # Ändra till fler städer vid behov
