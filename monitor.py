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

Nécessite : pip install -r requirements.txt puis, une seule fois,
"playwright install chromium" (utilisé pour obtenir une session Centris
valide face à Cloudflare — voir search_centris_waterfront_cottages()).
Aucune configuration/secret requis — ajuster les constantes ci-dessous
au besoin (code postal, rayon, temps de route max).
"""

import base64
import json
import math
import os
import time
import webbrowser
from pathlib import Path

import requests
from bs4 import BeautifulSoup
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
    """
    Convertit un code postal canadien en (lat, lon) via Nominatim.

    Nominatim n'indexe presque jamais les codes postaux canadiens complets
    à 6 caractères comme entités propres (Canada Post ne publie pas de
    limites précises par LDU) : la recherche structurée `postalcode=`
    échoue donc systématiquement pour ce genre de code (confirmé en
    production le 2026-09-10 — `ValueError: Impossible de géocoder
    G3A2P8`). On utilise plutôt la recherche libre (`q=`), qui résout
    généralement via les données d'adresses OSM ; si le code complet ne
    donne rien, on retente avec seulement le secteur de tri (FSA, les 3
    premiers caractères), presque toujours indexé.
    """
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "chalet-monitor-personnel/1.0"}

    for query in (f"{postal_code}, Canada", f"{postal_code[:3]}, Canada"):
        resp = requests.get(url, params={"q": query, "format": "json"}, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
        time.sleep(1)  # respecter la politique d'usage de Nominatim entre deux essais

    raise ValueError(f"Impossible de géocoder {postal_code}")


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance à vol d'oiseau (km) entre deux points (lat, lon)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def driving_time_minutes(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    """
    Temps de route via OSRM (serveur public de démonstration).
    Usage personnel léger uniquement — ne pas appeler en boucle serrée.

    En cas d'échec (timeout, erreur HTTP, réponse "code" != "Ok"), affiche
    la raison sur stderr plutôt que de l'avaler silencieusement — un run du
    2026-09-10 sur GitHub Actions a trouvé zéro candidat sur ~2000 annonces
    sans que rien dans les logs n'explique pourquoi, ce qui a rendu le
    diagnostic impossible après coup.
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
            print(f"[OSRM] réponse non-Ok pour {dest} : {data.get('code')} — {data.get('message', '')}")
            return float("inf")
        return data["routes"][0]["duration"] / 60
    except requests.RequestException as exc:
        print(f"[OSRM] échec de requête pour {dest} : {exc}")
        return float("inf")


def parse_centris_listing_cards(html: str) -> list[dict]:
    """
    Parse le HTML pré-rendu retourné par GetInscriptions (vue "Thumbnail")
    en une liste de fiches structurées.

    Confirmé le 2026-09-10 sur un échantillon réel : chaque annonce est un
    bloc `.property-thumbnail-item` contenant, entre autres :
      - id/MLS : <meta itemprop="sku" content="...">
      - url : href du <a class="property-thumbnail-summary-link">
      - prix (nombre brut, sans formatage) : <meta itemprop="price" content="...">
      - adresse : les <div> à l'intérieur de <div class="address">
        (rue puis ville, la rue est parfois absente pour un terrain)
      - lat/lon : attributs data-lat/data-lng du <span class="ll-match-score">
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select(".property-thumbnail-item"):
        link = card.select_one("a.property-thumbnail-summary-link")
        sku_meta = card.select_one('meta[itemprop="sku"]')
        price_meta = card.select_one('meta[itemprop="price"]')
        score_span = card.select_one(".ll-match-score")
        if link is None or sku_meta is None or score_span is None:
            continue

        address_div = card.select_one(".address")
        address_lines = [d.get_text(strip=True) for d in address_div.find_all("div")] if address_div else []

        listings.append({
            "id": sku_meta["content"],
            "url": "https://www.centris.ca" + link["href"],
            "price": int(price_meta["content"]) if price_meta and price_meta.get("content") else None,
            "address": ", ".join(address_lines) or "Voir l'annonce",
            "lat": float(score_span["data-lat"]),
            "lon": float(score_span["data-lng"]),
        })
    return listings


def search_centris_waterfront_cottages(origin: tuple[float, float], radius_km: float) -> list[dict]:
    """
    Interroge l'endpoint interne de recherche Centris (GetInscriptions),
    capturé et validé par inspection réseau (F12) le 2026-09-10 sur une
    recherche Chalet + Terrain + Bord de l'eau + Villégiature.

    Centris est protégé par Cloudflare : l'appel exige des cookies de
    session (dont `cf_clearance`) obtenus en résolvant un défi JavaScript.
    On utilise donc Playwright pour ouvrir une vraie page Centris une
    première fois (ce qui établit ces cookies dans le contexte du
    navigateur), puis on référence les requêtes POST à travers ce même
    contexte — elles envoient automatiquement les bons cookies.

    Historique :
    - Un premier endpoint tenté (GetMarkers, /api/property/map/GetMarkers)
      ne retourne que des clusters de positions pour dessiner la carte, pas
      des annonces individuelles.
    - GetInscriptions (/Property/GetInscriptions), lui, retourne (dans
      `d.Result.html`) le HTML pré-rendu des fiches de la vue "Galerie",
      paginées par 20 (`pageSize`/`page`) plutôt que par zone géographique
      — la requête capturée n'a pas de rayon/bounding box, juste
      `"region": "Quebec"` (aucune ville précisée dans la barre de
      recherche du site lors de la capture). `d.Result.count` donne le
      nombre total d'annonces correspondant aux filtres. On parcourt donc
      toutes les pages pour "Quebec" et c'est le filtre de temps de route
      (voir main()) qui réduit ensuite aux propriétés à MAX_DRIVE_HOURS ou
      moins de ORIGIN_POSTAL_CODE — radius_km n'est donc pas utilisé ici
      pour l'instant (paramètre conservé pour usage futur si une
      restriction géographique côté Centris est ajoutée).
    """
    from playwright.sync_api import sync_playwright

    url = "https://www.centris.ca/Property/GetInscriptions"
    query = {
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
    }

    headers = {
        "content-type": "application/json; charset=UTF-8",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.centris.ca/fr/propriete~a-vendre",
    }

    page_size = 20
    max_pages = 60  # garde-fou (60 * 20 = 1200 annonces max)
    listings: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            )
            # Établit la session (cookies Cloudflare inclus) avant les appels.
            browser_page = context.new_page()
            browser_page.goto("https://www.centris.ca/fr", wait_until="networkidle", timeout=30000)

            total_count = None
            for page_number in range(1, max_pages + 1):
                payload = {
                    "mode": "Result",
                    "searchView": "Thumbnail",
                    "sortSeed": 1,
                    "sort": "None",
                    "pageSize": page_size,
                    "page": page_number,
                    "query": query,
                    "region": "Quebec",
                }
                resp = context.request.post(url, data=json.dumps(payload), headers=headers, timeout=15000)
                if not resp.ok:
                    raise RuntimeError(
                        f"Centris a refusé la requête GetInscriptions ({resp.status}) — "
                        "cookies de session ou défi Cloudflare probablement invalides."
                    )
                result = resp.json()["d"]["Result"]
                total_count = result["count"]
                page_listings = parse_centris_listing_cards(result["html"])
                if not page_listings:
                    break
                listings.extend(page_listings)
                if len(listings) >= total_count:
                    break
                time.sleep(1)  # ménager Centris entre les pages
        finally:
            browser.close()

    return listings


def parse_ubee_listings(results: list[dict]) -> list[dict]:
    """Transforme les entrées brutes de SearchProperties en fiches structurées."""
    listings = []
    for r in results:
        listings.append({
            "id": r["id"],
            "url": f"https://ubee.com/a-vendre/{r['citySlug']}/{r['slugFr']}",
            "price": r.get("askPrice"),
            "address": f"{r['address']}, {r['city']}",
            "lat": r["latitude"],
            "lon": r["longitude"],
        })
    return listings


def search_ubee_waterfront_cottages(origin: tuple[float, float], radius_km: float) -> list[dict]:
    """
    Interroge l'endpoint interne de recherche uBee (SearchProperties),
    capturé et validé par inspection réseau (F12) le 2026-09-10 avec le
    filtre "bord de l'eau" actif sur ubee.com/carte/a-vendre.

    Contrairement à Centris, uBee n'a aucune protection Cloudflare/cookie —
    un simple requests.post() suffit, pas besoin de Playwright.

    uBee n'a pas de catégorie "Chalet" distincte dans son interface : les
    chalets y sont classés sous "Unifamiliale" (résidence uni-familiale) ou
    "Terrain", d'où inscriptionTypes = ["Terrain", "Unifamiliale"] ci-dessous.

    Pagination par ?pageIndex=N (0-indexé) en paramètre d'URL ; le corps de
    la requête reste identique à chaque page (mapBoundaries fixe = bounding
    box couvrant tout le Québec habité, comme radius_km n'est pas utilisé
    par cet endpoint — le filtre de temps de route réel, voir main(),
    réduit ensuite aux annonces à MAX_DRIVE_HOURS ou moins).

    URL de fiche : https://ubee.com/a-vendre/{citySlug}/{slugFr}, confirmée
    le 2026-09-10 sur un exemple réel (ex. .../a-vendre/chertsey/terrain-...).
    """
    url = "https://api.ubee.ca/api/anonymous/Search/SearchProperties"
    headers = {
        "accept": "text/json",
        "content-type": "application/*+json",
        "origin": "https://ubee.com",
        "referer": "https://ubee.com/",
    }
    map_boundaries = {
        "type": "Polygon",
        "coordinates": [[
            [-79.02226422505078, 41.32405887412813],
            [-62.97773577494998, 41.32405887412813],
            [-62.97773577494998, 52.397461900458296],
            [-79.02226422505078, 52.397461900458296],
            [-79.02226422505078, 41.32405887412813],
        ]],
    }
    payload = {
        "minBathrooms": 0,
        "minBedrooms": 0,
        "toBuild": False,
        "onlineSinceInDays": 0,
        "sortBy": "DateDescending",
        "mapBoundaries": json.dumps(map_boundaries),
        "listingType": "Seller",
        "complimentaryFilters": {
            "hasCitySewerSystem": False,
            "hasCityWaterSupply": False,
            "hasSwimmingPool": False,
            "hasWaterAccess": True,
            "isAccessibleReducedMobility": False,
        },
        "isResidential": True,
        "minLandSurfaceInMeters": None,
        "maxLandSurfaceInMeters": None,
        "minLivingSurfaceInMeters": None,
        "maxLivingSurfaceInMeters": None,
        "hasGarage": False,
        "inscriptionTypes": ["Terrain", "Unifamiliale"],
    }

    max_pages = 100  # garde-fou
    listings: list[dict] = []
    page_index = 0
    while page_index < max_pages:
        resp = requests.post(url, params={"pageIndex": page_index}, json=payload, headers=headers, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"uBee a refusé la requête SearchProperties ({resp.status_code})")
        data = resp.json()
        page_results = data.get("results", [])
        if not page_results:
            break
        listings.extend(parse_ubee_listings(page_results))
        total_count = data.get("totalCount", 0)
        if len(listings) >= total_count:
            break
        page_index += 1
        time.sleep(1)  # ménager uBee entre les pages

    return listings


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
        price = l.get("price")
        price_display = f"{price:,}".replace(",", " ") + " $" if price else "Prix non précisé"
        cards += f"""
        <a class="card" href="{l['url']}" target="_blank" rel="noopener">
          <div class="card-body">
            <div class="card-address">{address}</div>
            <div class="card-meta">
              <span class="price">{price_display}</span>
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
    raw_listings += search_ubee_waterfront_cottages(origin, SEARCH_RADIUS_KM)
    print(f"{len(raw_listings)} annonce(s) brute(s) trouvée(s) (Centris + uBee).")

    # Pré-filtre à vol d'oiseau avant d'appeler OSRM : la route est presque
    # toujours plus longue que la ligne droite, donc ce filtre ne peut pas
    # exclure de vrai candidat, mais il évite des centaines d'appels OSRM
    # inutiles (annonces à Gatineau, Saguenay, etc., évidemment hors zone) —
    # important vu qu'OSRM est un serveur public de démo, sensible au
    # rate-limiting sur de gros volumes séquentiels.
    nearby = [l for l in raw_listings if haversine_km(origin, (l["lat"], l["lon"])) <= SEARCH_RADIUS_KM]
    print(f"{len(nearby)} annonce(s) dans le rayon de {SEARCH_RADIUS_KM} km à vol d'oiseau.")

    candidates = []
    for listing in nearby:
        dest = (listing["lat"], listing["lon"])
        minutes = driving_time_minutes(origin, dest)
        time.sleep(1)  # ménager le serveur OSRM public
        if minutes <= MAX_DRIVE_HOURS * 60:
            listing["drive_minutes"] = minutes
            candidates.append(listing)
    print(f"{len(candidates)} annonce(s) à {MAX_DRIVE_HOURS:.0f}h de route ou moins.")

    new_listings = [l for l in candidates if l["id"] not in seen_ids]

    if new_listings:
        from datetime import datetime

        map_path = build_map_image(origin, new_listings)
        report_path = build_html_report(new_listings, map_path, f"{datetime.now():%Y-%m-%d %H:%M}")
        if not os.environ.get("CI"):
            # Pas de navigateur à ouvrir sur un runner CI (ex. GitHub Actions) —
            # le rapport y est plutôt publié via GitHub Pages (voir workflow).
            webbrowser.open(f"file://{report_path.resolve()}")
        print(f"{len(new_listings)} nouvelle(s) annonce(s) — rapport généré : {report_path}")
    else:
        print("Aucune nouvelle annonce trouvée.")

    seen_ids.update(l["id"] for l in candidates)
    save_state(seen_ids)


if __name__ == "__main__":
    main()
