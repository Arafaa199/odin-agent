"""Tests for src.llm.models — free model registry."""

from __future__ import annotations

import pytest

from src.llm.models import FREE_MODELS, get_models_for_strength


class TestFreeModels:
    def test_all_models_have_free_suffix(self) -> None:
        for m in FREE_MODELS:
            assert ":free" in m.id, f"{m.id} missing :free suffix"

    def test_all_models_frozen(self) -> None:
        m = FREE_MODELS[0]
        with pytest.raises(AttributeError):
            m.id = "changed"  # type: ignore[misc]

    def test_all_models_have_required_fields(self) -> None:
        for m in FREE_MODELS:
            assert m.id
            assert m.name
            assert m.context_window > 0
            assert m.strength in ("strong", "medium", "light")

    def test_ordering_strong_first(self) -> None:
        strengths = [m.strength for m in FREE_MODELS]
        strong_indices = [i for i, s in enumerate(strengths) if s == "strong"]
        medium_indices = [i for i, s in enumerate(strengths) if s == "medium"]
        if strong_indices and medium_indices:
            assert max(strong_indices) < min(medium_indices), (
                "Strong models should come before medium models"
            )

    def test_no_duplicates(self) -> None:
        ids = [m.id for m in FREE_MODELS]
        assert len(ids) == len(set(ids)), "Duplicate model IDs found"


class TestGetModelsForStrength:
    def test_strong_filter(self) -> None:
        strong = get_models_for_strength("strong")
        assert all(m.strength == "strong" for m in strong)
        assert len(strong) < len(FREE_MODELS)

    def test_medium_filter(self) -> None:
        medium = get_models_for_strength("medium")
        assert all(m.strength in ("strong", "medium") for m in medium)

    def test_light_returns_all(self) -> None:
        light = get_models_for_strength("light")
        assert len(light) == len(FREE_MODELS)

    def test_unknown_strength_returns_all(self) -> None:
        result = get_models_for_strength("unknown")
        assert len(result) == len(FREE_MODELS)
