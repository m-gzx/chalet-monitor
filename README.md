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

## 2. Étape importante : ajuster la requête Centris

Centris n'a pas d'API publique documentée. `monitor.py` contient une
première tentative basée sur l'endpoint interne `/property/GetInscriptions`,
mais **il faut la valider avant de l'automatiser** :

1. Demander à Claude Code d'ouvrir centris.ca dans un navigateur
   (ou le faire manuellement), faire une recherche "chalet à vendre" +
   filtre "bord de l'eau" pour le secteur voulu.
2. Ouvrir l'onglet Réseau (F12) du navigateur pendant la recherche.
3. Repérer la requête envoyée (souvent en JSON, vers un endpoint
   `/property/...`), et copier son format exact.
4. Ajuster la fonction `search_centris_waterfront_cottages()` dans
   `monitor.py` avec les bons noms de champs.

C'est le genre de tâche où Claude Code peut t'aider directement en
inspectant les requêtes et en corrigeant le script.

**Alternative plus simple si l'API Centris s'avère trop instable** :
utiliser un flux RSS de recherche sauvegardée (Centris et DuProprio en
offrent parfois) et adapter `monitor.py` pour parser du RSS au lieu de
l'API — je peux réécrire cette partie si tu préfères cette avenue.

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
2. Cherche les chalets bord de l'eau dans un rayon large autour de ce point.
3. Filtre par **temps de route réel** (≤ 2h) via OSRM, pas juste à vol d'oiseau.
4. Compare avec `state.json` (annonces déjà vues).
5. S'il y a du nouveau : génère une image de carte (`map.png`) et envoie
   un courriel HTML avec la carte + un tableau de liens cliquables,
   prix, et temps de route.
6. Met à jour `state.json`.

## Notes

- Le service de temps de route utilisé (OSRM, serveur public de démo)
  est gratuit mais partagé — éviter les vérifications trop fréquentes
  (1x/jour est raisonnable).
- Pour ajouter DuProprio en plus de Centris, il faudrait une deuxième
  fonction de recherche similaire à `search_centris_waterfront_cottages()`.
