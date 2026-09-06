# Moniteur de chalets bord de l'eau

Surveille automatiquement les nouvelles inscriptions de chalets à vendre,
bord de l'eau, à 2h de route ou moins de G3A2P8, et envoie un courriel
avec une carte et des liens cliquables dès qu'il y a du nouveau.

## 1. Installation (à faire une fois, dans Claude Code)

```bash
cd chalet-monitor
pip install -r requirements.txt
cp .env.example .env
```

Puis ouvrir `.env` et remplir :
- `EMAIL_FROM` / `EMAIL_TO` : ton adresse Gmail
- `EMAIL_APP_PASSWORD` : un **mot de passe d'application** Gmail
  (pas ton mot de passe habituel). À générer ici :
  https://myaccount.google.com/apppasswords
  (nécessite la validation en 2 étapes activée sur le compte)

## 2. Étape importante : obtenir le flux RSS Centris

`monitor.py` récupère les annonces via un **flux RSS d'une recherche
sauvegardée** sur Centris (plus stable qu'une API interne non documentée,
qui peut changer sans préavis) :

1. Sur centris.ca, faire une recherche "chalet à vendre" + filtre
   "bord de l'eau" pour le secteur voulu (autour de G3A2P8, rayon large —
   le filtre par temps de route réel se fait ensuite dans le script).
2. Sauvegarder la recherche, puis repérer l'option "Flux RSS" sur la page
   de résultats (souvent une icône RSS ou dans le menu de partage/options
   de la recherche sauvegardée) et copier son URL.
3. Coller cette URL dans `.env` sous `CENTRIS_RSS_URL`.

Si Centris ne propose pas de flux RSS pour ce type de recherche : DuProprio
en offre parfois pour ses recherches sauvegardées — une deuxième fonction
similaire à `fetch_centris_rss_listings()` pourrait être ajoutée pour ce
site.

## 3. Tester manuellement

```bash
python monitor.py
```

La première exécution va probablement envoyer un courriel avec TOUTES
les annonces existantes (rien n'est encore dans `state.json`). C'est
normal — les exécutions suivantes ne signaleront que les nouveautés.

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

## 5. Ce que fait le script à chaque exécution

1. Géocode G3A2P8.
2. Lit le flux RSS Centris (`CENTRIS_RSS_URL`) pour la liste des chalets
   bord de l'eau de la recherche sauvegardée.
3. Compare avec `state.json` (annonces déjà vues) — ne garde que les
   nouvelles.
4. Géocode l'adresse de chaque nouvelle annonce, puis filtre par
   **temps de route réel** (≤ 2h) via OSRM, pas juste à vol d'oiseau.
5. S'il y a du nouveau après ce filtre : génère une image de carte
   (`map.png`) et envoie un courriel HTML avec la carte + un tableau de
   liens cliquables, prix, et temps de route.
6. Met à jour `state.json` avec **toutes** les annonces vues dans le flux
   (même celles à plus de 2h), pour ne pas les regéocoder inutilement
   aux prochaines exécutions.

## Notes

- Le service de temps de route utilisé (OSRM, serveur public de démo)
  est gratuit mais partagé — éviter les vérifications trop fréquentes
  (1x/jour est raisonnable).
- Nominatim (géocodage) impose aussi une limite d'usage : le script
  respecte 1 requête/seconde, mais éviter quand même les exécutions trop
  fréquentes.
- Pour ajouter DuProprio en plus de Centris, il faudrait une deuxième
  fonction de recherche similaire à `fetch_centris_rss_listings()`.
