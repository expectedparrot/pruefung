from __future__ import annotations

from typing import Any

from .core import ValidationError

TYPE_MAP = {
    "mcq": "QuestionMultipleChoice",
    "true_false": "QuestionYesNo",
    "checkbox": "QuestionCheckBox",
    "free_text": "QuestionFreeText",
}


def edsl_module():
    try:
        import edsl
    except ImportError as exc:
        raise ValidationError("EDSL is required; install the `edsl` package") from exc
    return edsl


def make_question(ptype: str, name: str, text: str, options: list[str]) -> Any:
    edsl = edsl_module()
    if ptype not in TYPE_MAP:
        raise ValidationError(f"unsupported question type: {ptype}")
    kwargs: dict[str, Any] = {"question_name": name, "question_text": text}
    if ptype in {"mcq", "checkbox"}:
        kwargs["question_options"] = options
    try:
        return getattr(edsl, TYPE_MAP[ptype])(**kwargs)
    except Exception as exc:
        raise ValidationError(str(exc)) from exc


def restore_question(data: dict[str, Any]) -> Any:
    edsl = edsl_module()
    qtype = data.get("question_type")
    names = {
        "multiple_choice": "QuestionMultipleChoice",
        "checkbox": "QuestionCheckBox",
        "yes_no": "QuestionYesNo",
        "free_text": "QuestionFreeText",
    }
    try:
        cls = getattr(edsl, names[qtype])
        return cls.from_dict(data)
    except Exception as exc:
        raise ValidationError(f"invalid EDSL question: {exc}") from exc


def make_survey(question_dicts: list[dict[str, Any]], instructions: str | None = None) -> Any:
    edsl = edsl_module()
    questions = [restore_question(item) for item in question_dicts]
    survey = edsl.Survey(questions)
    if instructions:
        try:
            survey = edsl.Survey([edsl.Instruction(name="exam_instructions", text=instructions), *questions])
        except Exception:
            try:
                survey.add_instruction(edsl.Instruction(name="exam_instructions", text=instructions))
            except Exception:
                pass
    return survey
