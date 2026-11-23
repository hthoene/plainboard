"""Lists of models and effort levels that come from the CLIs themselves (#92), and the Codex CLI keeping itself current."""

from plainboard_runner import codex_cli
from plainboard_runner.providers.base import merge_efforts
from plainboard_runner.providers.claude_code import ClaudeCodeProvider, cli_models

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LEVELS = {"supportedEffortLevels": EFFORTS}
# As Claude Code's initialize answer has them, with a model PlainBoard does not know yet.
CLAUDE = [
    {"value": "default", "resolvedModel": "claude-opus-6", "displayName": "Default (recommended)", "description": "Use the default model (currently Opus 6) · $5/$25", **LEVELS},
    {"value": "opus", "resolvedModel": "claude-opus-6", "displayName": "Opus", "description": "Opus 6 · Best for everyday, complex tasks", "supportedEffortLevels": [*EFFORTS, "ultra"]},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet", "description": "Sonnet 5.5 · Efficient", **LEVELS},
    {"value": "something", "displayName": "Not a model id"},
]


def test_effort_levels_nobody_knew_are_taken_over_in_their_place():
    assert merge_efforts(EFFORTS, ["low", "max", "ultra"], ["minimal", "low"]) == [*EFFORTS, "ultra", "minimal"]
    assert merge_efforts(["low", "max"], ["low", "medium", "max"]) == ["low", "medium", "max"]


def test_claude_code_s_own_list_adds_new_models():
    models, efforts = cli_models(CLAUDE)
    assert [(m.id, m.label, m.tier) for m in models] == [("claude-opus-6", "Claude Opus 6", "large")]
    assert efforts == [*EFFORTS, "ultra"]


async def test_claude_s_catalog_is_refreshed_in_the_background(monkeypatch):
    provider = ClaudeCodeProvider()

    async def listed(_self):
        return cli_models(CLAUDE)

    monkeypatch.setattr(ClaudeCodeProvider, "_cli_models", listed)
    first = await provider.catalog()  # never waits for Claude Code to start
    assert "claude-opus-6" not in [m.id for m in first.models]
    await provider._listing
    info = await provider.catalog()
    assert [m.id for m in info.models][-1] == "claude-opus-6" and "claude-haiku-5-5" in [m.id for m in info.models]
    assert info.efforts[-1] == "ultra" and provider._listing.done()  # fresh: not asked again


def fake_cli(monkeypatch, newest: str, have: str = "codex-cli 0.171.0"):
    installs = []

    async def latest():
        return newest

    async def installed(_bin):
        return have

    async def install(version, root):
        installs.append(version)
        path = codex_cli._bin(root, version)
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        return path

    monkeypatch.setattr(codex_cli, "latest", latest)
    monkeypatch.setattr(codex_cli, "installed", installed)
    monkeypatch.setattr(codex_cli, "install", install)
    monkeypatch.delenv("PLAINBOARD_CODEX_BIN", raising=False)
    monkeypatch.delenv("PLAINBOARD_CODEX_UPDATE", raising=False)
    return installs


async def passes(_bin):
    return None


async def fails(_bin):
    raise RuntimeError("its app server does not start")


async def test_a_newer_codex_cli_is_used_once_it_passes_its_try(tmp_path, monkeypatch):
    installs = fake_cli(monkeypatch, "0.172.0")
    assert codex_cli.binary(tmp_path) == "codex"  # the image's
    assert await codex_cli.update(tmp_path, check=passes) == "0.172.0"
    assert codex_cli.binary(tmp_path) == str(tmp_path / "0.172.0" / "node_modules" / ".bin" / "codex")
    fake_cli(monkeypatch, "0.172.0", have="codex-cli 0.172.0")
    assert await codex_cli.update(tmp_path, check=passes) is None and installs == ["0.172.0"]  # current already
    # Two more releases: at most two versions stay, the one in use among them.
    for version in ("0.173.0", "0.174.0"):
        fake_cli(monkeypatch, version, have="codex-cli 0.0.1")
        assert await codex_cli.update(tmp_path, check=passes) == version
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["0.173.0", "0.174.0"]


async def test_a_codex_cli_that_fails_its_try_is_not_used(tmp_path, monkeypatch):
    installs = fake_cli(monkeypatch, "0.172.0")
    assert await codex_cli.update(tmp_path, check=fails) is None
    assert codex_cli.binary(tmp_path) == "codex" and not (tmp_path / "0.172.0").exists()
    assert await codex_cli.update(tmp_path, check=passes) is None and installs == ["0.172.0"]  # not tried again
    fake_cli(monkeypatch, "0.173.0-alpha.1")
    assert await codex_cli.update(tmp_path, check=passes) is None  # no pre-releases


async def test_no_update_with_a_cli_of_one_s_own(tmp_path, monkeypatch):
    fake_cli(monkeypatch, "0.172.0")
    monkeypatch.setenv("PLAINBOARD_CODEX_BIN", "/opt/my-codex")
    assert await codex_cli.update(tmp_path, check=passes) is None and codex_cli.binary(tmp_path) == "/opt/my-codex"

