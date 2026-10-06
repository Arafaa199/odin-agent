"""
Transcript Conditioner
Cleans filler words and condenses long transcripts into concise, action-preserving summaries.

Pipeline: raw whisper segments → filler removal → chunk + condense (for long recordings) → clean transcript
"""

from __future__ import annotations

import logging
import re

import ollama

from .schema import TranscriptSegment

log = logging.getLogger("plaud-conditioner")

# Filler patterns — ordered by specificity (longer patterns first)
FILLER_PATTERNS: list[re.Pattern] = [
    # Multi-word fillers
    re.compile(r"\byou know what I mean\b", re.IGNORECASE),
    re.compile(r"\byou know what\b", re.IGNORECASE),
    re.compile(r"\byou know\b", re.IGNORECASE),
    re.compile(r"\bI mean\b", re.IGNORECASE),
    re.compile(r"\bkind of\b", re.IGNORECASE),
    re.compile(r"\bsort of\b", re.IGNORECASE),
    re.compile(r"\bat the end of the day\b", re.IGNORECASE),
    re.compile(r"\bto be honest\b", re.IGNORECASE),
    # "basically" is almost always filler; "literally" and "actually" can carry
    # meaning ("actually, we decided against it") so only strip them when
    # surrounded by commas or at segment boundaries — not mid-sentence.
    re.compile(r"\bbasically\b", re.IGNORECASE),
    re.compile(r"(?:^|,)\s*literally\s*(?:,|$)", re.IGNORECASE),
    re.compile(r"(?:^|,)\s*actually\s*(?:,|$)", re.IGNORECASE),
    # Hesitation sounds
    re.compile(r"\bu+h+\b", re.IGNORECASE),
    re.compile(r"\bu+m+\b", re.IGNORECASE),
    re.compile(r"\be+r+\b", re.IGNORECASE),
    re.compile(r"\bah+\b", re.IGNORECASE),
    re.compile(r"\bmm+\b", re.IGNORECASE),
    re.compile(r"\bhmm+\b", re.IGNORECASE),
    # Stuttered repeated words: "the the the" → "the"
    re.compile(r"\b(\w+)([ ,]+\1){1,}\b", re.IGNORECASE),
    # "like" as filler (between commas or at segment start/end)
    re.compile(r"(?:^|,)\s*like\s*(?:,|$)", re.IGNORECASE),
    re.compile(r"\blike\s*,\s*like\b", re.IGNORECASE),
]

# Collapse repeated "no" / "yes" chains: "no, no, no, no" → "no"
REPEAT_CHAINS = re.compile(
    r"\b(no|yes|yeah|okay|ok|right|so)\b(?:\s*[,.]?\s*\1\b){2,}",
    re.IGNORECASE,
)

# Multi-space collapse
MULTI_SPACE = re.compile(r"  +")
# Orphaned punctuation
ORPHAN_PUNCT = re.compile(r"(?:^[,.\s]+|[,.\s]+$)")
DOUBLE_COMMA = re.compile(r",\s*,")


def clean_fillers(text: str) -> str:
    """Remove filler words, hesitations, and stutters from a text segment."""
    result = text
    for pat in FILLER_PATTERNS:
        result = pat.sub(" ", result)
    result = REPEAT_CHAINS.sub(r"\1", result)
    result = DOUBLE_COMMA.sub(",", result)
    result = MULTI_SPACE.sub(" ", result)
    result = ORPHAN_PUNCT.sub("", result)
    return result.strip()


def clean_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Clean filler words from all segments, dropping empty ones."""
    cleaned = []
    for seg in segments:
        text = clean_fillers(seg.text)
        if text and len(text) > 2:
            cleaned.append(
                TranscriptSegment(
                    start=seg.start,
                    end=seg.end,
                    text=text,
                    speaker=seg.speaker,
                )
            )
    return cleaned


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _chunk_segments(
    segments: list[TranscriptSegment],
    chunk_minutes: int = 10,
) -> list[list[TranscriptSegment]]:
    """Split segments into time-based chunks."""
    if not segments:
        return []
    chunk_seconds = chunk_minutes * 60
    chunks: list[list[TranscriptSegment]] = []
    current_chunk: list[TranscriptSegment] = []
    chunk_start = segments[0].start

    for seg in segments:
        if seg.start - chunk_start >= chunk_seconds and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            chunk_start = seg.start
        current_chunk.append(seg)

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _build_chunk_text(segments: list[TranscriptSegment]) -> str:
    lines = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            lines.append(f"[{_fmt_ts(seg.start)}] {text}")
    return "\n".join(lines)


CONDENSE_PROMPT = """\
You are condensing a section of a meeting transcript. Your job is to produce a SHORTER version \
that preserves ALL of the following:

1. **Decisions made** — what was agreed on
2. **Action items** — tasks someone committed to, with who and any deadlines
3. **Key information** — facts, numbers, names, dates that matter
4. **Conclusions** — what a back-and-forth discussion resolved to

REMOVE:
- Filler words, false starts, repetition
- Back-and-forth that led nowhere (keep only the conclusion)
- Social pleasantries, off-topic tangents
- Redundant restatements of the same point

FORMAT:
- Use timestamps [M:SS] to mark when key points occur
- Write in direct, factual sentences
- Keep speaker attribution only when it matters (who committed to what)
- Target ~30% of the original length

TRANSCRIPT SECTION ({start} to {end}):
{text}

CONDENSED VERSION:"""


def condense_chunk(
    segments: list[TranscriptSegment],
    model: str = "qwen2.5:7b",
    ollama_url: str = "http://localhost:11434",
) -> str:
    """Condense a chunk of segments via Ollama."""
    text = _build_chunk_text(segments)
    if not text:
        return ""

    start_ts = _fmt_ts(segments[0].start)
    end_ts = _fmt_ts(segments[-1].end)

    prompt = CONDENSE_PROMPT.format(
        start=start_ts,
        end=end_ts,
        text=text,
    )

    try:
        client = ollama.Client(host=ollama_url)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1, "num_ctx": 16384},
        )
        condensed = response.message.content.strip()
        log.info(
            f"Condensed chunk [{start_ts}-{end_ts}]: "
            f"{len(text)} → {len(condensed)} chars "
            f"({len(condensed) / max(len(text), 1):.0%})"
        )
        return condensed
    except Exception as e:
        log.warning(f"Condense failed for chunk [{start_ts}-{end_ts}]: {e}")
        return text


def condition_transcript(
    segments: list[TranscriptSegment],
    duration_seconds: float,
    model: str = "qwen2.5:7b",
    ollama_url: str = "http://localhost:11434",
    condense_threshold_minutes: int = 10,
    chunk_minutes: int = 10,
) -> tuple[list[TranscriptSegment], str]:
    """
    Full conditioning pipeline.

    Returns:
        (cleaned_segments, condensed_text)
        - cleaned_segments: filler-cleaned segments (for raw transcript in .md)
        - condensed_text: LLM-condensed text (for extraction prompt)
          For short recordings, this is just the cleaned timestamped transcript.
    """
    # Stage 1: Clean fillers from all segments
    cleaned = clean_segments(segments)
    log.info(
        f"Filler cleaning: {len(segments)} → {len(cleaned)} segments "
        f"(dropped {len(segments) - len(cleaned)} empty)"
    )

    duration_minutes = duration_seconds / 60

    # Stage 2: Short recordings — just return cleaned text, no LLM condensing
    if duration_minutes < condense_threshold_minutes:
        condensed_text = _build_chunk_text(cleaned)
        log.info(f"Short recording ({duration_minutes:.0f}min) — skipping LLM condense")
        return cleaned, condensed_text

    # Stage 3: Long recordings — chunk and condense
    chunks = _chunk_segments(cleaned, chunk_minutes)
    log.info(
        f"Long recording ({duration_minutes:.0f}min) — "
        f"condensing {len(chunks)} chunks of ~{chunk_minutes}min"
    )

    condensed_parts = []
    for i, chunk in enumerate(chunks):
        start_ts = _fmt_ts(chunk[0].start)
        end_ts = _fmt_ts(chunk[-1].end)
        header = f"--- [{start_ts} – {end_ts}] ---"
        condensed = condense_chunk(chunk, model, ollama_url)
        condensed_parts.append(f"{header}\n{condensed}")

    condensed_text = "\n\n".join(condensed_parts)
    raw_len = sum(len(seg.text) for seg in segments)
    log.info(
        f"Conditioning complete: {raw_len} → {len(condensed_text)} chars "
        f"({len(condensed_text) / max(raw_len, 1):.0%})"
    )

    return cleaned, condensed_text
