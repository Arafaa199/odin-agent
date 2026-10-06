"""Tests for src.llm.complexity — complexity scoring."""

from __future__ import annotations

import pytest

from src.llm.complexity import (
    CLI_THRESHOLD,
    score_complexity,
)


class TestComplexityScoring:
    def test_simple_task_low_score(self) -> None:
        result = score_complexity("Check the status of the dashboard", tags={"confirm"})
        assert result.score < CLI_THRESHOLD
        assert not result.needs_cli

    def test_critical_tag_raises_score(self) -> None:
        result = score_complexity("Deploy the app", tags={"critical"})
        assert result.score > 0
        assert "complex tags" in result.reason

    def test_operator_tag_raises_score(self) -> None:
        result = score_complexity("Run forensics sweep", tags={"operator"})
        assert result.score > 0

    def test_prod_write_tier_raises_score(self) -> None:
        result = score_complexity("Update the database", tier="PROD_WRITE")
        assert result.score >= 0.4
        # PROD_WRITE (0.4) + multiple patterns push over 0.6 threshold
        result2 = score_complexity(
            "Run the database schema migration and deploy to production",
            tier="PROD_WRITE",
        )
        assert result2.needs_cli

    def test_prod_readonly_moderate(self) -> None:
        result = score_complexity("Query the database", tier="PROD_READONLY")
        assert result.score > 0
        assert result.score < CLI_THRESHOLD  # not enough alone

    def test_complex_patterns_detected(self) -> None:
        result = score_complexity("Run the database migration and deploy to production environment")
        assert "complex patterns" in result.reason

    def test_long_prompt_adds_score(self) -> None:
        short = score_complexity("Fix the bug")
        long = score_complexity("Fix the bug " + "x" * 9000)
        assert long.score > short.score

    def test_multiple_complex_tags_accumulate(self) -> None:
        single = score_complexity("task", tags={"critical"})
        double = score_complexity("task", tags={"critical", "operator"})
        assert double.score > single.score

    def test_simple_tags_reduce_score(self) -> None:
        # Start with some positive score so reduction is visible
        bare = score_complexity("Run the migration", tags={"critical"})
        with_simple = score_complexity("Run the migration", tags={"critical", "email", "followup"})
        assert with_simple.score < bare.score

    def test_score_clamped_to_0_1(self) -> None:
        # Many simple tags shouldn't go negative
        result = score_complexity(
            "hi",
            tags={"confirm", "followup", "meeting", "email", "demo", "plaud"},
        )
        assert result.score >= 0.0

        # Many complex signals shouldn't exceed 1.0
        result = score_complexity(
            "migration deploy rollback database schema security audit " + "x" * 10000,
            tags={"critical", "operator", "security", "migration", "prod_write"},
            tier="PROD_WRITE",
        )
        assert result.score <= 1.0

    def test_frozen_dataclass(self) -> None:
        result = score_complexity("test")
        with pytest.raises(AttributeError):
            result.score = 0.5  # type: ignore[misc]

    def test_auto_tier_no_extra_score(self) -> None:
        result = score_complexity("Simple task", tier="AUTO")
        assert "PROD" not in result.reason

    def test_needs_cli_threshold(self) -> None:
        """Tasks just below and above threshold behave correctly."""
        # Tags alone at boundary
        below = score_complexity("task", tags={"critical"})  # 0.3
        above = score_complexity(
            "migration task", tags={"critical", "operator"}
        )  # 0.3 + 0.3 + pattern
        assert not below.needs_cli or below.score >= CLI_THRESHOLD
        assert above.needs_cli
