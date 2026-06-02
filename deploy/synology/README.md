# Synology Deployment

This deployment uses the image built by GitHub Actions:

```text
ghcr.io/zipenok/ytb-dl:latest
```

The NAS no longer needs local source overrides or local image builds. Pull the compose file from the GitHub repository, adjust the secrets, and start the project.

## Requirements

- Synology DSM 7 with Container Manager.
- x86_64/amd64 NAS. The Dockerfile currently builds `linux/amd64`.
- App data directory: `/volume1/docker/ytb-dl`.
- Download directory: `/volume1/Nas/downloads/youtube`.

## Prepare Directories

```bash
mkdir -p /volume1/docker/ytb-dl/config
mkdir -p /volume1/Nas/downloads/youtube
```

## Deploy From GitHub

```bash
mkdir -p /volume1/docker/ytb-dl
cd /volume1/docker/ytb-dl
wget -O docker-compose.yml https://raw.githubusercontent.com/ZiPenOk/ytb-dl/main/deploy/synology/docker-compose.yml
```

Edit these values before starting:

```yaml
WEB_AUTH_PASSWORD: change-this-password
API_TOKEN: change-this-api-token
AUTH_SECRET: change-this-session-secret
```

Start:

```bash
docker compose pull
docker compose up -d
docker compose logs -f ytb-dl
```

Open:

```text
http://<NAS-IP>:9832
```

## Updating

GitHub Actions builds and publishes a new image after pushes to `main`.

Update the NAS:

```bash
cd /volume1/docker/ytb-dl
wget -O docker-compose.yml https://raw.githubusercontent.com/ZiPenOk/ytb-dl/main/deploy/synology/docker-compose.yml
docker compose pull
docker compose up -d
```

## Persistent Data

- App config: `/volume1/docker/ytb-dl/config/config.json`
- Auth config: `/volume1/docker/ytb-dl/config/auth.json`
- YouTube cookies: `/volume1/docker/ytb-dl/config/cookies.txt`
- Downloads: `/volume1/Nas/downloads/youtube`

## Notes

- If port `9832` is already used, change the left side of the port mapping, for example `"19832:9832"`.
- Do not enable `/dev/dri` unless your NAS exposes that device.
- For a private GHCR package, log in on the NAS before `docker compose pull`. Public packages do not need login.
