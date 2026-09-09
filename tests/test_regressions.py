"""Regression coverage using synthetic course data and mocked network I/O."""
import copy
import json
from pathlib import Path

import pytest
from test_core import make_question_project, prepare_exam

from pruefung.cli import cli, deterministic_grade, grade_report_data, render_grade_report_html
from pruefung.core import ValidationError, read_json, write_json
from pruefung.integrations import normalize_answer


def run(runner, *args):
    result = runner.invoke(cli, list(args))
    assert result.exit_code == 0, (result.output, result.exception)
    return json.loads(result.output)["data"]


def envelope(task, payload):
    return {**{key: task[key] for key in ("task_id", "kind", "input_hashes", "created_at")}, "payload": payload}


@pytest.mark.parametrize("options,answer,index", [
    (["1", "2", "3", "4"], "2", 1),
    (["20", "10", "40", "30"], "10", 1),
    (["001", "002", "003", "004"], "002", 1),
    (["a", "b", "c", "d"], "2", 2),
    (["1", "2", "3", "4"], 2, 2),
])
def test_numeric_labels_and_explicit_indices(options, answer, index):
    assert normalize_answer(answer, "mcq", options) == index
    assert normalize_answer([answer], "checkbox", options) == [index]


def test_preview_dry_run_and_publication_preserve_instructions(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    run(runner, "exam", "create", "quiz", "--instructions", "Use only the supplied formula sheet.")
    run(runner, "exam", "add", "quiz", "q001")
    published = []

    def humanize(self, **kwargs):
        published.append(self.to_dict())
        return {"human_survey_uuid": "synthetic-survey"}

    monkeypatch.setattr("edsl.Survey.humanize", humanize)
    run(runner, "exam", "preview", "quiz", "--web")
    dry = run(runner, "exam", "deploy", "quiz", "--open", "--allow-draft", "--no-shuffle", "--dry-run")
    run(runner, "exam", "deploy", "quiz", "--open", "--allow-draft", "--no-shuffle")
    assert dry["survey"] == published[-1]
    for survey in [*published, dry["survey"]]:
        assert "Use only the supplied formula sheet." in json.dumps(survey)
        names = [q["question_name"] for q in survey["questions"] if "question_name" in q]
        assert names[0] == "pruefung_respondent_email"


def test_roster_filters_grades_but_open_enrollment_accepts_emails(tmp_path, monkeypatch):
    runner = prepare_exam(tmp_path, monkeypatch)
    monkeypatch.setattr("edsl.Survey.humanize", lambda self, **kw: {"human_survey_uuid": "synthetic"})
    run(runner, "exam", "deploy", "quiz-1", "--no-shuffle")
    responses = [
        {"response_id": "enrolled", "email": "student@example.edu", "answers": {"q1_topic": "beta"}},
        {"response_id": "outside", "email": "outside@example.edu", "answers": {"q1_topic": "beta"}},
        {"response_id": "anonymous", "email": "", "answers": {"q1_topic": "beta"}},
    ]
    monkeypatch.setattr("pruefung.cli.sync_responses", lambda *args: responses)
    result = run(runner, "grade", "quiz-1")
    assert result["unmatched_responses"] == 2
    gradebook = read_json(Path(result["gradebook"]))
    assert [s["email"] for s in gradebook["students"]] == ["student@example.edu"]
    exam = read_json(tmp_path / ".pruefung/exams/quiz-1/exam.json")
    exam["published"]["enrollment_mode"] = "open"
    opened = deterministic_grade(exam, responses, None, False)
    assert len(opened["students"]) == 2
    assert len(opened["unmatched_responses"]) == 1


@pytest.mark.parametrize("field,value", [
    ("points", "-3"), ("points", "0"), ("points", "nan"), ("points", "inf"),
    ("partial-credit", "anything"), ("partial-credit", "per_option"),
])
def test_invalid_question_edits_are_atomic(tmp_path, monkeypatch, field, value):
    runner = make_question_project(tmp_path, monkeypatch)
    path = tmp_path / ".pruefung/questions/q001.json"
    before = path.read_bytes()
    result = runner.invoke(cli, ["question", "set", "q001", field, "--", value])
    assert isinstance(result.exception, ValidationError)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("points", -1), ("points", float("nan")), ("answer", 9)])
def test_validation_and_deploy_reject_directly_edited_metadata(tmp_path, monkeypatch, field, value):
    runner = prepare_exam(tmp_path, monkeypatch)
    path = tmp_path / ".pruefung/questions/q001.json"
    question = read_json(path)
    question["meta"][field] = value
    write_json(path, question)
    assert isinstance(runner.invoke(cli, ["validate"]).exception, ValidationError)
    assert isinstance(runner.invoke(cli, ["exam", "deploy", "quiz-1", "--dry-run"]).exception, ValidationError)


@pytest.mark.parametrize("mutation", ["missing_model", "duplicate_model", "null_vote", "invalid_vote", "missing_answer", "short_notes"])
def test_qc_incomplete_panels_do_not_mutate_any_questions(tmp_path, monkeypatch, mutation):
    runner = make_question_project(tmp_path, monkeypatch)
    run(runner, "question", "add", "--type", "true_false", "--name", "second", "--text", "A fact?",
        "--answer", "true", "--points", "1", "--concept", "topic")
    run(runner, "qc", "make", "--models", "test")
    directory = tmp_path / ".pruefung/inference/qc_01"
    task = read_json(directory / "task.json")
    # A two-model task fixture; no inference is run.
    task["models"] = ["reviewer-a", "reviewer-b"]
    write_json(directory / "task.json", task)
    good = {"models": task["models"], "panel_answers": ["beta", "beta"],
            "blocking": [False, False], "notes": ["Clear", "Clear"]}
    bad = copy.deepcopy(good)
    bad["panel_answers"] = [True, True]
    if mutation == "missing_model":
        bad["models"] = ["reviewer-a"]
    elif mutation == "duplicate_model":
        bad["models"] = ["reviewer-a", "reviewer-a"]
    elif mutation == "null_vote":
        bad["blocking"][1] = None
    elif mutation == "invalid_vote":
        bad["blocking"][1] = "unavailable"
    elif mutation == "missing_answer":
        bad["panel_answers"][1] = None
    else:
        bad["notes"] = []
    write_json(directory / "result.json", envelope(task, {"questions": {"q001": good, "q002": bad}}))
    before = {p: p.read_bytes() for p in (tmp_path / ".pruefung/questions").glob("*.json")}
    assert isinstance(runner.invoke(cli, ["qc", "ingest", "qc_01"]).exception, ValidationError)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert read_json(directory / "task.json")["status"] == "pending"


def free_text_project(tmp_path, monkeypatch):
    runner = prepare_exam(tmp_path, monkeypatch)
    run(runner, "question", "add", "--type", "free_text", "--name", "explain", "--text", "Explain beta.",
        "--rubric", "4 for a complete explanation, 0 otherwise.", "--points", "4", "--concept", "topic")
    run(runner, "exam", "add", "quiz-1", "q002")
    monkeypatch.setattr("edsl.Survey.humanize", lambda self, **kw: {"human_survey_uuid": "synthetic"})
    run(runner, "exam", "deploy", "quiz-1", "--allow-draft", "--no-shuffle")
    responses = [{"email": "student@example.edu", "answers": {"q1_topic": "beta", "q2_topic": "An explanation."}}]

    class Coop:
        def get_human_survey_responses(self, uuid):
            return responses

    monkeypatch.setattr("pruefung.integrations.get_coop", Coop)
    run(runner, "grade", "quiz-1")
    made = run(runner, "grade-make", "quiz-1", "--models", "test")
    directory = tmp_path / ".pruefung/inference" / made["task_id"]
    task = read_json(directory / "task.json")
    task["models"] = ["reviewer-a", "reviewer-b"]
    write_json(directory / "task.json", task)
    rows = [{"input": {"answer_id": "student@example.edu:q2_topic"}, "model": model, "score": score,
             "score_comment": f"Rubric assessment: {score}"}
            for model, score in zip(task["models"], [0, 4])]
    return runner, directory, task, rows


@pytest.mark.parametrize("mutation", ["missing_model", "duplicate_model", "missing_answer", "unknown_answer", "nan", "infinity", "range"])
def test_rubric_result_requires_complete_valid_panel(tmp_path, monkeypatch, mutation):
    runner, directory, task, rows = free_text_project(tmp_path, monkeypatch)
    if mutation == "missing_model":
        rows.pop()
    elif mutation == "duplicate_model":
        rows[1]["model"] = rows[0]["model"]
    elif mutation == "missing_answer":
        rows = []
    elif mutation == "unknown_answer":
        rows[1]["input"]["answer_id"] = "someone@example.edu:q2_topic"
    else:
        rows[1]["score"] = {"nan": float("nan"), "infinity": float("inf"), "range": 5}[mutation]
    write_json(directory / "result.json", envelope(task, {"rows": rows}))
    path = tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json"
    before = path.read_bytes()
    assert isinstance(runner.invoke(cli, ["grade-ingest", "quiz-1", task["task_id"]]).exception, ValidationError)
    assert path.read_bytes() == before
    assert read_json(directory / "task.json")["status"] == "pending"


def test_disagreement_survives_regrading_and_professor_resolves_it(tmp_path, monkeypatch):
    runner, directory, task, rows = free_text_project(tmp_path, monkeypatch)
    write_json(directory / "result.json", envelope(task, {"rows": rows}))
    run(runner, "grade-ingest", "quiz-1", task["task_id"])
    path = tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json"
    before = read_json(path)["students"][0]["items"][1]
    for args in [[], ["--rescore"]]:
        result = run(runner, "grade", "quiz-1", *args)
        assert result["next_commands"] == ["pruefung review quiz-1"]
        assert read_json(path)["students"][0]["items"][1] == before
    assert run(runner, "agent", "next")["phase"] == "professor_review"
    assert runner.invoke(cli, ["grade-make", "quiz-1", "--models", "test"]).exit_code != 0
    inspection = run(runner, "review", "quiz-1")
    assert inspection["items"][0]["panel"][1]["feedback"] == "Rubric assessment: 4"
    provisional = run(runner, "grade-report", "quiz-1", "--anonymize")
    assert provisional["provisional"] is True
    assert provisional["summary"]["mean"] is None
    assert provisional["items"][1]["mean_proportion"] is None
    assert provisional["items"][1]["pending_responses"] == 1
    report_path = Path(run(runner, "post-exam-report", "quiz-1")["html"])
    assert "Provisional report" in report_path.read_text()
    assert "provisional" in path.with_suffix(".csv").read_text()
    args = ["review", "quiz-1", "--student", "student@example.edu", "--question", "q002",
            "--score", "3", "--reason", "Professor awards partial credit"]
    assert runner.invoke(cli, args).exit_code != 0
    run(runner, *args, "--professor-approved")
    resolved = read_json(path)["students"][0]["items"][1]
    assert resolved["score"] == 3 and resolved["override"] and not resolved["needs_review"]
    assert resolved["panel"] == before["panel"]
    assert resolved["override_history"][0]["previous_score"] is None
    assert "Provisional report" not in report_path.read_text()
    assert "student@example.edu,,5.0,6.0,final" in path.with_suffix(".csv").read_text()
    run(runner, "grade", "quiz-1", "--rescore")
    assert read_json(path)["students"][0]["items"][1] == resolved
    assert run(runner, "agent", "next")["phase"] == "responses_and_grading"


def test_report_excludes_pending_scores_but_includes_earned_zero():
    exam = {"exam_id": "quiz", "questions": [{"bank_id": "q001", "question_name": "essay", "frozen": {
        "edsl": {"question_text": "Explain."}, "meta": {"ptype": "free_text", "points": 4,
        "concept": "topic", "rubric": "Four points for a complete explanation."}}}]}
    students = [{"email": f"synthetic-{i}@example.edu", "score": score or 0, "total_points": 4,
                 "items": [{"question_name": "essay", "max_points": 4, "score": score,
                            "needs_review": score is None}]} for i, score in enumerate([None, 0, 4])]
    report = grade_report_data(exam, {"students": students}, True)
    assert report["summary"]["mean"] == 2
    assert report["summary"]["graded_count"] == 2
    assert report["items"][0]["mean_proportion"] == 0.5
    assert report["students"][0]["score"] is None
    assert report["items"][0]["score_total_correlation"] == 1
    assert "Provisional report" in render_grade_report_html(report)


@pytest.mark.parametrize("stale", [False, True])
def test_concept_import_validates_and_completes_task(tmp_path, monkeypatch, stale):
    runner = make_question_project(tmp_path, monkeypatch)
    run(runner, "concepts", "suggest", "--models", "test")
    directory = tmp_path / ".pruefung/inference/concepts_01"
    task = read_json(directory / "task.json")
    write_json(directory / "result.json", envelope(task, {"suggestions": [{"id": "new_topic", "note": "Example"}]}))
    if stale:
        manifest_path = tmp_path / ".pruefung/materials/manifest.json"
        manifest = read_json(manifest_path)
        manifest["sources"]["lecture"]["renders"][0]["hash"] = "different"
        write_json(manifest_path, manifest)
        assert runner.invoke(cli, ["concepts", "import", str(directory / "result.json")]).exit_code != 0
        assert not (tmp_path / ".pruefung/concepts/new_topic.json").exists()
        assert read_json(directory / "task.json")["status"] == "stale"
    else:
        run(runner, "concepts", "import", str(directory / "result.json"))
        assert read_json(directory / "task.json")["status"] == "ingested"
        assert run(runner, "agent", "next")["phase"] != "inference_ingest"
        assert run(runner, "concepts", "import", str(directory / "result.json"))["ingested"] is False
