# Private SearXNG on a VPS

A self-hosted SearXNG removes the per-request cost for internal tools (scheduled
searches, personal agents). This guide targets a small Linux VPS (for example Hetzner
Cloud) with Docker.

## 1. Requirements

* Docker Engine with the Compose plugin (`docker compose version`).
* ~512 MB RAM free. No public port is needed.

## 2. Start the instance

```bash
cd deploy/searxng
cp .env.example .env
sed -i "s/change-me/$(openssl rand -hex 32)/" .env
chmod 600 .env
docker compose up -d
docker compose logs -f searxng      # Ctrl+C once it reports it is listening
```

The compose file:

* publishes the container's port 8080 on **127.0.0.1:8888 only**;
* mounts `./searxng` as `/etc/searxng` (SearXNG may write generated files there);
* takes the secret from `SEARXNG_SECRET`.

`searxng/settings.yml` enables the JSON format (otherwise the API returns 403), turns
off the bot limiter (it would block scripts; safe because the port is private) and
lowers upstream timeouts.

## 3. Check it

```bash
curl -s "http://127.0.0.1:8888/search?q=searxng&format=json" | python3 -m json.tool | head -30
```

Expect a `results` array. A 403 means JSON is not enabled in the settings file that the
container actually loaded.

## 4. Point SeekRelay at it

On the same server:

```bash
export SEARXNG_BASE_URL=http://127.0.0.1:8888
seekrelay --providers searxng search "python asyncio taskgroup" -n 3
```

From your own computer, through an SSH tunnel (nothing exposed publicly):

```bash
ssh -N -L 8888:127.0.0.1:8888 user@your-server
# in another terminal
export SEARXNG_BASE_URL=http://127.0.0.1:8888
```

## 5. Choose engines

Some engines (notably Google) block metasearch traffic quickly. When that happens the
provider raises `ProviderUnavailableError` ("upstream engines unresponsive") and the
gateway falls back. To reduce it, pin engines that tolerate automated use:

```bash
export SEARXNG_ENGINES=duckduckgo,brave,wikipedia,github
```

Enabled engines and their names are listed on the instance's preferences page
(`http://127.0.0.1:8888/preferences`, through the tunnel).

## 6. Exposing it (only if you must)

If other machines need direct access, do not open port 8888. Put a reverse proxy
(Caddy, nginx) in front with HTTPS and authentication, enable `server.limiter: true`
(it needs a Valkey/Redis service; see the SearXNG docs), and pass the credentials via
`SEARXNG_AUTH_HEADER`.

## 7. Maintenance

```bash
docker compose pull && docker compose up -d     # update
docker compose logs --tail=100 searxng           # inspect upstream errors
```

Upstream engines change often; updating every few weeks keeps them working.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `AuthenticationError ... search.formats` | JSON not enabled, or the limiter blocked the client |
| Empty results + "unresponsive engines" | Upstream CAPTCHA/timeouts: change `SEARXNG_ENGINES`, wait, or update the image |
| `ProviderUnavailableError ... ConnectError` | Container down or tunnel closed: `docker compose ps`, re-open the tunnel |
| `unexpected redirect` | Wrong `SEARXNG_BASE_URL` (path or scheme), or a proxy redirecting to login |
| Container restarts on start-up | Missing `SEARXNG_SECRET`; check `docker compose logs` |
