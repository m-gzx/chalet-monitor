#!/usr/bin/env python3
"""
Moniteur de chalets à vendre bord de l'eau, à 2h de route ou moins de G3A2P8.

Flux :
  1. Géocode le point de départ (G3A2P8) avec Nominatim (OpenStreetMap).
  2. Récupère les chalets à vendre bord de l'eau via un flux RSS Centris
     (recherche sauvegardée) — voir CENTRIS_RSS_URL dans .env.
  3. Compare avec les annonces déjà vues (state.json) pour ne garder que
     les nouvelles, puis géocode leur adresse.
  4. Filtre les nouvelles annonces par temps de route réel (via OSRM), pas
     juste à vol d'oiseau.
  5. Génère une carte (image statique) + un courriel HTML avec liens
     cliquables, et l'envoie.

Nécessite : pip install -r requirements.txt
Configuration : copier .env.example en .env et remplir les valeurs.
"""

import json
import os
import re
import smtplib
import time
import xml.etree.ElementTree as ET
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

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
MAP_FILE = BASE_DIR / "map.png"

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
CENTRIS_RSS_URL = os.environ.get("CENTRIS_RSS_URL", "")

PRICE_RE = re.compile(r"(\d[\d\s ]{2,})\s*\$")


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


def parse_price(*texts: str) -> str | None:
    """Extrait un prix (ex. '249 900') du premier texte où il apparaît, pour affichage."""
    for text in texts:
        match = PRICE_RE.search(text or "")
        if match:
            return match.group(1).strip()
    return None


def fetch_centris_rss_listings() -> list[dict]:
    """
    Récupère les chalets à vendre bord de l'eau via un flux RSS Centris
    (recherche sauvegardée), configuré dans CENTRIS_RSS_URL (.env).

    Comment obtenir cette URL :
    1. Sur centris.ca, faire une recherche "chalet à vendre" + filtre
       "bord de l'eau" pour le secteur voulu, puis sauvegarder la recherche.
    2. Chercher l'option "Flux RSS" sur la page de résultats (souvent une
       icône RSS ou dans le menu de partage/options de la recherche
       sauvegardée) et copier son URL.
    3. Coller cette URL dans .env sous CENTRIS_RSS_URL.

    Chaque <item> du flux ne contient généralement pas de coordonnées
    précises : l'adresse est extraite du titre et géocodée séparément
    (voir geocode_address) avant le calcul du temps de route.

    Si Centris ne propose pas de flux RSS pour ce type de recherche,
    voir la note dans le README pour l'alternative DuProprio.
    """
    if not CENTRIS_RSS_URL:
        raise RuntimeError(
            "CENTRIS_RSS_URL n'est pas configuré dans .env. "
            "Voir le README (section 2) pour comment récupérer l'URL du flux RSS."
        )

    resp = requests.get(CENTRIS_RSS_URL, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    listings = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip() or link
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        if not link or not guid:
            continue
        listings.append(
            {
                "id": guid,
                "url": link,
                "address": title or "Voir l'annonce",
                "price": parse_price(title, description),
            }
        )
    return listings


def geocode_address(address: str) -> tuple[float, float] | None:
    """Géocode l'adresse d'une annonce via Nominatim. Retourne None si introuvable."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "country": "Canada", "format": "json"}
    headers = {"User-Agent": "chalet-monitor-personnel/1.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


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

    raw_listings = fetch_centris_rss_listings()
    unseen = [l for l in raw_listings if l["id"] not in seen_ids]

    new_listings = []
    for listing in unseen:
        coords = geocode_address(listing["address"])
        time.sleep(1)  # respecter la limite Nominatim (1 requête/seconde)
        if coords is None:
            continue
        minutes = driving_time_minutes(origin, coords)
        time.sleep(1)  # ménager le serveur OSRM public
        if minutes <= MAX_DRIVE_HOURS * 60:
            listing["lat"], listing["lon"] = coords
            listing["drive_minutes"] = minutes
            new_listings.append(listing)

    if new_listings:
        map_path = build_map_image(origin, new_listings)
        msg = build_email(new_listings, map_path)
        send_email(msg)
        print(f"{len(new_listings)} nouvelle(s) annonce(s) envoyée(s) par courriel.")
    else:
        print("Aucune nouvelle annonce trouvée.")

    seen_ids.update(l["id"] for l in raw_listings)
    save_state(seen_ids)


if __name__ == "__main__":
    main()
