"""Providers that are an address: a model server of one's own, a model host, a gateway.

The agent's turn runs in the same harness as on Claude (Claude Code, with all its tools and
PlainBoard's), pointed at another address. Two interfaces are spoken:

- ``messages``: the server speaks Anthropic's Messages interface itself. The harness talks to it directly.
- ``chat``: the server speaks Chat Completions. The runner translates (``translate.py``).

An account holds the address (empty: the provider's usual one), the key, the models it offers and
how many tokens they take at once. No model names are kept here: they change by the month, and the
server names what it has (``models``). To add a provider, add a line to ``ENDPOINTS``.

Anthropic does not support Claude Code on models that are not Claude's. What is set in ``auth_env``
keeps the harness to what any such server understands; how well an agent works is the model's part.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx
from plainboard_protocol import CompletionResult, CompletionSpec, Credentials, EndpointInfo, LimitWindow, ModelInfo, ProviderInfo, RunEvent, RunSpec

from .. import translate
from ..tool_bridge import ToolBridge
from .claude_code import ClaudeCodeProvider

# Where the runner itself answers, for the harness it starts (the translation is one of its routes).
RUNNER_PORT = int(os.environ.get("PLAINBOARD_RUNNER_PORT", "8090"))
# What a model takes at once if the account does not say. Claude Code assumes a million tokens for a
# model it does not know and would sum up its work far too late; too low only sums up early.
CONTEXT_TOKENS = 128_000
HOST = "http://host.docker.internal"  # this machine, seen from the runner's container
LOCAL = "A model server of your own. The model has to call tools well and take a long prompt: give it 64,000 tokens of context or more."


@dataclass(frozen=True)
class Endpoint:
    name: str
    label: str
    api: Literal["messages", "chat"]
    url: str = ""  # the usual address; empty: the account names it
    key: bool = True
    hint: str = ""
    models: tuple[ModelInfo, ...] = ()


ENDPOINTS: tuple[Endpoint, ...] = (
    # Model servers of one's own. They speak the Messages interface themselves (Ollama from 0.14, LM Studio from 0.4.1).
    Endpoint("ollama", "Ollama", "messages", f"{HOST}:11434", key=False, hint=f"{LOCAL} For Ollama's cloud models: https://ollama.com and a key."),
    Endpoint("lmstudio", "LM Studio", "messages", f"{HOST}:1234", key=False, hint=LOCAL),
    Endpoint("llamacpp", "llama.cpp server", "messages", f"{HOST}:8080", key=False, hint=f"{LOCAL} Start it with --jinja, or it calls no tools."),
    Endpoint("vllm", "vLLM", "messages", f"{HOST}:8000", key=False, hint=f"{LOCAL} Start it with --enable-auto-tool-choice and a tool call parser."),
    # Hosts with the Messages interface.
    Endpoint("openrouter", "OpenRouter", "messages", "https://openrouter.ai/api", hint="Many models of many hosts behind one key."),
    Endpoint("deepseek", "DeepSeek", "messages", "https://api.deepseek.com/anthropic"),
    Endpoint("moonshot", "Kimi (Moonshot)", "messages", "https://api.moonshot.ai/anthropic"),
    Endpoint("zai", "GLM (Z.ai)", "messages", "https://api.z.ai/api/anthropic", hint="It names no models itself: type them in."),
    Endpoint("minimax", "MiniMax", "messages", "https://api.minimax.io/anthropic"),
    Endpoint("qwen", "Qwen (Alibaba Model Studio)", "messages", "https://dashscope-intl.aliyuncs.com/apps/anthropic", hint="It names no models itself: type them in. A Coding Plan key has an address of its own."),
    Endpoint("fireworks", "Fireworks", "messages", "https://api.fireworks.ai/inference", hint="It names no models here: type them in, as accounts/fireworks/models/..."),
    Endpoint("together", "Together", "messages", "https://api.together.ai", hint="It names no models here: type them in."),
    # Hosts with Chat Completions: the runner translates.
    Endpoint("gemini", "Gemini (Google)", "chat", "https://generativelanguage.googleapis.com/v1beta/openai", hint="With a key from Google AI Studio."),
    Endpoint("xai", "Grok (xAI)", "chat", "https://api.x.ai/v1"),
    Endpoint("mistral", "Mistral", "chat", "https://api.mistral.ai/v1"),
    Endpoint("groq", "Groq", "chat", "https://api.groq.com/openai/v1"),
    Endpoint("cerebras", "Cerebras", "chat", "https://api.cerebras.ai/v1", hint="It names no models itself: type them in."),
    Endpoint("huggingface", "Hugging Face (Inference Providers)", "chat", "https://router.huggingface.co/v1", hint="Models as org/model, with a Hugging Face token."),
    Endpoint("nvidia", "Nvidia NIM", "chat", "https://integrate.api.nvidia.com/v1"),
    Endpoint("azure", "Azure OpenAI", "chat", hint="The address of your resource, https://NAME.openai.azure.com/openai/v1. A model is named as its deployment."),
    # Anything else that speaks one of the two.
    Endpoint("messages", "Other: Messages interface", "messages", key=False, hint="A server or gateway that speaks Anthropic's Messages interface (LiteLLM, a gateway of your own). The address without /v1."),
    Endpoint("chat", "Other: Chat Completions interface", "chat", key=False, hint="A server that speaks OpenAI's Chat Completions interface. The address with /v1, without /chat/completions."),
)


class EndpointProvider(ClaudeCodeProvider):
    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    def info(self) -> ProviderInfo:
        e = self.endpoint
        return ProviderInfo(name=e.name, label=e.label, models=list(e.models), efforts=[], endpoint=EndpointInfo(url=e.url, key=e.key, hint=e.hint))

    def address(self, creds: Credentials) -> str:
        url = (creds.endpoint_url or self.endpoint.url).strip().rstrip("/")
        if not url:
            raise RuntimeError(f"{self.endpoint.label}: no address is set (Settings, Subscriptions).")
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"{self.endpoint.label}: the address has to start with http:// or https://.")
        return url

    def auth_env(self, creds: Credentials, model: str = "") -> dict[str, str]:
        url = self.address(creds)
        if self.endpoint.key and not creds.endpoint_key:
            raise RuntimeError(f"{self.endpoint.label}: no API key is set (Settings, Subscriptions).")
        if self.endpoint.api == "chat":
            url = f"http://127.0.0.1:{RUNNER_PORT}/compat/{translate.address(url)}"
        env = {
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_AGENT_SDK_CLIENT_APP": "plainboard/0.1",
            "ANTHROPIC_BASE_URL": url,
            # Sent as "Authorization: Bearer", which is what these servers read. One without a key still gets a word.
            "ANTHROPIC_AUTH_TOKEN": creds.endpoint_key or "none",
            # No sign-in of Claude's that the runner's own environment may carry.
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            # Claude Code's own side requests (telemetry, update and error reports) go to Anthropic: not from here.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Request fields that are in trial at Anthropic. Another server refuses what it does not know.
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            # Finding tools on demand is done by Anthropic's servers: here every tool is in the prompt.
            "ENABLE_TOOL_SEARCH": "false",
            # Claude's way to ask for thinking. A model that thinks does so by itself.
            "CLAUDE_CODE_DISABLE_THINKING": "1",
            # A line at the top of the prompt that differs from request to request: a model server
            # that keeps the read prompt between requests would read all of it again each time.
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(creds.endpoint_context or CONTEXT_TOKENS),
            # A model server of one's own reads a long prompt for minutes before its first word.
            "API_TIMEOUT_MS": "3000000",
        }
        if model:
            # The harness names models of its own for side work (a title, a sub-agent). Here there is one.
            tiers = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
            env.update({name: model for name in tiers})
        return env

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        # These models have no effort levels of Claude's, and what a turn cost is not known here: the
        # harness would reckon with Claude's prices.
        async for event in super().run(spec.model_copy(update={"effort": None}), bridge):
            if event.type == "result":
                event = event.model_copy(update={"session_cost_usd": 0.0})
            yield event

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        result = await super().complete(spec.model_copy(update={"effort": None}))
        return result.model_copy(update={"cost_usd": 0.0})

    async def limits(self, credentials: Credentials) -> list[LimitWindow]:
        return []

    async def plan(self, credentials: Credentials) -> tuple[str | None, float | None]:
        return None, None

    async def models(self, credentials: Credentials) -> list[str]:
        """What the server says it offers. Both interfaces list models the same way."""
        url = self.address(credentials)
        key = credentials.endpoint_key
        headers = {"Authorization": f"Bearer {key}", "x-api-key": key, "anthropic-version": "2023-06-01"} if key else {}
        # A Messages server is addressed without "/v1" (the harness adds it), a Chat Completions server with it.
        paths = ["/v1/models", "/models"] if self.endpoint.api == "messages" else ["/models"]
        refusal = ""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for path in paths:
                try:
                    response = await client.get(url + path, headers=headers)
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"{url} does not answer: {exc}") from exc
                if response.status_code >= 400:
                    refusal = f"{url}{path} answered {response.status_code}: {response.text[:200]}"
                    continue
                try:
                    listed = response.json()
                except ValueError:
                    refusal = f"{url}{path} did not answer with a list of models"
                    continue
                rows = listed.get("data") or listed.get("models") or [] if isinstance(listed, dict) else listed
                names = [str(row.get("id") or row.get("name") or "").removeprefix("models/") if isinstance(row, dict) else str(row) for row in rows]
                return sorted({name for name in names if name})
        raise RuntimeError(refusal or f"{url} names no models")


def providers() -> list[EndpointProvider]:
    return [EndpointProvider(endpoint) for endpoint in ENDPOINTS]
