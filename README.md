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
- **Assistant de configuration en 2 étapes dans l'UI Music Assistant** : saisissez l'URL Lidarr et la clé API, puis sélectionnez vos profils chargés dynamiquement depuis Lidarr (*Root Folder*, *Quality Profile*, *Metadata Profile*, *Artist Monitoring Mode* et *Tag* optionnel).

## Déploiement avec Docker + Home Assistant (Option 3)

Cette méthode fait tourner **Music Assistant Server** dans un conteneur Docker (sur votre serveur / LXC Proxmox) avec le provider `lidarr` monté directement en volume, puis l'associe à **Home Assistant** via l'intégration officielle **Music Assistant**.

### 1. Préparer le serveur Docker

Copiez ce dépôt sur votre hôte Docker (via `git clone` ou `rsync`), puis créez votre fichier `.env` à partir de `.env.example` :

```bash
cp .env.example .env
# Ajustez éventuellement MA_IMAGE_TAG, MA_DATA_DIR et MUSIC_DIR dans .env
```

Si vous souhaitez que Music Assistant lise directement le dossier musical géré par Lidarr, décommentez la ligne `MUSIC_DIR` dans `docker-compose.yml` et `.env`.

### 2. Lancer Music Assistant Server

```bash
docker compose up -d
docker compose logs -f music-assistant-server
```

Vérifiez dans les logs que le serveur démarre sur le port `8095` et charge le provider `lidarr` depuis `/custom_providers`.

### 3. Configurer le provider Lidarr dans Music Assistant

1. Ouvrez l'interface web Music Assistant : `http://<IP_DU_SERVEUR>:8095`.
2. Allez dans **Settings → Music Sources → Add Music Source → Lidarr**.
3. **Étape 1 (Identifiants)** : renseignez l'**URL Lidarr** (ex. `http://192.168.1.50:8686`) et la **clé API** (*Lidarr → Settings → General → Security*).
4. **Étape 2 (Profils)** : sélectionnez votre *Root Folder*, *Quality Profile*, *Metadata Profile*, mode de monitoring artiste et tag Lidarr optionnel, puis validez.

### 4. Connecter Home Assistant

1. Si l'ancien add-on Music Assistant tourne encore sur Home Assistant, arrêtez-le (ou supprimez-le après avoir vérifié que votre nouvelle instance fonctionne).
2. Dans Home Assistant : **Paramètres → Appareils et services → Ajouter une intégration → Music Assistant**.
3. Indiquez l'URL de votre conteneur : `http://<IP_DU_SERVEUR>:8095`.
4. Home Assistant retrouvera automatiquement vos lecteurs (`media_player.*`) et affichera l'interface Music Assistant dans la barre latérale.

### 5. Mettre à jour le plugin

```bash
git pull
docker compose restart music-assistant-server
```

## Développement & Tests locaux

```bash
# Installer l'environnement (Python 3.14 + music-assistant)
uv sync

# Démarrer une instance locale de test sur http://localhost:8095
./scripts/run.sh

# Lancer les tests unitaires et d'intégration
uv run pytest

# Vérifier le lint, le formatage, le typage et l'orthographe
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run codespell
```
