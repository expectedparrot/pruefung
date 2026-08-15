from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PruefungError(Exception):
    def __init__(self, message: str, *, code: str = "error", exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class ValidationError(PruefungError):
    def __init__(self, message: str):
        super().__init__(message, code="validation_error", exit_code=2)


class ConflictError(PruefungError):
    def __init__(self, message: str):
        super().__init__(message, code="state_conflict", exit_code=3)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def find_root(start: Path | None = None) -> Path:
    path = (start or Path.cwd()).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".pruefung" / "config.json").is_file():
            return candidate
    raise PruefungError("not inside a pruefung project; run `pruefung init --course ...`", code="not_initialized")


def state(root: Path) -> Path:
    return root / ".pruefung"


def question_hash(question: dict[str, Any]) -> str:
    meta = question.get("meta", {})
    return canonical_hash(
        {
            "edsl": question.get("edsl"),
            "answer": meta.get("answer"),
            "rubric": meta.get("rubric"),
            "points": meta.get("points"),
        }
    )


def validate_id(value: str, pattern: str, label: str) -> None:
    if re.fullmatch(pattern, value) is None:
        raise ValidationError(f"invalid {label}: {value}")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "source"


def question_files(root: Path) -> list[Path]:
    return sorted((state(root) / "questions").glob("q*.json"))


def concept_files(root: Path) -> list[Path]:
    return sorted((state(root) / "concepts").glob("*.json"))


def load_question(root: Path, qid: str) -> dict[str, Any]:
    path = state(root) / "questions" / f"{qid}.json"
    result = read_json(path)
    if result is None:
        raise ValidationError(f"unknown question: {qid}")
    return result


def load_exam(root: Path, exam_id: str) -> tuple[Path, dict[str, Any]]:
    path = state(root) / "exams" / exam_id / "exam.json"
    result = read_json(path)
    if result is None:
        raise ValidationError(f"unknown exam: {exam_id}")
    return path, result


def parse_roster(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [str(x).strip().lower() for x in (reader.fieldnames or [])]
        if "email" not in reader.fieldnames:
            raise ValidationError("roster requires an email column")
        for line, raw in enumerate(reader, start=2):
            clean = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items()}
            email = clean.get("email", "").lower()
            if not email or "@" not in email:
                warnings.append(f"row {line}: malformed or missing email")
                continue
            if email in seen:
                warnings.append(f"row {line}: duplicate email {email} dropped")
                continue
            seen.add(email)
            rows.append({k: clean.get(k, "") for k in ("email", "name", "student_id")})
    return rows, warnings


def score_checkbox(expected: list[int], observed: list[int], points: float, mode: str) -> float:
    correct, selected = set(expected), set(observed)
    if mode == "none":
        return points if correct == selected else 0.0
    k = len(correct)
    if not k:
        return 0.0
    return max(0.0, min(points, (len(correct & selected) - len(selected - correct)) * points / k))
