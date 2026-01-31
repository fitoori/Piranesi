# Piranesi

This repository defines a Docker Compose stack for a personalized AI assistant
ecosystem. The `docker-compose.yml` file provisions a full local runtime with
LLMs, orchestration, data stores, and optional UI tooling.

## What the Compose stack does

### Core services
- **Ollama (CPU)**: Runs the local LLM runtime and exposes the configured port
  (`${OLLAMA_PORT}`) so other containers and your host can access it.
- **Redis**: Simple cache/queue backend used by the Discord bot or other
  integrations.
- **Postgres**: Persistent database backing n8n workflows.
- **Qdrant**: Vector database for embeddings and retrieval use cases.
- **Letta**: Agent runtime and server for stateful assistants.

### Applications
- **Browser-Use WebUI**: A web UI + VNC stack that connects to the Ollama
  service in this Compose network for local model inference.
- **Discord Bot**: A bot container that points to the Ollama and Redis services
  using the IP/port settings from `.env`.
- **n8n**: Workflow automation platform connected to Postgres and Ollama.
  - **n8n-import** runs once at startup to import demo credentials/workflows if
    `./n8n/demo-data` is present.

### Helper job
- **ollama-pull-devstral**: A one-shot helper container that waits for Ollama
  to become healthy and then pulls the `devstral` model if it is not already
  present.

## Networking
- All services attach to the custom `ollama-net` bridge network.
- The subnet is configured by `${SUBNET_ADDRESS}` in your `.env` file.
- Several services use fixed container IPs from `.env` to simplify cross-container
  configuration.

## Storage
Persistent data is stored in named Docker volumes:
- `ollama` for model data
- `redis` for Redis storage
- `postgres_storage` for Postgres data
- `n8n_storage` for n8n state
- `qdrant_storage` for Qdrant data
- `discord` for the Discord bot workspace

## Ports exposed to the host
Each service below lists the host port(s) that are bound in `docker-compose.yml`
so you know exactly where to connect from your machine.

- **Browser-Use WebUI**: `7788` (web UI), `6080` (noVNC), `5901` (VNC),
  `9222` (Chrome debugging).
- **Ollama**: `${OLLAMA_PORT}` (container listens on the same port).
- **Redis**: `${REDIS_PORT}` (container listens on the same port).
- **Postgres**: `5432`.
- **n8n**: `5678`.
- **Qdrant**: `6333`.
- **Letta**: `${LETTA_PORT:-8283}` → `8283` in the container (defaults to `8283`
  on the host if `LETTA_PORT` is unset).
- **SearXNG**: `8080`.
- **Discord bot**: no host port binding (outbound-only service).

## Environment configuration
Populate `.env` with the values referenced in `docker-compose.yml` (for example
`SUBNET_ADDRESS`, `OLLAMA_PORT`, `LETTA_PORT`, `POSTGRES_*`, `N8N_*`, and
Discord/Redis settings). These variables drive port bindings, IP assignments,
and application credentials.

Optional image overrides (useful if you host your own images):
- `BROWSER_USE_WEBUI_IMAGE` (defaults to `browser-use/webui:latest`)
- `DISCORD_IMAGE` (defaults to `kevinthedang/discord-ollama:latest`)
