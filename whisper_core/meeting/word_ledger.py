"""Незмінний послівний реєстр ASR для нарад.

Ledger є джерелом істини для тексту: diarization у наступному slice додає
SpeakerAssignment як окремий шар і не переписує жодного WordRecord.
"""
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


LEDGER_SCHEMA = 1


class ImmutableLedgerError(RuntimeError):
    """Спроба змінити вже опублікований word ledger."""


@dataclass(frozen=True)
class WordRecord:
    word_id: str
    track: str
    start_sample: int
    end_sample: int
    text: str
    source: str
    asr_provenance: Mapping

    def __post_init__(self):
        if not self.word_id or not self.track:
            raise ValueError("word_id і track обов’язкові")
        if self.start_sample < 0 or self.end_sample < self.start_sample:
            raise ValueError("некоректний sample range слова")
        if not self.text:
            raise ValueError("порожнє слово не входить у ledger")
        object.__setattr__(
            self, "asr_provenance",
            MappingProxyType(dict(self.asr_provenance or {})),
        )

    def to_dict(self) -> dict:
        return {
            "word_id": self.word_id,
            "track": self.track,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "text": self.text,
            "source": self.source,
            "asr_provenance": dict(self.asr_provenance),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WordRecord":
        return cls(
            word_id=str(payload["word_id"]),
            track=str(payload["track"]),
            start_sample=int(payload["start_sample"]),
            end_sample=int(payload["end_sample"]),
            text=str(payload["text"]),
            source=str(payload["source"]),
            asr_provenance=payload.get("asr_provenance", {}),
        )


@dataclass(frozen=True)
class SpeakerCandidate:
    speaker_id: str
    overlap_samples: int


@dataclass(frozen=True)
class SpeakerAssignment:
    """Замінний derived-layer для Slice 3; не є частиною WordRecord."""

    word_id: str
    speaker_id: "str | None"
    assignment_reason: str
    candidates: tuple[SpeakerCandidate, ...]
    overlap_suspected: bool
    confidence_class: str


def word_id_counter(words) -> Counter:
    return Counter(word.word_id for word in words)


def validate_word_ledger(words) -> list[WordRecord]:
    records = list(words)
    counts = word_id_counter(records)
    duplicates = sorted(word_id for word_id, count in counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"word_id має траплятися рівно раз: {duplicates[0]}")
    return records


def _render_jsonl(words) -> str:
    records = validate_word_ledger(words)
    return "".join(
        json.dumps(word.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        for word in records
    )


def write_word_ledger(path, words) -> Path:
    """Атомарно створити ledger; наявний можна лише ідемпотентно підтвердити."""
    path = Path(path)
    rendered = _render_jsonl(words)
    if path.exists():
        if path.read_text(encoding="utf-8") == rendered:
            return path
        raise ImmutableLedgerError(
            f"ledger уже існує й не може бути змінений: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def read_word_ledger(path) -> list[WordRecord]:
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(WordRecord.from_dict(json.loads(line)))
    return validate_word_ledger(records)


def assert_same_word_ids(before, after) -> None:
    """Гейт zero-loss/zero-duplication для derived views та export."""
    if word_id_counter(before) != Counter(after):
        raise ValueError("Counter(word_id) змінився під час побудови export")


__all__ = [
    "LEDGER_SCHEMA", "ImmutableLedgerError", "WordRecord",
    "SpeakerCandidate", "SpeakerAssignment", "word_id_counter", "validate_word_ledger",
    "write_word_ledger", "read_word_ledger", "assert_same_word_ids",
]
