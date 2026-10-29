# PlainBoard – notes for coding agents

Read `ARCHITECTURE.md` first. Layout: `app/` (FastAPI backend + orchestration), `runner/` (agent
execution, providers), `protocol/` (shared wire models), `frontend/` (React + TS + Tailwind v4).

## Rules
- Domain logic lives in `app/plainboard/services/`. REST endpoints (`api/`) and agent tools (`tools/`)
  are thin layers over services. Don't duplicate logic between them.
- New agent capabilities = a new `@tool` in `app/plainboard/tools/`. Keep tool descriptions and
  results short: they cost tokens on every call.
- Provider-specific code stays in `runner/plainboard_runner/providers/`. The app must stay
  provider-agnostic and talk to the runner only through `protocol` models.
- Keep system prompts stable (no timestamps) so prompt caching works. Volatile data goes into the
  user turn (`prompts.with_preface`).
- Timestamps are naive UTC in the DB and serialized with a trailing `Z` (`schemas.UTCDateTime`).
- All UI strings go through i18n (`frontend/src/i18n/en.json`).
- Schema changes need an Alembic migration (see ARCHITECTURE.md).

## Commands
```bash
docker compose up -d --build                     # run everything (http://localhost:8080)
docker run --rm -w /src/app --entrypoint python plainboard-app -m pytest -q
docker run --rm -w /src/runner plainboard-runner python -m pytest -q
cd frontend && npm run build                     # typecheck + production build
cd frontend && npm run dev                       # dev server, proxies /api to :8080
```
On Git Bash for Windows, prefix `docker run ... -w /src/...` with `MSYS_NO_PATHCONV=1`.
