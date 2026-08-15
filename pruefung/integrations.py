from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import PruefungError, canonical_hash, read_json, write_json


def json_safe(value: Any) -> Any:
    """Convert EDSL/API return values into durable JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return json_safe(value.to_dict())
        except Exception:
            pass
    return str(value)


def _find_value(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names and item:
                return item
        for item in value.values():
            found = _find_value(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, names)
            if found:
                return found
    return None


def publication_record(result: Any) -> dict[str, Any]:
    safe = json_safe(result)
    uuid = _find_value(safe, {"human_survey_uuid", "project_uuid", "uuid"})
    if not uuid:
        raise PruefungError(
            "Humanize created a project but returned no survey UUID; inspect the deployment receipt before retrying",
            code="network_error",
            exit_code=4,
        )
    uuid = str(uuid)
    respondent_url = _find_value(safe, {"respondent_url", "survey_url", "url"})
    admin_url = _find_value(safe, {"admin_url"})
    return {
        "project_uuid": uuid,
        "respondent_url": str(respondent_url or f"https://www.expectedparrot.com/respond/human-surveys/{uuid}"),
        "admin_url": str(admin_url or f"https://www.expectedparrot.com/home/human-surveys/{uuid}"),
        "raw": safe,
    }


def result_rows(result: Any) -> list[dict[str, Any]]:
    """Flatten an EDSL Results/ScenarioList or plain API payload."""
    if hasattr(result, "select"):
        try:
            return [json_safe(row) for row in result.select("answer.*", "scenario.*").to_dicts(remove_prefix=False)]
        except Exception:
            pass
    if hasattr(result, "to_list"):
        try:
            values = result.to_list()
            if isinstance(values, list):
                return [json_safe(row) for row in values]
        except Exception:
            pass
    safe = json_safe(result)
    if isinstance(safe, list):
        return [row if isinstance(row, dict) else {"value": row} for row in safe]
    if isinstance(safe, dict):
        for key in ("responses", "data", "items", "results"):
            if isinstance(safe.get(key), list):
                return [row if isinstance(row, dict) else {"value": row} for row in safe[key]]
        return [safe]
    return []


def normalize_response(row: dict[str, Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    nested_answers = row.get("answers") or row.get("answer")
    if isinstance(nested_answers, dict):
        answers.update(nested_answers)
    for key, value in row.items():
        if key.startswith("answer."):
            answers[key.split(".", 1)[1]] = value
        elif key.startswith("scenario."):
            metadata[key.split(".", 1)[1]] = value
    email = (
        str(
            answers.get("pruefung_respondent_email")
            or row.get("email")
            or row.get("identifier")
            or metadata.get("email")
            or ""
        )
        .strip()
        .lower()
    )
    response_id = str(
        row.get("response_id")
        or row.get("id")
        or metadata.get("response_id")
        or metadata.get("respondent_id")
        or canonical_hash({"email": email, "answers": answers})
    )
    return {
        "response_id": response_id,
        "email": email,
        "name": str(row.get("name") or metadata.get("name") or ""),
        "answers": answers,
        "submitted_at": row.get("submitted_at") or metadata.get("submitted_at") or metadata.get("created_at"),
    }


def merge_responses(path: Path, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = read_json(path, [])
    by_id = {str(row.get("response_id") or canonical_hash(row)): row for row in existing}
    for row in incoming:
        by_id[row["response_id"]] = row
    merged = sorted(by_id.values(), key=lambda row: (str(row.get("submitted_at") or ""), row["response_id"]))
    write_json(path, merged)
    return merged


def get_coop():
    from edsl import Coop

    return Coop()


def sync_responses(exam_dir: Path, exam: dict[str, Any], coop: Any | None = None) -> list[dict[str, Any]]:
    uuid = exam.get("published", {}).get("project_uuid")
    if not uuid:
        raise PruefungError("deployed exam has no Humanize project UUID", code="network_error", exit_code=4)
    try:
        remote = (coop or get_coop()).get_human_survey_responses(uuid)
        normalized = [normalize_response(row) for row in result_rows(remote)]
    except PruefungError:
        raise
    except Exception as exc:
        raise PruefungError(f"could not fetch Humanize responses: {exc}", code="network_error", exit_code=4) from exc
    return merge_responses(exam_dir / "responses.json", normalized)


def normalize_answer(value: Any, ptype: str, options: list[str]) -> Any:
    if isinstance(value, dict):
        value = value.get("answer", value.get("value", value))
    if ptype == "true_false":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"yes", "true", "1"}:
            return True
        if lowered in {"no", "false", "0"}:
            return False
        return value
    if ptype == "mcq":
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        for index, option in enumerate(options):
            if text == option or text.lower() == option.lower():
                return index
        return value
    if ptype == "checkbox":
        values = value if isinstance(value, list) else [value]
        normalized = [normalize_answer(item, "mcq", options) for item in values]
        return sorted(item for item in normalized if isinstance(item, int))
    return value
