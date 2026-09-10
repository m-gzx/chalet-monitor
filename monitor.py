#!/usr/bin/env python3
"""
Moniteur de chalets à vendre bord de l'eau, à 2h de route ou moins de G3A2P8.

Flux :
  1. Géocode le point de départ (G3A2P8) avec Nominatim (OpenStreetMap).
  2. Interroge Centris pour les chalets à vendre dans un large rayon autour
     de ce point, avec le filtre "bord de l'eau".
  3. Filtre les résultats par temps de route réel (via OSRM), pas juste
     à vol d'oiseau.
  4. Compare avec les annonces déjà vues (state.json) pour ne garder que
     les nouvelles.
  5. Génère un rapport HTML autonome (carte + fiches cliquables) et
     l'ouvre dans le navigateur par défaut.

Nécessite : pip install -r requirements.txt
Aucune configuration/secret requis — ajuster les constantes ci-dessous
au besoin (code postal, rayon, temps de route max).
"""

import base64
import json
import time
import webbrowser
from pathlib import Path

import requests
from staticmap import CircleMarker, StaticMap

# --- CONFIGURATION ---
ORIGIN_POSTAL_CODE = "G3A2P8"
MAX_DRIVE_HOURS = 2.0
SEARCH_RADIUS_KM = 180  # rayon large à vol d'oiseau, filtré ensuite par temps de route réel

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
MAP_FILE = BASE_DIR / "map.png"
REPORT_FILE = BASE_DIR / "report.html"


def geocode_postal_code(postal_code: str) -> tuple[float, float]:
    """Convertit un code postal canadien en (lat, lon) via Nominatim."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"postalcode": postal_code, "country": "Canada", "format": "json"}
    headers = {"User-Agent": "chalet-monitor-personnel/1.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Impossible de géocoder {postal_code}")
    return float(data[0]["lat"]), float(data[0]["lon"])


def driving_time_minutes(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    """
    Temps de route via OSRM (serveur public de démonstration).
    Usage personnel léger uniquement — ne pas appeler en boucle serrée.
    """
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    )
    try:
        resp = requests.get(url, params={"overview": "false"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            return float("inf")
        return data["routes"][0]["duration"] / 60
    except requests.RequestException:
        return float("inf")


def search_centris_waterfront_cottages(origin: tuple[float, float], radius_km: float) -> list[dict]:
    """
    Interroge l'endpoint interne de la carte Centris (GetMarkers), capturé
    par inspection réseau (F12) le 2026-09-10 sur une recherche
    Chalet + Terrain + Bord de l'eau + Villégiature.

    ATTENTION — Cloudflare : Centris est protégé par Cloudflare. La requête
    capturée incluait des cookies de session (dont `cf_clearance`), obtenus
    en résolvant un défi JavaScript qu'un simple `requests.post()` ne peut
    pas faire. Sans session valide, cet appel risque de recevoir un 403 au
    lieu du JSON attendu — surtout après expiration du cookie (quelques
    heures à quelques jours). Voir la note dans le README sur l'option
    Playwright pour obtenir une session valide avant chaque exécution.

    ENCORE À FAIRE : le format exact de la réponse (noms des champs par
    annonce — id, lat/lon, prix, adresse, url) n'a pas encore été confirmé.
    Le code ci-dessous suppose une liste sous la clé "markers" avec des
    champs à valider/ajuster une fois un échantillon de réponse examiné.
    """
    url = "https://www.centris.ca/api/property/map/GetMarkers"
    lat, lon = origin
    delta = radius_km / 111  # ~111 km par degré de latitude

    payload = {
        "zoomLevel": 6,
        "mapBounds": {
            "NorthEast": {"Lat": lat + delta, "Lng": lon + delta},
            "SouthWest": {"Lat": lat - delta, "Lng": lon - delta},
        },
        "mode": "Result",
        "sort": "None",
        "sortSeed": 1,
        "query": {
            "SearchName": "",
            "UseGeographyShapes": 0,
            "Filters": [],
            "FieldsValues": [
                {"fieldId": "PropertyType", "value": "Chalet", "fieldConditionId": "", "valueConditionId": "IsResidential"},
                {"fieldId": "PropertyType", "value": "ResidentialLot", "fieldConditionId": "", "valueConditionId": "IsResidential"},
                {"fieldId": "NearbyWater", "value": "Waterfront", "fieldConditionId": "IsResidential", "valueConditionId": ""},
                {"fieldId": "Resort", "value": "Resort", "fieldConditionId": "IsResort", "valueConditionId": ""},
                {"fieldId": "Category", "value": "Residential", "fieldConditionId": "", "valueConditionId": ""},
                {"fieldId": "SellingType", "value": "Sale", "fieldConditionId": "", "valueConditionId": ""},
                {"fieldId": "LivingArea", "value": "SquareFeet", "fieldConditionId": "IsResidentialNotLot", "valueConditionId": ""},
                {"fieldId": "LandArea", "value": "SquareFeet", "fieldConditionId": "IsLandArea", "valueConditionId": ""},
                {"fieldId": "SalePrice", "value": 0, "fieldConditionId": "ForSale", "valueConditionId": ""},
                {"fieldId": "SalePrice", "value": 999999999999, "fieldConditionId": "ForSale", "valueConditionId": ""},
            ],
            "BrokerCode": None,
            "OfficeKey": None,
        },
        "region": "Quebec",
        "openListing": None,
    }

    headers = {
        "content-type": "application/json; charset=UTF-8",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    return body.get("markers", [])  # TODO: confirmer la vraie clé/structure de la réponse


def load_state() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(seen_ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen_ids)))


def build_map_image(origin: tuple[float, float], listings: list[dict]) -> Path:
    m = StaticMap(800, 500)
    m.add_marker(CircleMarker((origin[1], origin[0]), "#2563eb", 14))  # point de départ
    for listing in listings:
        m.add_marker(CircleMarker((listing["lon"], listing["lat"]), "#dc2626", 12))
    image = m.render()
    image.save(str(MAP_FILE))
    return MAP_FILE


def build_html_report(new_listings: list[dict], map_path: Path, generated_at: str) -> Path:
    map_data_uri = "data:image/png;base64," + base64.b64encode(map_path.read_bytes()).decode()

    cards = ""
    for l in new_listings:
        address = l.get("address", "Voir l'annonce")
        price = l.get("price", "?")
        cards += f"""
        <a class="card" href="{l['url']}" target="_blank" rel="noopener">
          <div class="card-body">
            <div class="card-address">{address}</div>
            <div class="card-meta">
              <span class="price">{price} $</span>
              <span class="drive">🚗 {l['drive_minutes']:.0f} min</span>
            </div>
          </div>
        </a>"""

    html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Chalets bord de l'eau</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 24px 16px; background: #f5f5f4; color: #1c1917;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .subtitle {{ color: #57534e; margin: 0 0 20px; font-size: 0.9rem; }}
  .map {{ width: 100%; border-radius: 12px; border: 1px solid #d6d3d1; display: block; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }}
  .card {{
    display: block; background: white; border: 1px solid #e7e5e4; border-radius: 10px;
    padding: 14px 16px; text-decoration: none; color: inherit; transition: box-shadow .15s, transform .15s;
  }}
  .card:hover {{ box-shadow: 0 4px 14px rgba(0,0,0,.08); transform: translateY(-1px); }}
  .card-address {{ font-weight: 600; margin-bottom: 8px; }}
  .card-meta {{ display: flex; justify-content: space-between; font-size: 0.9rem; color: #44403c; }}
  .price {{ font-weight: 600; color: #15803d; }}
  footer {{ margin-top: 24px; font-size: 0.8rem; color: #78716c; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>🏡 {len(new_listings)} nouveau(x) chalet(s) bord de l'eau</h1>
    <p class="subtitle">{MAX_DRIVE_HOURS:.0f}h de route max de {ORIGIN_POSTAL_CODE} — généré le {generated_at}</p>
    <img class="map" src="{map_data_uri}" alt="Carte des chalets trouvés">
    <div class="grid">
      {cards}
    </div>
    <footer>chalet-monitor · rapport régénéré à chaque nouvelle trouvaille</footer>
  </div>
</body>
</html>"""

    REPORT_FILE.write_text(html, encoding="utf-8")
    return REPORT_FILE


def main() -> None:
    origin = geocode_postal_code(ORIGIN_POSTAL_CODE)
    seen_ids = load_state()

    raw_listings = search_centris_waterfront_cottages(origin, SEARCH_RADIUS_KM)

    candidates = []
    for listing in raw_listings:
        dest = (listing["lat"], listing["lon"])
        minutes = driving_time_minutes(origin, dest)
        time.sleep(1)  # ménager le serveur OSRM public
        if minutes <= MAX_DRIVE_HOURS * 60:
            listing["drive_minutes"] = minutes
            candidates.append(listing)

    new_listings = [l for l in candidates if l["id"] not in seen_ids]

    if new_listings:
        from datetime import datetime

        map_path = build_map_image(origin, new_listings)
        report_path = build_html_report(new_listings, map_path, f"{datetime.now():%Y-%m-%d %H:%M}")
        webbrowser.open(f"file://{report_path.resolve()}")
        print(f"{len(new_listings)} nouvelle(s) annonce(s) — rapport ouvert : {report_path}")
    else:
        print("Aucune nouvelle annonce trouvée.")

    seen_ids.update(l["id"] for l in candidates)
    save_state(seen_ids)


if __name__ == "__main__":
    main()
