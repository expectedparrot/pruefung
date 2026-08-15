import json
import py_compile
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from pruefung.cli import cli
from pruefung.core import question_hash, read_json, score_checkbox, write_json
from pruefung.integrations import normalize_response


def invoke(runner, root, args):
    with runner.isolated_filesystem(temp_dir=root):
        return runner.invoke(cli, args)


def test_checkbox_partial_credit():
    assert score_checkbox([0, 2], [0, 1], 4, "per_option") == 0
    assert score_checkbox([0, 2], [0], 4, "per_option") == 2
    assert score_checkbox([0, 2], [0, 2], 4, "none") == 4


def test_agent_next_starts_with_setup_then_concepts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    before = runner.invoke(cli, ["agent", "next"])
    assert before.exit_code == 0
    payload = json.loads(before.output)
    assert payload["data"]["phase"] == "setup"
    assert payload["data"]["action"]["commands"] == ['pruefung init --course "<course name>"']

    runner.invoke(cli, ["init", "--course", "STAT 101"])
    after = runner.invoke(cli, ["agent", "next"])
    payload = json.loads(after.output)
    assert payload["data"]["phase"] == "materials"
    assert payload["data"]["action"]["commands"][0].startswith("pruefung source add")


def test_agent_guide_keeps_internals_out_of_professor_messages():
    guide = (Path(__file__).parents[1] / "AGENTS.md").read_text()
    assert "Pruefung is an implementation detail" in guide
    assert "not “The QC panel is ready.”" in guide
    assert "Never\ncopy the `reason` or `commands` fields" in guide


def test_basic_question_and_exam(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--course", "STAT 101"]).exit_code == 0
    assert runner.invoke(cli, ["concepts", "add", "sampling"]).exit_code == 0
    result = runner.invoke(
        cli,
        [
            "question",
            "add",
            "--type",
            "mcq",
            "--name",
            "sampling_item",
            "--text",
            "Pick",
            "--option",
            "a",
            "--option",
            "b",
            "--option",
            "c",
            "--option",
            "d",
            "--answer",
            "2",
            "--points",
            "2",
            "--concept",
            "sampling",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] and payload["data"]["id"] == "q001"
    assert runner.invoke(cli, ["validate"]).exit_code == 0
    assert runner.invoke(cli, ["exam", "create", "quiz-1"]).exit_code == 0
    assert runner.invoke(cli, ["exam", "add", "quiz-1", "q001"]).exit_code == 0
    stats = json.loads(runner.invoke(cli, ["exam", "stats", "quiz-1"]).output)
    assert stats["data"]["total_points"] == 2
    assert stats["data"]["ready_to_deploy"] is False


def test_invalid_mcq_is_atomic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init", "--course", "X"])
    runner.invoke(cli, ["concepts", "add", "x"])
    result = runner.invoke(
        cli,
        [
            "question",
            "add",
            "--type",
            "mcq",
            "--name",
            "bad",
            "--text",
            "Bad",
            "--option",
            "a",
            "--option",
            "b",
            "--option",
            "c",
            "--answer",
            "1",
            "--points",
            "1",
            "--concept",
            "x",
        ],
    )
    assert result.exit_code == 1  # standalone main maps this domain error to process exit 2
    assert not list((tmp_path / ".pruefung/questions").iterdir())


def make_question_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init", "--course", "Course"])
    source = tmp_path / "lecture.md"
    source.write_text("# Lecture\n\nThe correct answer is beta.\n")
    assert runner.invoke(cli, ["ingest", str(source), "--name", "lecture"]).exit_code == 0
    assert runner.invoke(cli, ["concepts", "add", "topic"]).exit_code == 0
    result = runner.invoke(
        cli,
        [
            "question",
            "add",
            "--type",
            "mcq",
            "--name",
            "topic_item",
            "--text",
            "Choose beta",
            "--option",
            "alpha",
            "--option",
            "beta",
            "--option",
            "gamma",
            "--option",
            "delta",
            "--answer",
            "1",
            "--points",
            "2",
            "--concept",
            "topic",
            "--source-material",
            "lecture",
        ],
    )
    assert result.exit_code == 0, result.output
    return runner


def test_qc_contract_rejects_raw_results_then_ingests_normalized(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    made = runner.invoke(
        cli,
        ["qc", "make", "--models", "test"],
    )
    assert made.exit_code == 0, made.output
    task_dir = tmp_path / ".pruefung/inference/qc_01"
    task = read_json(task_dir / "task.json")
    survey = read_json(task_dir / "survey.edsl.json")
    names = [question["question_name"] for question in survey["questions"]]
    assert "topic_item" in names and "qc_review_q001" in names
    flag = next(question for question in survey["questions"] if question["question_name"] == "qc_review_q001")
    assert "Choose beta" in flag["question_text"]
    assert "The correct answer is beta" in flag["question_text"]
    assert "material:lecture" in task["input_hashes"]
    assert json.loads(made.output)["data"]["estimated_calls"] == 2
    assert "raw_results" not in (task_dir / "run.py").read_text()
    py_compile.compile(str(task_dir / "run.py"), doraise=True)
    approval = subprocess.run(
        [sys.executable, str(task_dir / "run.py")],
        capture_output=True,
        text=True,
    )
    assert approval.returncode == 2
    assert '"requires_approval": true' in approval.stdout

    malformed = {
        "task_id": "qc_01",
        "kind": "qc",
        "input_hashes": task["input_hashes"],
        "created_at": task["created_at"],
        "payload": {"raw_results": []},
    }
    write_json(task_dir / "result.json", malformed)
    rejected = runner.invoke(cli, ["qc", "ingest", "qc_01"])
    assert rejected.exit_code == 1
    assert read_json(tmp_path / ".pruefung/questions/q001.json")["meta"]["status"] == "draft"

    malformed["payload"] = {
        "questions": {
            "q001": {
                "panel_answers": [1, "beta", 1],
                "blocking": [True, "No", False],
                "notes": ["Possible issue", "none", "Looks fine"],
                "models": ["gpt-4o", "gpt-4.1-mini", "gemini-2.5-flash"],
            }
        }
    }
    write_json(task_dir / "result.json", malformed)
    ingested = runner.invoke(cli, ["qc", "ingest", "qc_01"])
    assert ingested.exit_code == 0, ingested.output
    assert read_json(tmp_path / ".pruefung/questions/q001.json")["meta"]["status"] == "qc_passed"
    report = runner.invoke(cli, ["qc", "report", "qc_01"])
    assert report.exit_code == 0, report.output


def test_question_ids_are_retired_and_source_names_are_slugged(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    assert runner.invoke(cli, ["question", "rm", "q001"]).exit_code == 0
    source = tmp_path / "extra.md"
    source.write_text("Extra")
    added_source = runner.invoke(cli, ["source", "add", str(source), "--name", "Lecture 8: Review"])
    assert added_source.exit_code == 0, added_source.output
    assert "lecture-8-review" in read_json(tmp_path / ".pruefung/materials/manifest.json")["sources"]
    added = runner.invoke(
        cli,
        [
            "question",
            "add",
            "--type",
            "true_false",
            "--name",
            "replacement",
            "--text",
            "True?",
            "--answer",
            "true",
            "--points",
            "1",
            "--concept",
            "topic",
        ],
    )
    assert json.loads(added.output)["data"]["id"] == "q002"


def test_all_mcq_bank_requires_professor_mix_review(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    for number in (2, 3):
        result = runner.invoke(
            cli,
            [
                "question",
                "add",
                "--type",
                "mcq",
                "--name",
                f"item_{number}",
                "--text",
                "Pick",
                "--option",
                "a",
                "--option",
                "b",
                "--option",
                "c",
                "--option",
                "d",
                "--answer",
                "0",
                "--points",
                "1",
                "--concept",
                "topic",
            ],
        )
        assert result.exit_code == 0, result.output
    action = json.loads(runner.invoke(cli, ["agent", "next"]).output)["data"]
    assert action["phase"] == "question_mix_review"
    approved = runner.invoke(
        cli, ["question", "mix", "approve", "--decision", "keep", "--note", "Professor wants all MCQ"]
    )
    assert approved.exit_code == 0, approved.output
    assert json.loads(runner.invoke(cli, ["agent", "next"]).output)["data"]["phase"] == "quality_control"


def test_invalid_model_panel_is_atomic(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["qc", "make", "--models", "test,test"])
    assert result.exit_code == 1
    assert "distinct model names" in str(result.exception)
    assert not list((tmp_path / ".pruefung/inference").iterdir())


def test_stale_result_retires_task_for_safe_replacement(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    assert runner.invoke(cli, ["qc", "make", "--models", "test"]).exit_code == 0
    task_dir = tmp_path / ".pruefung/inference/qc_01"
    task = read_json(task_dir / "task.json")
    write_json(
        task_dir / "result.json",
        {
            "task_id": "qc_01",
            "kind": "qc",
            "input_hashes": task["input_hashes"],
            "created_at": task["created_at"],
            "payload": {"questions": {}},
        },
    )
    question_path = tmp_path / ".pruefung/questions/q001.json"
    question = read_json(question_path)
    question["meta"]["content_hash"] = "changed"
    write_json(question_path, question)
    stale = runner.invoke(cli, ["qc", "ingest", "qc_01"])
    assert stale.exit_code == 1
    retired = read_json(task_dir / "task.json")
    assert retired["status"] == "stale"
    assert retired["changed_objects"] == ["q001"]
    replacement = runner.invoke(cli, ["qc", "make", "--models", "test"])
    assert replacement.exit_code == 0, replacement.output
    assert json.loads(replacement.output)["data"]["task_id"] == "qc_02"


def prepare_exam(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    question_path = tmp_path / ".pruefung/questions/q001.json"
    question = read_json(question_path)
    question["meta"]["status"] = "qc_passed"
    write_json(question_path, question)
    runner.invoke(cli, ["exam", "create", "quiz-1"])
    runner.invoke(cli, ["exam", "add", "quiz-1", "q001"])
    roster = tmp_path / "roster.csv"
    roster.write_text("email,name\nstudent@example.edu,Ada\n")
    runner.invoke(cli, ["exam", "roster", "quiz-1", str(roster)])
    return runner


def test_deploy_receipt_resumes_without_duplicate_humanize(tmp_path, monkeypatch):
    runner = prepare_exam(tmp_path, monkeypatch)
    calls = []

    class ScenarioLike:
        def to_dict(self):
            return {"human_survey_uuid": "survey-123"}

    def fake_humanize(self, **kwargs):
        calls.append(kwargs)
        return {"human_survey_uuid": "survey-123", "metadata": ScenarioLike()}

    monkeypatch.setattr("edsl.Survey.humanize", fake_humanize)
    import pruefung.cli as cli_module

    real_write = cli_module.write_json
    failed = {"value": False}

    def fail_final_exam_write(path: Path, value):
        if path.name == "exam.json" and value.get("state") == "deployed" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated disk failure")
        return real_write(path, value)

    monkeypatch.setattr(cli_module, "write_json", fail_final_exam_write)
    first = runner.invoke(cli, ["exam", "deploy", "quiz-1"])
    assert first.exit_code == 1
    receipt = read_json(tmp_path / ".pruefung/exams/quiz-1/deployment.json")
    assert receipt["published"]["project_uuid"] == "survey-123"
    remote_options = receipt["questions"][0]["frozen"]["edsl"]["question_options"]
    monkeypatch.setattr(cli_module, "write_json", real_write)
    second = runner.invoke(cli, ["exam", "deploy", "quiz-1"])
    assert second.exit_code == 0, second.output
    assert len(calls) == 1
    exam = read_json(tmp_path / ".pruefung/exams/quiz-1/exam.json")
    assert exam["published"]["project_uuid"] == "survey-123"
    assert exam["questions"][0]["frozen"]["edsl"]["question_options"] == remote_options
    json.dumps(exam)
    deployed_survey = json.dumps(read_json(tmp_path / ".pruefung/exams/quiz-1/survey.edsl.json"))
    assert '"rubric"' not in deployed_survey
    assert '"points"' not in deployed_survey


def test_status_fetches_and_grade_normalizes_coop_results(tmp_path, monkeypatch):
    runner = prepare_exam(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "edsl.Survey.humanize",
        lambda self, **kwargs: {"human_survey_uuid": "survey-123"},
    )
    assert runner.invoke(cli, ["exam", "deploy", "quiz-1", "--no-shuffle"]).exit_code == 0

    class FakeCoop:
        def get_human_survey_responses(self, uuid):
            assert uuid == "survey-123"
            return [
                {
                    "response_id": "r1",
                    "answer": {
                        "pruefung_respondent_email": "STUDENT@example.edu",
                        "q1_topic": "beta",
                    },
                }
            ]

    monkeypatch.setattr("pruefung.integrations.get_coop", lambda: FakeCoop())
    status = runner.invoke(cli, ["status", "quiz-1"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["data"]["responded"] == 1
    grade = runner.invoke(cli, ["grade", "quiz-1"])
    assert grade.exit_code == 0, grade.output
    gradebook = read_json(tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json")
    assert gradebook["students"][0]["score"] == 2
    assert len(gradebook["students"][0]["items"]) == 1

    gradebook["students"][0]["items"][0].update(score=1.5, override=True)
    gradebook["students"][0]["score"] = 1.5
    write_json(tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json", gradebook)
    rescored = runner.invoke(cli, ["grade", "quiz-1", "--rescore"])
    assert rescored.exit_code == 0, rescored.output
    preserved = read_json(tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json")
    assert preserved["students"][0]["items"][0]["score"] == 1.5
    assert preserved["students"][0]["items"][0]["override"] is True
    report = runner.invoke(cli, ["grade-report", "quiz-1", "--anonymize"])
    assert report.exit_code == 0, report.output
    report_data = json.loads(report.output)["data"]
    assert report_data["students"][0]["student"] == "student-001"
    assert "student@example.edu" not in report.output
    assert report_data["items"][0]["mean_proportion"] == 0.75


def test_open_deploy_and_qc_override(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    question_path = tmp_path / ".pruefung/questions/q001.json"
    question = read_json(question_path)
    question["meta"].update(status="qc_failed", qc={"task_id": "qc_01", "passed": False, "notes": {}})
    write_json(question_path, question)
    override = runner.invoke(
        cli,
        [
            "qc",
            "override",
            "q001",
            "--decision",
            "pass",
            "--reason",
            "Professor reviewed the item",
            "--professor-approved",
        ],
    )
    assert override.exit_code == 0, override.output
    assert read_json(question_path)["meta"]["qc"]["notes"] == {}
    runner.invoke(cli, ["exam", "create", "open-quiz"])
    runner.invoke(cli, ["exam", "add", "open-quiz", "q001"])
    dry_run = runner.invoke(cli, ["exam", "deploy", "open-quiz", "--open", "--dry-run"])
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.output)["data"]["enrollment_mode"] == "open"
    monkeypatch.setattr("edsl.Survey.humanize", lambda self, **kwargs: {"human_survey_uuid": "open-survey"})
    assert runner.invoke(cli, ["exam", "deploy", "open-quiz", "--open", "--no-shuffle"]).exit_code == 0

    class OpenCoop:
        def get_human_survey_responses(self, uuid):
            return [
                {
                    "answer": {
                        "pruefung_respondent_email": {
                            "answer": "student@example.edu",
                            "answered_at": "2026-08-15T13:55:32Z",
                        },
                        "q1_topic": {"answer": "beta"},
                    }
                }
            ]

    monkeypatch.setattr("pruefung.integrations.get_coop", lambda: OpenCoop())
    status = json.loads(runner.invoke(cli, ["status", "open-quiz"]).output)["data"]
    assert status["responded"] == 1
    assert status["unmatched"] == []
    assert status["enrollment_mode"] == "open"


def test_real_humanize_answer_cells_are_unwrapped():
    response = normalize_response(
        {
            "answer": {
                "pruefung_respondent_email": {
                    "answer": "JJHorton@MIT.edu",
                    "comment": None,
                    "answered_at": "2026-08-15T13:55:32Z",
                },
                "q1_rag": {"answer": "A grounded answer", "comment": None},
            }
        }
    )
    assert response["email"] == "jjhorton@mit.edu"
    assert response["answers"]["q1_rag"] == "A grounded answer"
    assert response["submitted_at"] == "2026-08-15T13:55:32Z"


def test_post_exam_html_and_llm_rubric_grading(tmp_path, monkeypatch):
    runner = prepare_exam(tmp_path, monkeypatch)
    objective_path = tmp_path / ".pruefung/questions/q001.json"
    objective = read_json(objective_path)
    objective["meta"]["explanation"] = "Beta follows directly from the lecture."
    objective["meta"]["content_hash"] = question_hash(objective)
    write_json(objective_path, objective)
    added = runner.invoke(
        cli,
        [
            "question",
            "add",
            "--type",
            "free_text",
            "--name",
            "explain_topic",
            "--text",
            "Explain why beta is correct.",
            "--points",
            "4",
            "--concept",
            "topic",
            "--rubric",
            "4 points: identifies the lecture rule and applies it; 2 points: partial explanation; 0: incorrect.",
            "--explanation",
            "A strong response identifies the rule and explicitly applies it to beta.",
        ],
    )
    assert added.exit_code == 0, added.output
    free_path = tmp_path / ".pruefung/questions/q002.json"
    free = read_json(free_path)
    free["meta"]["status"] = "qc_passed"
    write_json(free_path, free)
    assert runner.invoke(cli, ["exam", "add", "quiz-1", "q002"]).exit_code == 0
    monkeypatch.setattr("edsl.Survey.humanize", lambda self, **kwargs: {"human_survey_uuid": "survey-report"})
    assert runner.invoke(cli, ["exam", "deploy", "quiz-1", "--no-shuffle"]).exit_code == 0

    class FakeCoop:
        def get_human_survey_responses(self, uuid):
            assert uuid == "survey-report"
            return [
                {
                    "answer": {
                        "pruefung_respondent_email": "student@example.edu",
                        "q1_topic": "beta",
                        "q2_topic": "Because the lecture rule selects beta.",
                    }
                },
                {
                    "answer": {
                        "pruefung_respondent_email": "second@example.edu",
                        "q1_topic": "alpha",
                        "q2_topic": "I guessed.",
                    }
                },
            ]

    monkeypatch.setattr("pruefung.integrations.get_coop", lambda: FakeCoop())
    assert runner.invoke(cli, ["grade", "quiz-1"]).exit_code == 0
    next_step = json.loads(runner.invoke(cli, ["agent", "next"]).output)["data"]
    assert next_step["phase"] == "free_text_grading"
    assert next_step["action"]["commands"] == ["pruefung grade-make quiz-1"]
    made = runner.invoke(cli, ["grade-make", "quiz-1", "--models", "test"])
    assert made.exit_code == 0, made.output
    task_dir = tmp_path / ".pruefung/inference/grade_quiz-1_01"
    task = read_json(task_dir / "task.json")
    result = {
        "task_id": task["task_id"],
        "kind": "rubric_grade",
        "input_hashes": task["input_hashes"],
        "created_at": task["created_at"],
        "payload": {
            "rows": [
                {
                    "input": {"answer_id": "student@example.edu:q2_topic"},
                    "model": "test",
                    "score": 4,
                    "justification": "Applies the rule.",
                },
                {
                    "input": {"answer_id": "second@example.edu:q2_topic"},
                    "model": "test",
                    "score": 0,
                    "justification": "No rubric criteria.",
                },
            ]
        },
    }
    write_json(task_dir / "result.json", result)
    ingested = runner.invoke(cli, ["grade-ingest", "quiz-1", task["task_id"]])
    assert ingested.exit_code == 0, ingested.output
    gradebook = read_json(tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.json")
    assert {student["email"]: student["score"] for student in gradebook["students"]} == {
        "student@example.edu": 6,
        "second@example.edu": 0,
    }
    assert "student@example.edu,,6.0,6.0" in (tmp_path / ".pruefung/gradebooks/quiz-1.gradebook.csv").read_text()

    output = tmp_path / "report.html"
    report = runner.invoke(cli, ["post-exam-report", "quiz-1", "--output", str(output)])
    assert report.exit_code == 0, report.output
    page = output.read_text()
    assert "Choose beta" in page
    assert "alpha" in page and "beta" in page
    assert "Beta follows directly from the lecture." in page
    assert "Explain why beta is correct." in page
    assert "Score distribution" in page
    assert "A strong response identifies the rule" in page
    assert "student@example.edu" not in page
    assert "E[&#x1f99c;] Expected Parrot" in page
    assert "github.com/expectedparrot/pruefung" in page
    final_step = json.loads(runner.invoke(cli, ["agent", "next"]).output)["data"]
    assert final_step["action"]["commands"][-1] == "pruefung post-exam-report quiz-1"
