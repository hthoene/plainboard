# Releasing PlainBoard

PlainBoard's images on Docker Hub (`thoenehannes/plainboard`, `thoenehannes/plainboard-runner`, both
amd64 and arm64) come in two channels, published by `.github/workflows/publish.yml`:

| Tag | What | When |
| --- | --- | --- |
| `:edge` | the newest commit of main | every commit on main that passed CI (overwritten) |
| `:X.Y.Z` | one release | a pushed tag `vX.Y.Z` |
| `:X.Y` | the newest release of that line | with a release, if it is the newest `X.Y.*` |
| `:latest` | the newest release | with a release, if it is the newest of all |

`deploy/docker-compose.yml` uses `:latest`, so installations only change when a release is made.
`PLAINBOARD_TAG=edge` (or a fixed `X.Y.Z`) picks another channel. There are no per-commit tags.

## Making a release

Only when the user asks for one. Pick the commit (normally the newest on main whose CI passed), then:

```bash
git tag vX.Y.Z <commit> && git push origin vX.Y.Z
```

That builds the tag's commit and pushes `:X.Y.Z`, plus `:X.Y` and `:latest` when it is the newest of its
line / of all: a fix for an older line (`v0.1.4` after `v0.2.0`) never takes over `:latest`. The first
release is `v0.1.0` (the version in the `pyproject.toml` files); then `Z` for fixes, `Y` for features,
`X` once PlainBoard declares a stable 1.0.

To publish an existing release again (a failed run, a rebuilt base image): Actions → Publish images →
Run workflow, version `X.Y.Z`. The tag must exist; without a version it makes an `:edge` build of the
chosen branch.

## What the version changes

- The app knows its release (build argument `PLAINBOARD_VERSION` → `/app/version`) and shows it in
  Settings; an `:edge` or local build shows `edge-<commit>`.
- The update notice: a release asks GitHub for newer tags `vX.Y.Z` and names the newest; an edge build
  counts the newer commits on main, as before.
- Settings → Runners hands out `plainboard-runner:latest` on a release, `:edge` otherwise. Runners
  update their code from the app themselves, independent of tags; `docker compose pull` is only needed
  when a new image is (other system tools or Python packages).
