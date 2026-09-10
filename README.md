# Moniteur de chalets bord de l'eau

Surveille automatiquement les nouvelles inscriptions de chalets à vendre,
bord de l'eau, à 2h de route ou moins de G3A2P8, et ouvre un rapport HTML
(carte + fiches cliquables) dans le navigateur dès qu'il y a du nouveau.

## 1. Installation (à faire une fois, dans Claude Code)

```bash
cd chalet-monitor
pip install -r requirements.txt
```

Aucun secret/compte courriel requis. Pour ajuster le point de départ, le
rayon de recherche ou le temps de route max, modifier directement les
constantes en haut de `monitor.py` (`ORIGIN_POSTAL_CODE`, `MAX_DRIVE_HOURS`,
`SEARCH_RADIUS_KM`).

## 2. Étape importante : ajuster la requête Centris

Centris n'a pas d'API publique documentée. `monitor.py` contient une
première tentative basée sur l'endpoint interne `/property/GetInscriptions`,
mais **il faut la valider avant de l'automatiser**. Voici comment capturer
la vraie requête avec les outils de développement du navigateur (F12) :

### Marche à suivre générale (Chrome, Edge ou Firefox)

1. Ouvrir le site (centris.ca ou duproprio.com) dans le navigateur.
2. Ouvrir les outils de développement : touche **F12**, ou clic droit
   n'importe où sur la page → **Inspecter** / **Examiner l'élément**.
3. Cliquer sur l'onglet **Réseau** (*Network*) dans le panneau qui s'ouvre.
4. Cocher **Conserver le journal** / **Preserve log** (sinon la liste des
   requêtes se vide à chaque nouvelle page).
5. Dans la barre de filtre du panneau Réseau, filtrer par **Fetch/XHR**
   pour ne garder que les appels d'API (ça retire les images, CSS, etc.
   et rend la liste beaucoup plus courte).
6. Faire la recherche sur le site pendant que le panneau est ouvert :
   "chalet à vendre" + filtre "bord de l'eau", secteur voulu.
7. Dans la liste qui apparaît, chercher une requête dont le nom contient
   un mot comme `search`, `inscriptions`, `properties`, `listings` ou
   `graphql` — c'est généralement celle qui retourne les résultats.
8. Cliquer sur cette requête, puis :
   - onglet **Charge utile** / **Payload** (ou **Request**) : montre ce
     qui a été envoyé (les filtres — région, prix, type de propriété).
   - onglet **Réponse** / **Response** : montre le JSON retourné avec les
     annonces (adresse, prix, coordonnées, id, URL).
9. Le plus simple pour tout capturer d'un coup : clic droit sur la requête
   → **Copier** → **Copier en tant que cURL** (*Copy as cURL*). Ça inclut
   l'URL exacte, les en-têtes et le corps de la requête.
10. Coller ce cURL (en retirant les cookies/tokens de session personnels
    si présents) dans une conversation Claude Code pour ajuster
    `search_centris_waterfront_cottages()` avec les bons noms de champs.

### Spécifique à Centris

- L'endpoint actuel dans le code (`/property/GetInscriptions`) est une
  hypothèse à confirmer — le vrai nom peut différer.
- Le filtre "bord de l'eau" correspond probablement à une valeur précise
  dans une liste de caractéristiques (`characteristics`) — à repérer dans
  le payload une fois une recherche filtrée faite sur le site.

### Spécifique à DuProprio

- DuProprio utilise souvent une API de type GraphQL (une seule requête
  **POST**, avec un champ `query` contenant la requête GraphQL et un champ
  `variables` avec les filtres) — repérable dans le panneau Réseau par une
  requête vers un chemin contenant `graphql`.
- Il faudrait une fonction distincte, par exemple
  `search_duproprio_waterfront_cottages()`, suivant le même principe que
  celle de Centris.

### uBee (ubee.com/carte/a-vendre)

- Même méthode : ouvrir `https://ubee.com/carte/a-vendre`, F12 → Réseau →
  filtrer Fetch/XHR, faire une recherche par secteur, puis repérer la
  requête qui charge les fiches affichées sur la carte (souvent déclenchée
  quand on déplace/zoome la carte, donc utile de laisser le filtre Réseau
  ouvert *avant* de bouger la carte).
- Une troisième fonction, par exemple `search_ubee_waterfront_cottages()`,
  suivant le même principe.

**Alternative plus simple si les API s'avèrent trop instables** :
utiliser un flux RSS de recherche sauvegardée (Centris et DuProprio en
offrent parfois) et adapter `monitor.py` pour parser du RSS au lieu de
l'API — je peux réécrire cette partie si tu préfères cette avenue.

## 3. Tester manuellement

```bash
python monitor.py
```

La première exécution va probablement ouvrir un rapport avec TOUTES les
annonces existantes (rien n'est encore dans `state.json`). C'est normal —
les exécutions suivantes ne signaleront que les nouveautés.

## 4. Automatiser avec cron (Mac/Linux)

```bash
crontab -e
```

Ajouter une ligne pour vérifier tous les jours à 8h :

```
0 8 * * * cd /chemin/vers/chalet-monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

Sur Windows : utiliser le Planificateur de tâches avec une action qui
lance `python monitor.py` dans le dossier du projet.

Note : si le cron tourne pendant que l'ordinateur est verrouillé ou
éteint, `webbrowser.open()` peut échouer silencieusement à ouvrir un
onglet — le rapport reste quand même disponible dans `report.html` et
peut être ouvert manuellement au retour.

## 5. Ce que fait le script à chaque exécution

1. Géocode G3A2P8.
2. Cherche les chalets bord de l'eau dans un rayon large autour de ce point.
3. Filtre par **temps de route réel** (≤ 2h) via OSRM, pas juste à vol d'oiseau.
4. Compare avec `state.json` (annonces déjà vues).
5. S'il y a du nouveau : génère une image de carte (`map.png`), construit
   un rapport HTML autonome (`report.html`, carte + fiches cliquables avec
   adresse, prix et temps de route) et l'ouvre dans le navigateur par défaut.
6. Met à jour `state.json`.

## Notes

- Le service de temps de route utilisé (OSRM, serveur public de démo)
  est gratuit mais partagé — éviter les vérifications trop fréquentes
  (1x/jour est raisonnable).
- Pour ajouter DuProprio ou uBee en plus de Centris, il faudrait une
  fonction de recherche additionnelle par site, similaire à
  `search_centris_waterfront_cottages()` (voir section 2 ci-dessus).
- `report.html` et `map.png` sont régénérés à chaque exécution avec du
  nouveau — ils ne sont pas versionnés dans Git (voir `.gitignore`).
