"""Which model of another provider stands in for a model: the one of the same size.

Used wherever a model has to be carried over to a provider that does not have it: an agent is
given another provider (``agents``), or runs on another one while its own is used up
(``accounts.route``).
"""

from __future__ import annotations

from plainboard_protocol import ProviderInfo

SIZES = ("small", "medium", "large", "xlarge")  # ModelInfo.tier, in order


def equivalent_model(catalog: list[ProviderInfo], model: str, target: str) -> str | None:
    """The target provider's model of the same size (tier) as ``model``, which may belong to any
    provider; ``model`` itself if the target has it. If the target has none of that size: its next
    smaller one, so that nothing runs on more than it was given; its smallest, if all are larger.
    A model no provider lists counts as medium. None if the target is unknown or has no models."""
    provider = next((p for p in catalog if p.name == target), None)
    if provider is None or not provider.models:
        return None
    if any(m.id == model for m in provider.models):
        return model
    tier = next((m.tier for p in catalog for m in p.models if m.id == model), "medium")
    first: dict[str, str] = {}
    for m in provider.models:
        first.setdefault(m.tier, m.id)
    at = SIZES.index(tier)
    return next(first[size] for size in (*reversed(SIZES[: at + 1]), *SIZES[at + 1 :]) if size in first)
