"""
PartyPrep Nightclub Agent
=========================
Söker automatiskt efter nattklubbar via Google Places (New API) + listningssajter,
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
import anthropic

# ─────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "DIN_GOOGLE_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "DIN_CLAUDE_API_KEY")

OUTPUT_FILE = "nightclubs.json"
CITIES      = ["Stockholm"]  # Lägg till "Göteborg", "Malmö" vid behov

LISTING_SITES = [
    {"url": "https://www.thatsup.se/stockholm/noje/nattliv/",   "city": "Stockholm", "name": "Thatsup Stockholm"},
    {"url": "https://www.visitstockholm.com/see--do/nightlife/","city": "Stockholm", "name": "Visit Stockholm"},
    {"url": "https://www.goteborg.com/en/nightlife/",           "city": "Göteborg",  "name": "Visit Göteborg"},
]

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── Google Places (New) API ────────────────
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
# GOOGLE PLACES (NEW API)
# ─────────────────────────────────────────────

def _gplaces_headers(field_mask: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
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
                    "textQuery":     query,
                    "includedType":  "night_club",
                    "languageCode":  "sv",
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
                f"{PLACES_PHOTO_URL.format(photo_name=name)}?maxWidthPx=1200&key={GOOGLE_API_KEY}"
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
                handle = href.rstrip("/").split("instagram.com/")[-1].split("/")[0].split("?")[0]
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

def extract_with_claude(website_text: str, club_name: str, listing_info: str = "", google_hours: list = None) -> dict:
    if not website_text and not listing_info:
        return {}

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": f"""Extrahera info om nattklubben '{club_name}'.

Google öppetider: {', '.join(google_hours) if google_hours else 'saknas'}
Listningssajt-info: {listing_info or 'ingen'}
Hemsidetext: {website_text[:3000] or 'saknas'}

Svara ENBART med JSON (inga backticks):
{{
  "age_limit": 23,
  "opening_hours": {{"monday":"stängt","tuesday":"stängt","wednesday":"stängt","thursday":"22:00-03:00","friday":"22:00-05:00","saturday":"22:00-05:00","sunday":"stängt"}},
  "seasonal_info": null,
  "description": "Kort beskrivning",
  "dress_code": null,
  "hours_confidence": "high"
}}
Sätt null om info saknas. Åldersgräns måste vara officiell (18/20/23).
"""}]
    )

    try:
        return json.loads(_clean_json(response.content[0].text.strip()))
    except:
        return {}


# ─────────────────────────────────────────────
# MERGE & CONFIDENCE
# ─────────────────────────────────────────────

def merge_sources(details: dict, website_data: dict, ai_data: dict, listing_match: dict, city: str) -> dict:
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

    age_limit = ai_data.get("age_limit") or listing_match.get("age_limit")
    handle    = website_data.get("instagram_handle")

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
        "website":         details.get("websiteUri"),
        "instagram":       f"https://instagram.com/{handle}" if handle else None,
        "phone":           details.get("nationalPhoneNumber"),
        "images":          get_place_images(details.get("photos", [])),
        "google_rating":   details.get("rating"),
        "google_reviews":  details.get("userRatingCount"),
        "confidence": {
            "opening_hours":        hours_confidence,
            "opening_hours_source": hours_source,
            "age_limit":            "high" if age_limit else "unknown",
        },
        "sources_used": {
            "google_places":   True,
            "website_scraped": bool(website_data.get("raw_text")),
            "listing_site":    bool(listing_match),
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

    # STEG 1: Listningssajter
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

    # STEG 2: Google Places per stad
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

            website_url  = details.get("websiteUri")
            website_data = {}
            if website_url:
                print("    → Scrapar hemsida...")
                website_data = scrape_website(website_url)
                time.sleep(1)
            else:
                print("    ⚠️  Ingen hemsida")

            print("    → Claude Haiku extraherar info...")
            ai_data = extract_with_claude(
                website_text=website_data.get("raw_text", ""),
                club_name=name,
                listing_info=json.dumps(listing_match, ensure_ascii=False) if listing_match else "",
                google_hours=parse_opening_hours(details),
            )
            time.sleep(0.3)

            club = merge_sources(details, website_data, ai_data, listing_match, city)

            print(
                f"    ✅ Klar | "
                f"Ålder: {club.get('age_limit', '?')} | "
                f"Öppet: {club['confidence']['opening_hours']} | "
                f"Instagram: {'ja' if club.get('instagram') else 'nej'}"
            )
            all_clubs.append(club)

    # STEG 3: Spara
    print(f"\n💾 STEG 3 – Sparar {len(all_clubs)} klubbar...")

    high_conf     = sum(1 for c in all_clubs if c["confidence"]["opening_hours"] == "high")
    has_age       = sum(1 for c in all_clubs if c.get("age_limit"))
    has_instagram = sum(1 for c in all_clubs if c.get("instagram"))
    has_images    = sum(1 for c in all_clubs if c.get("images"))

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
    print("═" * 55)

    return all_clubs


if __name__ == "__main__":
    run_agent(cities=["Stockholm"])
