# 🎉 PartyPrep Nightclub Agent

Automatisk AI-agent som söker nattklubbar, scrapar deras info från flera källor,
och sparar strukturerad data till `nightclubs.json` – redo för Supabase & Lovable.

## Datakällor

| Källa | Vad den ger |
|---|---|
| Google Places API | Namn, adress, lat/lng, öppetider, bilder, hemsida |
| Klubbens hemsida | Åldersgräns, säsongsinfo, Instagram-länk |
| Thatsup / Visit-sajter | Verifiering + extra info |
| Claude Haiku | Jämför & strukturerar alla källor |

## Dataformat (nightclubs.json)

```json
{
  "name": "Sturecompagniet",
  "city": "Stockholm",
  "address": "Sturegatan 4, 114 35 Stockholm",
  "lat": 59.336,
  "lng": 18.073,
  "opening_hours": {
    "friday": "22:00-05:00",
    "saturday": "22:00-05:00"
  },
  "age_limit": 23,
  "instagram": "https://instagram.com/sturecompagniet",
  "website": "https://sturecompagniet.se",
  "images": ["https://...google-places-bild..."],
  "confidence": {
    "opening_hours": "high",
    "age_limit": "high"
  },
  "last_scraped": "2025-04-01T10:00:00"
}
```

## Setup

### 1. Klona repot & installera
```bash
git clone https://github.com/ditt-repo/nightclub-agent
cd nightclub-agent
pip install -r requirements.txt
```

### 2. Skapa API-nycklar
- **Google Places API**: https://console.cloud.google.com → aktivera "Places API"
- **Claude API**: https://console.anthropic.com

### 3. Konfigurera miljövariabler
```bash
cp .env.example .env
# Redigera .env med dina nycklar
```

### 4. Kör agenten
```bash
python nightclub_agent.py
```

## GitHub Actions (automatisk körning)

1. Gå till ditt GitHub-repo → Settings → Secrets
2. Lägg till:
   - `GOOGLE_API_KEY`
   - `CLAUDE_API_KEY`
3. Agenten kör automatiskt 1:a varje månad
4. Kör manuellt: Actions → "Uppdatera nattklubbar" → Run workflow

## Ändra städer

I `nightclub_agent.py`, längst ned:
```python
run_agent(cities=["Stockholm", "Göteborg", "Malmö"])
```

## Kostnad

| Tjänst | Free tier | Förväntad kostnad |
|---|---|---|
| Google Places | $200 kredit/mån | Gratis för <500 klubbar |
| Claude Haiku | - | ~$0.01/mån |
| GitHub Actions | 2000 min/mån | Gratis |
| **Totalt** | | **~$0/mån** |

## Nästa steg: Supabase

När du är nöjd med datan, lägg till detta i slutet av `run_agent()`:
```python
from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
for club in all_clubs:
    supabase.table("nightclubs").upsert(club, on_conflict="name,city").execute()
```
