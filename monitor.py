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
  5. Génère une carte (image statique) + un courriel HTML avec liens
     cliquables, et l'envoie.

Nécessite : pip install -r requirements.txt
Configuration : copier .env.example en .env et remplir les valeurs.
"""

import json
import os
import smtplib
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
from staticmap import CircleMarker, StaticMap

load_dotenv()

# --- CONFIGURATION ---
ORIGIN_POSTAL_CODE = "G3A2P8"
MAX_DRIVE_HOURS = 2.0
SEARCH_RADIUS_KM = 180  # rayon large à vol d'oiseau, filtré ensuite par temps de route réel

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
MAP_FILE = BASE_DIR / "map.png"

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))


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
    Interroge l'endpoint de recherche interne de Centris.

    IMPORTANT : Centris n'offre pas d'API publique documentée. Ce payload
    reproduit l'appel utilisé par leur carte de recherche (endpoint
    /property/GetInscriptions), reconstitué par inspection réseau. La
    structure exacte des filtres (noms de champs, valeurs pour
    "bord de l'eau", format des coordonnées) peut changer sans préavis.

    -> À faire une première fois avec Claude Code : ouvrir centris.ca,
    faire une recherche "chalet à vendre" + filtre "bord de l'eau" dans
    le secteur voulu, puis inspecter l'onglet Réseau du navigateur pour
    capturer la vraie requête et ajuster cette fonction en conséquence.
    Cette fonction est un point de départ, pas un produit fini.
    """
    url = "https://www.centris.ca/property/GetInscriptions"
    lat, lon = origin
    delta = radius_km / 111  # ~111 km par degré de latitude

    payload = {
        "startPosition": 0,
        "filters": {
            "category": "Cottage",
            "transactionType": "Sale",
            "characteristics": ["Waterfront"],
            "boundingBox": {
                "north": lat + delta,
                "south": lat - delta,
                "east": lon + delta,
                "west": lon - delta,
            },
        },
    }

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    return body.get("listings", [])


def load_state() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(seen_ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen_ids)))


def build_map_image(origin: tuple[float, float], listings: list[dict]) -> Path:
    m = StaticMap(800, 600)
    m.add_marker(CircleMarker((origin[1], origin[0]), "blue", 14))  # point de départ
    for listing in listings:
        m.add_marker(CircleMarker((listing["lon"], listing["lat"]), "red", 12))
    image = m.render()
    image.save(str(MAP_FILE))
    return MAP_FILE


def build_email(new_listings: list[dict], map_path: Path) -> MIMEMultipart:
    msg = MIMEMultipart("related")
    msg["Subject"] = (
        f"🏡 {len(new_listings)} nouveau(x) chalet(s) bord de l'eau "
        f"— {datetime.now():%Y-%m-%d}"
    )
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    rows = ""
    for l in new_listings:
        rows += (
            "<tr>"
            f"<td><a href='{l['url']}'>{l.get('address', 'Voir l’annonce')}</a></td>"
            f"<td>{l.get('price', '?')} $</td>"
            f"<td>{l['drive_minutes']:.0f} min</td>"
            "</tr>"
        )

    html = f"""
    <html><body style="font-family: sans-serif;">
    <h2>Nouveaux chalets bord de l'eau — {MAX_DRIVE_HOURS:.0f}h de route max de {ORIGIN_POSTAL_CODE}</h2>
    <img src="cid:map_image" width="800" style="border:1px solid #ccc;"><br><br>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Adresse (lien cliquable)</th><th>Prix</th><th>Temps de route</th></tr>
      {rows}
    </table>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))

    with open(map_path, "rb") as f:
        img = MIMEImage(f.read())
        img.add_header("Content-ID", "<map_image>")
        msg.attach(img)

    return msg


def send_email(msg: MIMEMultipart) -> None:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.send_message(msg)


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
        map_path = build_map_image(origin, new_listings)
        msg = build_email(new_listings, map_path)
        send_email(msg)
        print(f"{len(new_listings)} nouvelle(s) annonce(s) envoyée(s) par courriel.")
    else:
        print("Aucune nouvelle annonce trouvée.")

    seen_ids.update(l["id"] for l in candidates)
    save_state(seen_ids)


if __name__ == "__main__":
    main()
