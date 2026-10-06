from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class ActionItem(BaseModel):
    description: str
    assignee: str = "self"  # self | other | unknown
    urgency: str = "medium"  # low | medium | high
    confidence: float = 0.0  # 0.0 = unknown, not 0.8
    project: str | None = None
    workstream: str | None = None
    due_date: str | None = None  # YYYY-MM-DD (resolved by code, not LLM)
    due_raw: str | None = None  # exact words from transcript about timing
    due_confidence: float = 0.0
    inferred_due: bool = False
    tags: list[str] = Field(default_factory=list)
    item_type: str = "task"  # task | decision | info | question
    source_quote: str = ""
    source_time_start: str = ""  # M:SS or H:MM:SS
    source_time_end: str = ""
    requires_confirmation: bool = False
    confirmation_question: str | None = None

    def idempotency_key(self, recording_id: str) -> str:
        normalized = self.description.strip().lower()
        parts = f"{recording_id}|{normalized}|{self.due_date or ''}|{self.item_type}"
        return hashlib.sha256(parts.encode()).hexdigest()[:16]


class ProcessedRecording(BaseModel):
    source_file: str
    date: str
    duration_seconds: float = 0.0
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    full_text: str = ""
    summary: str = ""
    action_items: list[ActionItem] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    processed_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    def to_markdown(self) -> str:
        lines = [
            f"# {Path(self.source_file).stem}",
            "",
            f"**Date**: {self.date}",
            f"**Duration**: {self._fmt_duration()}",
            f"**Participants**: {', '.join(self.participants) or 'Unknown'}",
            f"**Source**: `{Path(self.source_file).name}`",
            "",
            "---",
            "",
            "## Summary",
            "",
            self.summary or "_No summary generated._",
            "",
        ]

        if self.key_decisions:
            lines += ["## Key Decisions", ""]
            for d in self.key_decisions:
                lines.append(f"- {d}")
            lines.append("")

        if self.action_items:
            lines += ["## Action Items", ""]
            for a in self.action_items:
                urgency_tag = f" `{a.urgency}`" if a.urgency != "medium" else ""
                assignee_tag = f" @{a.assignee}" if a.assignee != "self" else ""
                due_tag = f" (due {a.due_date})" if a.due_date else ""
                project_tag = f" [{a.project}]" if a.project else ""
                ts_ref = ""
                if a.source_time_start:
                    ts_ref = f" `[{a.source_time_start}"
                    if a.source_time_end:
                        ts_ref += f"-{a.source_time_end}"
                    ts_ref += "]`"
                conf_tag = f" ⚠️{a.confidence:.0%}" if a.confidence < 0.7 else ""
                lines.append(
                    f"- [ ] {a.description}{assignee_tag}{urgency_tag}{due_tag}{project_tag}{ts_ref}{conf_tag}"
                )
                if a.source_quote:
                    lines.append(f"  > _{a.source_quote}_")
            lines.append("")

        lines += ["## Transcript", ""]
        for seg in self.transcript:
            text = seg.text.strip()
            if not text:
                continue
            ts = self._fmt_ts(seg.start)
            speaker = f"**{seg.speaker}**: " if seg.speaker else ""
            lines.append(f"[{ts}] {speaker}{text}")
        lines.append("")

        return "\n".join(lines)

    def _fmt_duration(self) -> str:
        m, s = divmod(int(self.duration_seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m {s}s"
        return f"{m}m {s}s"

    @staticmethod
    def _fmt_ts(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"
