# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately via
[GitHub security advisories](https://github.com/hthoene/plainboard/security/advisories/new).
You will get a response as soon as possible; please allow some time for a fix before disclosure.

## Security model (what to expect)

- **Agents have full control of the runner container** (shell, passwordless sudo, network). This is by
  design. Only give agents credentials (API keys, GitHub tokens, MCP secrets) you are comfortable with
  them using.
- The **app container** (UI, API, database, secrets) is separate from the runner. Agents reach the
  app only through tool calls, authenticated with a random token that is valid only while the run lasts.
- The web UI binds to `127.0.0.1` by default and has **no login unless `PLAINBOARD_PASSWORD` is set**.
  Before exposing it on a network, set a password and put it behind a reverse proxy with TLS.
- Secrets (tokens, MCP environment variables and headers) are encrypted at rest with a key generated
  in the `data` volume. Anyone with access to that volume can decrypt them.
- stdio MCP servers run commands inside the runner container; only add servers you trust.
