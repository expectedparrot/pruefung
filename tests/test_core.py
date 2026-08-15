import json
import py_compile
from pathlib import Path

from click.testing import CliRunner

from pruefung.cli import cli
from pruefung.core import read_json, score_checkbox, write_json


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
    assert "topic_item" in names and "qc_flag_q001" in names
    flag = next(question for question in survey["questions"] if question["question_name"] == "qc_flag_q001")
    assert "Choose beta" in flag["question_text"]
    assert "The correct answer is beta" in flag["question_text"]
    assert "material:lecture" in task["input_hashes"]
    assert "raw_results" not in (task_dir / "run.py").read_text()
    py_compile.compile(str(task_dir / "run.py"), doraise=True)

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
                "flags": ["none", "none", "Looks fine"],
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


def test_invalid_model_panel_is_atomic(tmp_path, monkeypatch):
    runner = make_question_project(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["qc", "make", "--models", "test,test"])
    assert result.exit_code == 1
    assert "distinct model names" in str(result.exception)
    assert not list((tmp_path / ".pruefung/inference").iterdir())


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
