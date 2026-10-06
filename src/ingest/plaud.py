#!/usr/bin/env python3
"""
Plaud Recording Processor
Transcribes mp3s with faster-whisper, extracts actions with Ollama, writes markdown.

Usage:
    python -m src.ingest.plaud /path/to/recording.mp3
    python -m src.ingest.plaud /path/to/dir/ --only-new /path/to/.plaud-manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import ollama

from ..config import load_settings as _load_config, vocabulary
from ..infra import audit
from . import date_resolver
from .conditioner import condition_transcript
from .schema import ActionItem, ProcessedRecording, TranscriptSegment
from .validator import validate_items

log = logging.getLogger("plaud-ingest")

# Lazy-loaded whisper model (heavy import + model download)
_whisper_model = None

EXTRACTION_PROMPT_TEMPLATE = """\
You are analyzing a timestamped transcript from a voice recording. Extract structured information.

The transcript has timestamps like [0:00] or [1:23] at the start of each segment.

Respond with ONLY valid JSON matching this schema:
{
  "summary": "1-3 sentence summary of the recording",
  "participants": ["list of speaker names mentioned, or 'Unknown' if unclear"],
  "action_items": [
    {
      "description": "self-contained instruction: verb + concrete object + enough context to act WITHOUT the recording",
      "assignee": "self | other | unknown",
      "urgency": "low | medium | high",
      "confidence": 0.85,
      "project": "project name or null",
      "workstream": "workstream name or null",
      "due_raw": "exact words from transcript about timing, or null",
      "item_type": "task | decision | info | question",
      "source_quote": "exact words from the transcript proving this item exists",
      "source_time_start": "M:SS timestamp where this item starts",
      "source_time_end": "M:SS timestamp where this item ends",
      "tags": ["controlled-vocabulary keywords only"]
    }
  ],
  "key_decisions": ["decisions that were made"]
}

Known projects/workstreams (use these exact values when context matches):
  {projects}

Rules:
- Only extract REAL action items — a concrete task someone committed to doing.
- Each description MUST be self-contained and specific: a clear VERB + a concrete OBJECT + enough
  context (who / what / which system / which document / which date) to act on it WITHOUT replaying
  the recording. Reuse the actual names, numbers, systems and dates said in the transcript.
    GOOD: "Email Sam the signed contract before Friday"
    BAD (too vague — SKIP entirely): "send an email", "follow up", "check in", "sort it out"
- SKIP vague or generic fragments. If you cannot make an item self-contained from the transcript,
  DO NOT emit it — an empty list is correct when nothing concrete was committed
- If no action items exist, return an empty list
- Keep the summary factual, not interpretive
- If you can't determine participants, use ["Unknown"]
- confidence: 0.0 to 1.0 — how certain you are this is a real, actionable task
  (1.0 = explicit commitment like "I will do X by Friday", 0.5 = vague mention)
- assignee: "self" if the recorder, "other" if delegated to a named person, "unknown" if unclear
- project/workstream: infer from context if a known project is discussed, otherwise null
- due_raw: copy the EXACT words about timing from the transcript (e.g. "by Friday", "next week",
  "before the end of the month", "February 20th"). Do NOT convert to dates. null if no deadline.
- item_type: "task" for actionable work, "decision" for agreed decisions, "info" for FYI items,
  "question" for open questions needing answers
- source_quote: MANDATORY — copy a short phrase (5-30 words) from the transcript that proves
  this item exists. Must be findable in the transcript text.
- source_time_start / source_time_end: timestamps from the transcript where this item is discussed
- tags: 0-3 keywords, ONLY from this controlled vocabulary (lowercase, exact match):
    {tags}
  Do NOT invent tags. NEVER put person names, company names, or acronyms in tags. Empty list if none apply.

TRANSCRIPT:
"""


def build_extraction_prompt(settings: dict | None = None) -> str:
    """Fill the prompt's vocabulary lists from settings `vocabulary`."""
    vocab = vocabulary(settings)
    domain_tags = [t for tags in vocab["tag_domains"].values() for t in tags]
    tags = list(dict.fromkeys(vocab["projects"] + domain_tags))
    return EXTRACTION_PROMPT_TEMPLATE.replace(
        "{projects}", ", ".join(vocab["projects"]) or "(none configured)"
    ).replace("{tags}", ", ".join(tags))


def load_settings() -> dict:
    return _load_config()


def _resolve_codex(binary: str | None) -> str | None:
    return shutil.which(binary or "codex")


def _resolve_output_dir(settings: dict) -> Path:
    plaud_cfg = settings.get("ingest", {}).get("plaud", {})
    output_dir = Path(plaud_cfg.get("output_dir", "~/tasks")).expanduser()
    return output_dir


def _ensure_output_dirs(output_dir: Path) -> dict[str, Path]:
    dirs = {
        "recordings": output_dir / "recordings",
        "transcripts": output_dir / "transcripts",
        "summaries": output_dir / "summaries",
        "logs": output_dir / "logs",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def get_whisper_model(model_name: str = "large-v3-turbo"):
    global _whisper_model
    if _whisper_model is None:
        log.info(f"Loading whisper model: {model_name}")
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _whisper_model


def transcribe(
    audio_path: str | Path, model_name: str
) -> tuple[list[TranscriptSegment], float, str, str]:
    model = get_whisper_model(model_name)

    log.info(f"Transcribing: {Path(audio_path).name}")
    segments_gen, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )

    language = info.language or "unknown"
    lang_prob = info.language_probability or 0.0
    log.info(f"Detected language: {language} ({lang_prob:.0%})")

    if lang_prob < 0.7:
        log.warning(f"Low language confidence ({lang_prob:.0%}) — auto-detection may be unreliable")

    segments = []
    full_parts = []
    for seg in segments_gen:
        segments.append(
            TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
            )
        )
        full_parts.append(seg.text)

    full_text = "".join(full_parts)
    duration = segments[-1].end if segments else 0.0
    return segments, duration, full_text, language


def _build_timestamped_transcript(segments: list[TranscriptSegment]) -> str:
    lines = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        m, s = divmod(int(seg.start), 60)
        ts = f"{m}:{s:02d}"
        lines.append(f"[{ts}] {text}")
    return "\n".join(lines)


def _extract_via_codex(
    prompt: str,
    codex_binary: str | None = None,
    codex_model: str = "gpt-5.2-codex",
) -> dict | None:
    codex_path = _resolve_codex(codex_binary)
    if not codex_path:
        log.warning("Codex binary not found on PATH — skipping Codex extraction")
        return None
    log.info(f"Attempting extraction via Codex ({codex_model})")
    try:
        result = subprocess.run(
            [
                codex_path,
                "exec",
                "--full-auto",
                "--skip-git-repo-check",
                "-m",
                codex_model,
                f"You are a JSON extraction engine. {prompt}\n\nReturn ONLY valid JSON. No explanation, no markdown fences.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            log.warning(f"Codex exec failed (rc={result.returncode}): {result.stderr[:200]}")
            return None

        output = result.stdout.strip()
        json_match = re.search(r"\{.*\}", output, re.DOTALL)
        if not json_match:
            log.warning(f"No JSON found in Codex output: {output[:200]}")
            return None

        parsed = json.loads(json_match.group())
        if "action_items" in parsed or "summary" in parsed:
            log.info("Codex extraction succeeded")
            return parsed

        log.warning("Codex returned JSON but missing expected keys")
        return None
    except subprocess.TimeoutExpired:
        log.warning("Codex extraction timed out (120s)")
        return None
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Codex extraction failed: {e}")
        return None


def _extract_via_ollama(
    prompt: str,
    model: str = "qwen2.5:7b",
    ollama_url: str = "http://localhost:11434",
) -> dict:
    log.info(f"Extracting actions via Ollama ({model})")
    client = ollama.Client(host=ollama_url)
    try:
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={"temperature": 0.1, "num_ctx": 16384},
        )
        content = response.message.content.strip()
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        log.warning(f"Failed to parse Ollama response as JSON: {content[:200]}")
        return {"summary": "", "action_items": [], "participants": [], "key_decisions": []}
    except Exception as e:
        log.error(f"Ollama extraction failed: {e}")
        return {"summary": "", "action_items": [], "participants": [], "key_decisions": []}


def extract_actions(
    transcript_text: str,
    model: str = "qwen2.5:7b",
    ollama_url: str = "http://localhost:11434",
    segments: list[TranscriptSegment] | None = None,
    conditioned_text: str | None = None,
    codex_enabled: bool = False,
    codex_binary: str | None = None,
    codex_model: str = "gpt-5.2-codex",
    settings: dict | None = None,
) -> dict:
    # Prefer conditioned (cleaned + condensed) text over raw
    if conditioned_text:
        timestamped = conditioned_text
    elif segments:
        timestamped = _build_timestamped_transcript(segments)
    else:
        timestamped = transcript_text

    if len(timestamped) > 24000:
        log.warning(
            f"Transcript truncated for extraction: {len(timestamped)} → 24000 chars "
            f"({len(timestamped) - 24000} chars lost)"
        )
    prompt = build_extraction_prompt(settings) + timestamped[:24000]

    if codex_enabled:
        result = _extract_via_codex(prompt, codex_binary, codex_model)
        if result is not None:
            return result
        log.info("Codex failed, falling back to Ollama")

    return _extract_via_ollama(prompt, model, ollama_url)


def process_recording(
    mp3_path: Path,
    settings: dict | None = None,
    output_dirs: dict[str, Path] | None = None,
) -> ProcessedRecording:
    if settings is None:
        settings = load_settings()

    plaud_cfg = settings.get("ingest", {}).get("plaud", {})
    whisper_model = plaud_cfg.get("whisper_model", "large-v3-turbo")
    ollama_model = plaud_cfg.get("ollama_model", "qwen2.5:7b")
    ollama_url = plaud_cfg.get("ollama_url", "http://localhost:11434")
    codex_enabled = plaud_cfg.get("codex_enabled", False)
    codex_binary = plaud_cfg.get("codex_binary", "codex")
    codex_model = plaud_cfg.get("codex_model", "gpt-5.2-codex")

    stem = mp3_path.stem
    date = stem[:10] if len(stem) >= 10 and stem[4] == "-" else "unknown"
    recording_id = hashlib.sha256(mp3_path.name.encode()).hexdigest()[:12]

    # Stage 1: Transcribe
    segments, duration, full_text, language = transcribe(mp3_path, whisper_model)
    transcript_checksum = hashlib.sha256(full_text.encode()).hexdigest()[:16]

    # Save transcript to output directory
    if output_dirs and "transcripts" in output_dirs:
        try:
            transcript_data = {
                "recording": mp3_path.name,
                "recording_id": recording_id,
                "duration_seconds": round(duration, 1),
                "language": language,
                "checksum": transcript_checksum,
                "full_text": full_text,
                "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
            }
            transcript_file = output_dirs["transcripts"] / f"{stem}.json"
            transcript_file.write_text(json.dumps(transcript_data, indent=2))
            log.info(f"Transcript saved: {transcript_file}")
        except Exception as e:
            log.warning(f"Failed to save transcript: {e}")

    # Copy recording to output directory for local reference
    if output_dirs and "recordings" in output_dirs:
        try:
            dest = output_dirs["recordings"] / mp3_path.name
            if not dest.exists():
                shutil.copy2(mp3_path, dest)
                log.info(f"Recording copied: {dest}")
        except Exception as e:
            log.warning(f"Failed to copy recording: {e}")

    audit.append(
        {
            "stage": "transcribe",
            "recording": mp3_path.name,
            "recording_id": recording_id,
            "duration_seconds": round(duration, 1),
            "language": language,
            "segments": len(segments),
            "chars": len(full_text),
            "checksum": transcript_checksum,
        }
    )

    # Stage 1.5: Condition transcript (clean fillers + condense long recordings)
    cleaned_segments, condensed_text = condition_transcript(
        segments,
        duration,
        model=ollama_model,
        ollama_url=ollama_url,
        condense_threshold_minutes=10,
        chunk_minutes=10,
    )
    audit.append(
        {
            "stage": "condition",
            "recording_id": recording_id,
            "raw_segments": len(segments),
            "cleaned_segments": len(cleaned_segments),
            "raw_chars": len(full_text),
            "condensed_chars": len(condensed_text),
        }
    )

    # Use cleaned segments for the .md transcript display
    segments = cleaned_segments

    # Stage 2: LLM extraction — English only (non-English transcripts are archived but not parsed)
    if language != "en":
        log.info(f"Skipping action extraction for non-English recording ({language})")
        extracted = {
            "summary": f"[{language}] {full_text[:200]}",
            "action_items": [],
            "participants": [],
            "key_decisions": [],
        }
    else:
        extracted = extract_actions(
            full_text,
            model=ollama_model,
            ollama_url=ollama_url,
            conditioned_text=condensed_text,
            codex_enabled=codex_enabled,
            codex_binary=codex_binary,
            codex_model=codex_model,
            settings=settings,
        )

    raw_items = extracted.get("action_items", [])
    audit.append(
        {
            "stage": "extract",
            "recording_id": recording_id,
            "model": ollama_model,
            "raw_count": len(raw_items),
            "summary_len": len(extracted.get("summary", "")),
        }
    )

    # Stage 3: Build ActionItems with deterministic date resolution
    action_items = []
    for a in raw_items:
        if not a.get("description"):
            continue

        due_raw = a.get("due_raw")
        resolved_date, was_inferred = date_resolver.resolve(due_raw, date)
        due_confidence = date_resolver.confidence_for(due_raw)

        action_items.append(
            ActionItem(
                description=a.get("description", ""),
                assignee=a.get("assignee", "self"),
                urgency=a.get("urgency", "medium"),
                confidence=a.get("confidence", 0.0),
                project=a.get("project"),
                workstream=a.get("workstream"),
                due_date=resolved_date,
                due_raw=due_raw,
                due_confidence=due_confidence,
                inferred_due=was_inferred,
                tags=a.get("tags", []),
                item_type=a.get("item_type", "task"),
                source_quote=a.get("source_quote", ""),
                source_time_start=a.get("source_time_start", ""),
                source_time_end=a.get("source_time_end", ""),
            )
        )

    # Stage 4: Validate (source quote verification + confirmation rules)
    accepted, rejected = validate_items(action_items, full_text)

    # Solo speaker: promote "unknown" assignee → "self" with confirmation
    participants = extracted.get("participants", [])
    solo = len(participants) <= 1 or participants == ["Unknown"]
    if solo:
        for item in accepted:
            if item.assignee == "unknown":
                item.assignee = "self"
                item.requires_confirmation = True
                item.confirmation_question = (
                    item.confirmation_question
                    or f"Solo recording, assumed self: '{item.description}' — correct?"
                )

    audit.append(
        {
            "stage": "validate",
            "recording_id": recording_id,
            "input_count": len(action_items),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "rejected_reasons": [r["reason"] for r in rejected],
            "solo_speaker": solo,
        }
    )

    return ProcessedRecording(
        source_file=str(mp3_path),
        date=date,
        duration_seconds=duration,
        transcript=segments,
        full_text=full_text,
        summary=extracted.get("summary", ""),
        action_items=accepted,
        key_decisions=extracted.get("key_decisions", []),
        participants=participants,
    )


def write_markdown(recording: ProcessedRecording, output_path: Path) -> None:
    output_path.write_text(recording.to_markdown(), encoding="utf-8")
    log.info(f"Wrote: {output_path}")


def get_processed_ids(manifest_path: Path) -> set[str]:
    """Get file IDs already processed (have .md alongside mp3 on disk or in manifest)."""
    if not manifest_path.exists():
        return set()
    try:
        manifest = json.loads(manifest_path.read_text())
        return {fid for fid, info in manifest.items() if info.get("processed", False)}
    except (json.JSONDecodeError, KeyError):
        return set()


def mark_processed(manifest_path: Path, file_id: str) -> None:
    """Mark a file as processed in the manifest."""
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
        if file_id in manifest:
            manifest[file_id]["processed"] = True
            manifest_path.write_text(json.dumps(manifest, indent=2))
    except (json.JSONDecodeError, KeyError):
        pass


def find_file_id(manifest_path: Path, filename: str) -> str | None:
    """Find the manifest file ID for a given filename."""
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
        for fid, info in manifest.items():
            if info.get("filename") == filename:
                return fid
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_to_memory(recording: ProcessedRecording, source_file: str) -> None:
    """Save transcript summary + key decisions to unified memory."""
    from ..infra.memory import get_plaud_memory_client

    mem = get_plaud_memory_client()
    if not mem:
        log.debug("Memory client unavailable, skipping memory save")
        return

    parts = [f"Plaud recording: {Path(source_file).stem}"]
    parts.append(f"Duration: {recording.duration_seconds:.0f}s")
    if recording.participants:
        parts.append(f"Participants: {', '.join(recording.participants)}")
    if recording.summary:
        parts.append(f"Summary: {recording.summary}")
    if recording.key_decisions:
        parts.append("Key decisions: " + "; ".join(recording.key_decisions))
    if recording.action_items:
        actions = [a.description for a in recording.action_items[:5]]
        parts.append("Actions: " + "; ".join(actions))

    content = "\n".join(parts)

    tags = ["plaud", "audio", "transcript"]
    if recording.participants and len(recording.participants) > 1:
        tags.append("meeting")

    entry_id = mem.save(
        content=content,
        category="archive",
        tags=tags,
        visibility="shared",
        namespace="plaud",
        confidence=0.85,
        source="agent_observation",
    )
    if entry_id:
        log.info(f"Saved to memory: {entry_id}")
    else:
        log.warning("Memory save returned no entry_id")


def main():
    parser = argparse.ArgumentParser(description="Process Plaud recordings")
    parser.add_argument("path", help="MP3 file or directory of MP3s")
    parser.add_argument("--only-new", metavar="MANIFEST", help="Skip already-processed files")
    parser.add_argument(
        "--create-tasks", action="store_true", help="Create TaskWarrior tasks from action items"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    try:
        audit.require_key()
    except audit.AuditKeyMissing as e:
        log.error(str(e))
        sys.exit(2)

    settings = load_settings()
    target = Path(args.path).expanduser()

    # Set up structured output directories
    output_dir = _resolve_output_dir(settings)
    output_dirs = _ensure_output_dirs(output_dir)
    log.info(f"Output directory: {output_dir}")

    if target.is_file() and target.suffix == ".mp3":
        mp3s = [target]
    elif target.is_dir():
        mp3s = sorted(target.glob("*.mp3"))
    else:
        log.error(f"Not a valid mp3 file or directory: {target}")
        sys.exit(1)

    if not mp3s:
        log.info("No mp3 files found")
        print("PROCESS_RESULT:" + json.dumps({"processed": 0, "actions": 0, "summary": ""}))
        return

    # Filter already-processed if manifest provided
    manifest_path = Path(args.only_new) if args.only_new else None
    if manifest_path:
        processed_ids = get_processed_ids(manifest_path)
        original_count = len(mp3s)
        mp3s = [mp3 for mp3 in mp3s if find_file_id(manifest_path, mp3.name) not in processed_ids]
        skipped = original_count - len(mp3s)
        if skipped:
            log.info(f"Skipping {skipped} already-processed recordings")

    if not mp3s:
        log.info("All recordings already processed")
        print("PROCESS_RESULT:" + json.dumps({"processed": 0, "actions": 0, "summary": ""}))
        return

    total_processed = 0
    total_actions = 0
    total_tasks_created = 0
    last_summary = ""

    for mp3 in mp3s:
        # Per-recording log file
        rec_log_file = output_dirs["logs"] / f"{mp3.stem}.log"
        rec_handler = logging.FileHandler(rec_log_file)
        rec_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
        logging.getLogger().addHandler(rec_handler)

        recording_id = hashlib.sha256(mp3.name.encode()).hexdigest()[:12]
        try:
            recording = process_recording(mp3, settings, output_dirs=output_dirs)
            md_path = mp3.with_suffix(".md")
            write_markdown(recording, md_path)

            save_to_memory(recording, str(mp3))

            # Mark as processed in manifest
            if manifest_path:
                fid = find_file_id(manifest_path, mp3.name)
                if fid:
                    mark_processed(manifest_path, fid)

            # Create TaskWarrior tasks if requested (with idempotency)
            if args.create_tasks and recording.action_items:
                from ..actions.taskwarrior import create_tasks
                from ..infra.idem_store import IdemStore

                idem = IdemStore()
                tw_results = create_tasks(
                    recording,
                    idem_store=idem,
                    summaries_dir=output_dirs.get("summaries"),
                )
                tasks_ok = sum(1 for r in tw_results if r["success"])
                tasks_deduped = sum(1 for r in tw_results if r.get("deduped"))
                total_tasks_created += tasks_ok
                idem.close()

                audit.append(
                    {
                        "stage": "create_tasks",
                        "recording_id": recording_id,
                        "created": tasks_ok,
                        "deduped": tasks_deduped,
                        "total_items": len(recording.action_items),
                    }
                )

                if tasks_ok:
                    log.info(f"Created {tasks_ok} TaskWarrior task(s) from {mp3.name}")
                if tasks_deduped:
                    log.info(f"Skipped {tasks_deduped} duplicate(s) from {mp3.name}")

            # Post action items to the task-queue webhook (dual-write alongside TW)
            if recording.action_items:
                from ..actions.task_queue import post_to_queue
                from ..infra.idem_store import IdemStore as TQIdemStore

                tq_idem = TQIdemStore()
                tq_results = post_to_queue(
                    recording,
                    recording.action_items,
                    idem_store=tq_idem,
                    recording_id=recording_id,
                )
                tq_idem.close()
                tq_ok = sum(1 for r in tq_results if r["success"])
                if tq_ok:
                    log.info(f"Queued {tq_ok} item(s) to the task queue from {mp3.name}")
                audit.append(
                    {
                        "stage": "task_queue",
                        "queued": tq_ok,
                        "failed": len(tq_results) - tq_ok,
                        "total_items": len(recording.action_items),
                    }
                )

            total_processed += 1
            total_actions += len(recording.action_items)
            last_summary = recording.summary
            log.info(
                f"Done: {mp3.name} "
                f"({recording.duration_seconds:.0f}s, "
                f"{len(recording.action_items)} actions)"
            )
        except Exception as e:
            log.error(f"Failed to process {mp3.name}: {e}")
        finally:
            logging.getLogger().removeHandler(rec_handler)
            rec_handler.close()

    result = {
        "processed": total_processed,
        "actions": total_actions,
        "tasks_created": total_tasks_created,
        "summary": last_summary[:200],
    }
    print("PROCESS_RESULT:" + json.dumps(result))


if __name__ == "__main__":
    main()
