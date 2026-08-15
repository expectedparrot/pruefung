import json

from click.testing import CliRunner

from pruefung.cli import cli
from pruefung.core import score_checkbox


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
    assert payload["data"]["phase"] == "concepts"
    assert payload["data"]["action"]["commands"][0] == "pruefung schema concept"


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
