"""Free model registry — ordered by capability for fallback chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class FreeModel:
    id: str
    name: str
    context_window: int
    strength: str  # "strong", "medium", "light"


# Ordered by quality/capability — best first.
# All are free on OpenRouter (prompt + completion cost = $0).
FREE_MODELS: tuple[FreeModel, ...] = (
    FreeModel(
        id="nousresearch/hermes-3-llama-3.1-405b:free",
        name="Hermes 405B",
        context_window=131_072,
        strength="strong",
    ),
    FreeModel(
        id="qwen/qwen3-coder:free",
        name="Qwen3 Coder 480B",
        context_window=262_000,
        strength="strong",
    ),
    FreeModel(
        id="nvidia/nemotron-3-super-120b-a12b:free",
        name="Nemotron 3 Super 120B",
        context_window=262_144,
        strength="strong",
    ),
    FreeModel(
        id="google/gemma-4-31b-it:free",
        name="Gemma 4 31B",
        context_window=262_144,
        strength="medium",
    ),
    FreeModel(
        id="meta-llama/llama-3.3-70b-instruct:free",
        name="Llama 3.3 70B",
        context_window=65_536,
        strength="medium",
    ),
    FreeModel(
        id="openai/gpt-oss-120b:free",
        name="GPT OSS 120B",
        context_window=131_072,
        strength="medium",
    ),
    FreeModel(
        id="google/gemma-4-26b-a4b-it:free",
        name="Gemma 4 26B",
        context_window=262_144,
        strength="light",
    ),
    FreeModel(
        id="minimax/minimax-m2.5:free",
        name="MiniMax M2.5",
        context_window=196_608,
        strength="light",
    ),
)

# The auto-router — OpenRouter picks the best available free model.
AUTO_ROUTER_MODEL = "openrouter/free"


def get_models_for_strength(min_strength: str = "light") -> Sequence[FreeModel]:
    """Filter models by minimum strength tier."""
    tiers = {"strong": 3, "medium": 2, "light": 1}
    threshold = tiers.get(min_strength, 1)
    return tuple(m for m in FREE_MODELS if tiers.get(m.strength, 0) >= threshold)
