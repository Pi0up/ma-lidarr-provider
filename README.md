# Music Assistant — Lidarr Provider

Provider communautaire **Music Assistant** permettant de rechercher des **artistes** et **albums** directement via votre serveur **Lidarr** et de déclencher automatiquement leur téléchargement depuis l'interface de Music Assistant.

## Fonctionnalités

- **Recherche intégrée (Artistes & Albums)** : interroge directement l'API Lidarr (`/api/v1/search`) depuis la barre de recherche de Music Assistant, avec un tag de version (`Lidarr Request`) afin de laisser Music Assistant fusionner les résultats avec votre bibliothèque locale.
- **Demande de téléchargement sur « Ajouter à la bibliothèque »** :
  - **Album** : ajoute l'artiste si absent (autres albums non monitorés), monitore uniquement l'album demandé et déclenche immédiatement `AlbumSearch`. Si l'album existe déjà mais n'est pas monitoré, active le monitoring et relance la recherche.
  - **Artiste** : ajoute l'artiste selon le mode de monitoring choisi (`future`, `all`, `missing`, `none`…) et déclenche `ArtistSearch`.
  - **Protection contre la lecture involontaire** : cliquer sur *Lecture (Play)* sur un résultat Lidarr renvoie un message clair sans lancer de téléchargement.
- **Suivi dans « Parcourir » (`Browse`)** :
  - *Downloading (Queue)* : affiche les albums en cours de téléchargement dans Lidarr avec leur pourcentage d'avancement.
  - *Missing / Requested Albums* : affiche les albums monitorés manquants.
- **Rescan automatique post-import** : surveille l'historique d'import Lidarr en arrière-plan et déclenche automatiquement une synchronisation de votre provider de bibliothèque locale dans Music Assistant dès qu'un album est importé.
- **Configuration dynamique dans l'UI Music Assistant** : saisissez l'URL Lidarr et la clé API, puis cliquez sur **Load profiles from Lidarr** pour charger automatiquement les listes déroulantes (Root Folder, Quality Profile, Metadata Profile, Tags).

## Installation sur Home Assistant (Add-on Music Assistant)

Ce dépôt suit le format officiel `music-assistant/community-provider-template`.

1. Publiez ce dépôt sur GitHub (en créant une release, ex. `v0.1.0`) ou testez-le localement.
2. Dans **Music Assistant** (Home Assistant) :
   - Activez les providers communautaires dans **Settings → Marketplace / Community Providers** (ou installez le dossier `lidarr/` dans votre répertoire de providers personnalisés si vous utilisez Docker/dev).
   - Allez dans **Settings → Music Sources → Add Music Source → Lidarr**.
   - Renseignez l'**URL Lidarr** (ex. `http://192.168.1.50:8686`) et la **clé API** (*Lidarr → Settings → General → Security*).
   - Cliquez sur **Load profiles from Lidarr**, sélectionnez votre dossier racine et vos profils, puis enregistrez.

## Développement & Tests locaux

```bash
# Installer l'environnement (Python 3.14 + music-assistant)
uv sync

# Lancer les tests unitaires et d'intégration
uv run pytest

# Vérifier le lint, le formatage, le typage et l'orthographe
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run codespell
```
