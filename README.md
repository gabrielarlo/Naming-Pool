# Naming Pool

A small, anonymous naming contest. Guests join one room with a shared code, submit up to five names with a reason, and vote through moderator-controlled stages. FastAPI, Alpine.js, and SQLite run in one container.

## What guests see

1. **Submission** — each guest adds up to five names. They see their own names, the pool-wide total, and how many more are needed to start voting, never anyone else's names.
2. **Stage 1** — all names, no reasons. Each guest selects up to 10. The top 10 advance.
3. **Stage 2** — the top 10 with reasons. Each guest selects up to 3. The top 3 advance.
4. **Final** — the top 3 with reasons. Each guest selects 1. A unique leader wins.
5. **Tiebreak** — if the final ties, all finalists are re-voted. If the tiebreak also ties, the moderator reruns it with a clean ballot.

The moderator opens and closes each stage with a separate code. The stage-control action opens an in-app confirmation that names the action and its consequence; closing a stage publishes its results and cannot be undone. There are no accounts and no email addresses. The browser keeps an anonymous device token; the server stores only salted hashes of the device token and IP address.

## Requirements

- Docker with Compose v2, **or** Python 3.12+ for local development.
- Tailscale with Funnel enabled in the tailnet for public access.

## Docker quick start

Create a new `.env` with long random values. Run this from the project directory; it overwrites an existing `.env`.

```bash
{
  echo "ROOM_CODE=$(openssl rand -hex 24)"
  echo "MODERATOR_CODE=$(openssl rand -hex 24)"
  echo "IDENTITY_SALT=$(openssl rand -hex 32)"
  echo "MIN_SUGGESTIONS=10"
  echo "DB_PATH=/data/naming_pool.db"
  echo "COOKIE_SECURE=true"
} > .env
chmod 600 .env
```

Start the app:

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8001/api/health
```

Open <http://127.0.0.1:8001> on the host. The published port is bound to loopback only; Tailscale Funnel provides the public HTTPS entry point.

The SQLite database lives in the Compose volume `naming-pool-data` at `/data/naming_pool.db`. Docker prefixes the volume with the Compose project name (for example, `namingpool_naming-pool-data`). Stopping or restarting the container does not erase it. To start a new event, remove the volume as described under **Reset**.

## Configuration

`.env` values:

| Variable | Required | Purpose |
| --- | --- | --- |
| `ROOM_CODE` | yes | Code guests use to join. Surrounding whitespace is ignored. |
| `MODERATOR_CODE` | yes | Separate code that unlocks stage controls. |
| `IDENTITY_SALT` | yes | Secret used to hash device tokens and IP addresses. Changing it resets anonymous identities and invalidates existing guest sessions. |
| `MIN_SUGGESTIONS` | yes | Minimum accepted suggestions before stage 1 can start. Must be a positive integer. |
| `DB_PATH` | no | SQLite file path. Defaults to `/data/naming_pool.db`. |
| `COOKIE_SECURE` | no | Defaults to `true`. Set `false` only for local plain-HTTP development. |

The app refuses to start if any required setting is missing or empty, or if `MIN_SUGGESTIONS` is not a positive integer. Keep `.env` private; it is gitignored.

## API docs

Swagger UI, ReDoc, and the OpenAPI schema are disabled. Use the guest UI; API actions remain protected by the room/moderator/session flow.

## Local development

Use a separate local database and turn off the secure-cookie flag for plain HTTP.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
export ROOM_CODE=local-room-code
export MODERATOR_CODE=local-moderator-code
export IDENTITY_SALT="$(openssl rand -hex 32)"
export MIN_SUGGESTIONS=10
export DB_PATH="$PWD/naming_pool.db"
export COOKIE_SECURE=false
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8001
```

Use the project virtualenv (`source .venv/bin/activate`) so the pinned FastAPI/Starlette versions are used.

Run the API/unit tests:

```bash
pytest -q -m "not ui"
```

Run the browser UI tests (install Chromium once):

```bash
python -m playwright install chromium
pytest -q -m ui
```

Run everything:

```bash
pytest -q
```

The UI tests start their own Uvicorn server with a temporary SQLite database; they never touch the live database.

The local database files (`naming_pool.db`, `naming_pool.db-wal`, `naming_pool.db-shm`) are gitignored.

## Public access with Tailscale Funnel

Keep Docker running on port 8001, then publish it:

```bash
tailscale status
tailscale funnel --bg 8001
tailscale funnel status
```

`tailscale funnel status` prints the public `https://<machine>.<tailnet>.ts.net` URL. Open that URL from a device that is **not** on the tailnet and verify the join form appears.

Stop publishing the public Funnel listener:

```bash
tailscale funnel --https=443 off
```

This disables only the public HTTPS listener. It leaves any other `tailscale serve` configuration on the machine intact.

Funnel must be enabled for the tailnet, and the machine must have a valid HTTPS certificate from Tailscale. The container port stays loopback-only, so Funnel is the only public path.

## Backup

Stop the stack before copying SQLite so the WAL is consistent. Set the actual volume name, which Docker prefixes with the Compose project name:

```bash
docker compose stop
VOLUME=$(docker volume ls --format '{{.Name}}' | grep 'naming-pool-data' | head -n 1)
test -n "$VOLUME"
docker run --rm -v "$VOLUME":/data:ro -v "$PWD":/backup alpine tar czf /backup/naming-pool-backup.tgz -C /data .
docker compose start
```

The archive is written to the current directory as `naming-pool-backup.tgz`. Store it somewhere outside this machine.

## Restore

Stop the stack and restore into a fresh volume. This replaces the current event data.

```bash
docker compose down
VOLUME=$(docker volume ls --format '{{.Name}}' | grep 'naming-pool-data' | head -n 1)
test -n "$VOLUME"
docker volume rm "$VOLUME"
docker volume create "$VOLUME"
docker run --rm -v "$VOLUME":/data -v "$PWD":/backup alpine tar xzf /backup/naming-pool-backup.tgz -C /data
docker compose up -d
```

## Upgrade

```bash
docker compose build --pull
docker compose up -d
```

The schema is created idempotently at startup. This first version has no data migrations; always back up the volume before upgrading. Existing deployments must add `MIN_SUGGESTIONS` to `.env` before restarting; startup fails without it.

## Reset for a new event

This permanently deletes the event database:

```bash
docker compose down -v
docker compose up -d --build
```

The room and moderator codes in `.env` stay the same unless you change them. Every new event starts in the `submission` stage.

## Operational notes

- Run exactly one Uvicorn worker. The in-memory access-code rate limiter assumes one process.
- Stage 1 cannot start until the pool reaches `MIN_SUGGESTIONS`; the moderator panel shows the shortfall.
- Uvicorn access logs are disabled in the container. The app does not log request bodies, codes, IP addresses, device IDs, or hashes.
- The app sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`. There is intentionally no CSP because Alpine evaluates expressions; user text is rendered with `x-text`, never `x-html`.
- 3-second polling pauses while the tab is hidden and runs immediately after mutations.
- Voting stages show the number of guests who saved at least one vote and the number of guests active within the last 15 seconds. Presence uses the same poll, so a closed or hidden tab drops out after that window.
- Back up before an event. Closing a stage cannot be undone.
