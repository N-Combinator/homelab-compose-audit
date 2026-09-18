# homelab-compose-audit

Offline CLI that cross-checks a pile of independent docker-compose files for port/name/network/mount collisions before `docker compose up`.

A homelab tends to accumulate one compose file per service, each written in isolation.
Nothing stops two of them from publishing port `8080`, naming a container `jellyfin`, or
bind-mounting the same directory — and you only find out when the second `docker compose up`
fails, one collision at a time.

This tool reads the files and reports every collision at once. It is **fully offline**: it
parses YAML with PyYAML, never contacts a docker daemon, and never inspects the host's real
port usage (you tell it which ports are taken).

## Install

```bash
pip install homelab-compose-audit
```

Or from a checkout:

```bash
pip install .
```

## Worked example

Given two stacks that were written independently:

`media.yml`

```yaml
version: "3.8"
services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    ports:
      - "8096:8096"
      - "7359:7359/udp"
    volumes:
      - /srv/media:/media:ro
      - ./jellyfin/config:/config
    networks:
      - proxy
  transmission:
    image: linuxserver/transmission
    container_name: transmission
    ports:
      - target: 9091
        published: 9091
        protocol: tcp
    volumes:
      - /srv/downloads:/downloads
networks:
  proxy:
    external: true
    name: edge-proxy
```

`web.yml`

```yaml
version: "3.9"
services:
  dashboard:
    image: homer:latest
    container_name: jellyfin
    ports:
      - "127.0.0.1:8096:80"
    volumes:
      - /srv/media/movies:/data
  caddy:
    image: caddy:2
    ports:
      - target: 80
        published: "8080"
      - "443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
networks:
  proxy:
    external:
      name: edge-proxy
```

Run the audit, telling it that port `8080` is already taken on the host:

```bash
homelab-compose-audit media.yml web.yml monitoring.yml --occupied-port 8080
```

Output:

```text
Checked 3 stack(s), 6 service(s): 5 conflict(s), 0 warning(s).

Host port collisions:
  - host port 8096/tcp is claimed by media/jellyfin [8096/tcp] and web/dashboard [127.0.0.1:8096/tcp]
  - host port 8080/tcp is claimed by web/caddy [8080/tcp] and (already occupied) [8080/tcp]

Duplicate container names:
  - container_name 'jellyfin' is used by media/jellyfin, web/dashboard

Shared external networks:
  - external network 'edge-proxy' is shared by media, web

Overlapping bind mounts:
  - host paths overlap: '/srv/media' (media/jellyfin) and '/srv/media/movies' (web/dashboard)
```

The exit code is `1`, so this fails a CI job.

Each finding is a real clash:

- `8096` — `media` publishes it on all interfaces, so `web`'s `127.0.0.1:8096` cannot bind.
- `8080` — `web` wants a port you declared occupied.
- `jellyfin` — two stacks want the same `container_name`; docker refuses the second.
- `edge-proxy` — both stacks attach to the same external network (allow it with
  `--allow-shared-network` when that is on purpose).
- `/srv/media` — `web` mounts a subdirectory of a tree `media` already mounts.

## Usage

```
homelab-compose-audit [PATH ...] [--glob PATTERN] [--occupied-port PORT]
                      [--occupied-ports-file FILE] [--format {text,json,markdown}]
                      [--allow-shared-network] [--fail-on-warning]
```

`PATH` may be a compose file or a directory containing `docker-compose.yml`,
`docker-compose.yaml`, `compose.yml` or `compose.yaml`.

| Flag | Meaning |
| --- | --- |
| `--glob PATTERN` | Glob for compose files, repeatable: `--glob 'stacks/*/docker-compose.yml'` |
| `--occupied-port PORT` | A host port already in use: `80`, `53/udp`, `127.0.0.1:8080`. Repeatable |
| `--occupied-ports-file FILE` | One port per line; `#` comments and blank lines ignored |
| `--format` | `text` (default), `json` or `markdown` |
| `--allow-shared-network` | Do not report two stacks sharing one external network |
| `--fail-on-warning` | Also exit nonzero when a file could not be read or parsed |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | No conflicts found |
| `1` | At least one conflict (or a warning with `--fail-on-warning`) |
| `2` | Usage error, or no target could be read at all |

## What it checks

**Host port collisions** — the short syntax (`8080:80`, `127.0.0.1:8080:80`, `8000-8002:80-82`,
`53:53/udp`), the long syntax (`published:` / `target:` / `protocol:` / `host_ip:`), and
`network_mode: host`, where every container port is bound directly on the host. Ports are
compared per protocol and per interface, so `127.0.0.1:8080` and `192.168.1.10:8080` do not
collide — but either collides with a wildcard `8080`.

**Duplicate `container_name`** — docker names are global, so the second stack fails to start.

**External network collisions** — two stacks attaching to the same `external: true` network.

**Overlapping bind mounts** — the same host path, or one nested inside the other. Two
read-only mounts of one path are not reported; a writer is.

All checks compare *different* stacks. A single compose file conflicting with itself is
docker's own error, not this tool's concern.

## CI usage

```yaml
- name: Audit compose stacks
  run: |
    pip install homelab-compose-audit
    homelab-compose-audit --glob 'stacks/*/docker-compose.yml' \
      --occupied-ports-file host-ports.txt --format markdown
```

A nonzero exit fails the job. `--format json` is convenient for further processing:

```json
{
  "ok": false,
  "summary": { "stacks": 2, "services": 3, "conflicts": 1, "warnings": 0 },
  "conflicts": [
    {
      "kind": "host_port",
      "detail": "host port 8096/tcp is claimed by media/jellyfin [8096/tcp] and (already occupied) [8096/tcp]",
      "where": [
        { "stack": "media", "path": "media.yml", "service": "jellyfin" },
        { "stack": "(already occupied)", "path": "" }
      ]
    }
  ],
  "warnings": []
}
```

## Robustness

One bad file never aborts the run. A file that is unreadable, not valid UTF-8, or not valid
YAML is recorded as a warning and the remaining stacks are still audited:

```text
Warnings:
  - stacks/legacy.yml: not valid UTF-8 (invalid continuation byte) — skipped
  - stacks/typo.yml: invalid YAML (expected <block end>, but found '<block sequence start>' at line 6, column 4) — skipped
```

Warnings alone do not fail the run — pass `--fail-on-warning` if you want them to. Exit code
`2` is reserved for the case where *no* target could be read at all. Files with a UTF-8 BOM are
read correctly rather than silently dropped.

## Scope

Supports compose v1 (top-level services), v2 and v3+ (`services:` / `networks:` / `volumes:`).

Deliberately **not** in v0.1: reading docker-daemon state, `.env` / `${VAR}` interpolation
(an uninterpolated port is skipped rather than guessed at), Swarm-only fields, and auto-fix.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest          # 132 tests, all offline
ruff check .
python -m build # wheel + sdist
```

## License

MIT — see [LICENSE](LICENSE).
