import pytest

from plainboard_runner import errors


@pytest.fixture(autouse=True)
def _error_log(tmp_path_factory, monkeypatch):
    """The tests' errors never go into the real ~/.plainboard/runner-errors.log (PLAINBOARD_RUNNER_ERRORS)."""
    monkeypatch.setattr(errors, "ERROR_LOG", tmp_path_factory.getbasetemp() / "runner-errors.log")


@pytest.fixture(autouse=True)
def _no_cli_lookups(monkeypatch):
    """No test starts Claude Code for its list of models or installs a Codex CLI (codex_cli) by itself."""
    from plainboard_runner import codex_cli
    from plainboard_runner.providers.claude_code import ClaudeCodeProvider

    async def nothing(*_args):
        return None

    async def no_models(_self):
        return [], []

    monkeypatch.setattr(codex_cli, "keep_current", nothing)
    monkeypatch.setattr(ClaudeCodeProvider, "_cli_models", no_models)
