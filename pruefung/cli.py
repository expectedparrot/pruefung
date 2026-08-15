from __future__ import annotations

import copy
import csv
import html
import json
import random
import shutil
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import click

from . import __version__
from .converters import render_file, render_git, render_url
from .core import (
    ConflictError,
    PruefungError,
    ValidationError,
    canonical_hash,
    concept_files,
    find_root,
    load_exam,
    load_question,
    now,
    parse_roster,
    question_files,
    question_hash,
    read_json,
    score_checkbox,
    slugify,
    state,
    validate_id,
    write_json,
)
from .edsl_support import make_question, make_survey, restore_question
from .integrations import normalize_answer, publication_record, sync_responses


class Context:
    def __init__(self) -> None:
        self.human = False
        self.command = "pruefung"


def emit(ctx: click.Context, data: Any = None, *, warnings: list[str] | None = None) -> None:
    command = ctx.meta.get("command", "pruefung")
    if ctx.find_root().obj.human:
        click.echo(json.dumps(data if data is not None else {}, indent=2, ensure_ascii=False))
        for warning in warnings or []:
            click.echo(f"Warning: {warning}", err=True)
        return
    click.echo(
        json.dumps(
            {"ok": True, "command": command, "data": data or {}, "warnings": warnings or [], "errors": []},
            ensure_ascii=False,
        )
    )


def human_option(function):
    return click.option("--human", "-H", is_flag=True, help="Use human-readable output.")(function)


def setup(ctx: click.Context, command: str, human: bool = False) -> Path:
    ctx.find_root().obj.human = ctx.find_root().obj.human or human
    ctx.meta["command"] = command
    return find_root()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--human", "-H", is_flag=True, help="Use human-readable output.")
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, human: bool) -> None:
    """Build, quality-control, administer, and grade EDSL exams."""
    ctx.obj = Context()
    ctx.obj.human = human


@cli.group("agent")
def agent_group() -> None:
    """State-aware guidance for coding agents."""


def next_action(root: Path) -> tuple[str, dict[str, Any]]:
    concepts = [read_json(path) for path in concept_files(root)]
    questions = [read_json(path) for path in question_files(root)]
    task_directories = sorted(path for path in (state(root) / "inference").iterdir() if path.is_dir())
    tasks = [read_json(path / "task.json") for path in task_directories]
    tasks = [task for task in tasks if task]
    exams = [read_json(path) for path in sorted((state(root) / "exams").glob("*/exam.json"))]

    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    sources = manifest.get("sources", {})
    renders = [render for source in sources.values() for render in source.get("renders", [])]
    if not sources and not concepts:
        return "materials", {
            "kind": "authoring_choice",
            "reason": "No course materials are registered. Register source-backed materials, or deliberately continue with manual concepts.",
            "commands": [
                "pruefung source add <locator> --name <name>",
                "pruefung concepts add <concept-id>",
            ],
        }
    if sources and not renders:
        first = next(iter(sources))
        return "materials", {
            "kind": "authoring",
            "reason": "Course sources are registered but none has a Markdown render.",
            "commands": [f"pruefung source render {first}", "pruefung materials -H"],
        }

    if not concepts:
        return "concepts", {
            "kind": "authoring",
            "reason": "The course has no concepts yet.",
            "commands": ["pruefung schema concept", "pruefung concepts add <concept-id>"],
        }
    if not questions:
        return "item_bank", {
            "kind": "authoring",
            "reason": "Concepts exist, but the item bank is empty.",
            "commands": ["pruefung schema question", "pruefung question add --help"],
        }

    mix = Counter(question["meta"]["ptype"] for question in questions)
    bank_hash = canonical_hash({question["meta"]["id"]: question["meta"]["content_hash"] for question in questions})
    mix_review = read_json(state(root) / "question-mix-review.json", {})
    if len(questions) >= 3 and len(mix) == 1 and mix_review.get("bank_hash") != bank_hash:
        only_type = next(iter(mix))
        return "question_mix_review", {
            "kind": "organizer_input",
            "reason": f"All {len(questions)} questions are {only_type}. Ask the professor whether they want a mix of question types before QC.",
            "type_mix": dict(mix),
            "question": "Keep this all-one-type bank, or revise it to include other formats such as true/false, checkbox, or free text?",
            "user_message": f"All {len(questions)} questions currently use the same format. Would you like to keep that, or include a mix of question types?",
            "commands": [
                'pruefung question mix approve --decision keep --note "<professor rationale>"',
                "pruefung question add --help",
            ],
        }

    pending = [task for task in tasks if task.get("status") == "pending"]
    if pending:
        task = pending[-1]
        result_path = state(root) / "inference" / task["task_id"] / "result.json"
        if result_path.exists():
            if task["kind"] == "qc":
                ingest = f"pruefung qc ingest {task['task_id']}"
            elif task["kind"] == "rubric_grade":
                ingest = f"pruefung grade-ingest {task.get('exam_id', '<exam-id>')} {task['task_id']}"
            else:
                ingest = f"pruefung concepts import {result_path} --review"
            return "inference_ingest", {
                "kind": "review",
                "reason": f"Completed model-assisted work is ready to apply ({task['task_id']}).",
                "user_message": "The independent review is complete. I’m checking the findings against the current questions now.",
                "commands": [ingest],
            }
        return "inference_run", {
            "kind": "external_execution",
            "reason": f"The prepared model-assisted work is awaiting execution ({task['task_id']}).",
            "user_message": "I’ve prepared the independent review. May I run it now? It uses paid model calls.",
            "requires_approval": True,
            "commands": [f"python .pruefung/inference/{task['task_id']}/run.py"],
        }

    drafts = [question["meta"]["id"] for question in questions if question["meta"]["status"] == "draft"]
    if drafts:
        return "quality_control", {
            "kind": "validation",
            "reason": f"{len(drafts)} question(s) need an independent check before use.",
            "user_message": "The questions are drafted. May I have independent reviewers check them for ambiguity and answer-key problems?",
            "question_ids": drafts,
            "commands": ["pruefung validate", "pruefung qc make"],
        }
    failed = [question["meta"]["id"] for question in questions if question["meta"]["status"] == "qc_failed"]
    if failed:
        qc_task = next(
            (
                question["meta"].get("qc", {}).get("task_id")
                for question in questions
                if question["meta"]["id"] in failed and question["meta"].get("qc")
            ),
            None,
        )
        return "revise_questions", {
            "kind": "review",
            "reason": f"Independent reviewers raised concerns about {len(failed)} question(s).",
            "user_message": f"The reviewers raised concerns about {len(failed)} question(s). I’ll summarize each concern so you can revise the wording or keep it as written.",
            "question_ids": failed,
            "commands": [
                *([f"pruefung qc report {qc_task} -H"] if qc_task else []),
                f'pruefung qc override {failed[0]} --decision pass --reason "<professor rationale>" --professor-approved',
                f"pruefung show {failed[0]} -H",
                "pruefung question add --help",
            ],
        }
    if not exams:
        return "exam_build", {
            "kind": "authoring",
            "reason": "The checked item bank is ready, but no exam exists.",
            "commands": ["pruefung exam create <exam-id> --title <title>"],
        }

    building = [exam for exam in exams if exam["state"] == "building"]
    if building:
        exam = building[-1]
        exam_id = exam["exam_id"]
        if not exam.get("members"):
            return "exam_membership", {
                "kind": "authoring",
                "reason": f"Exam {exam_id} has no questions.",
                "commands": ["pruefung ls --status qc_passed", f"pruefung exam add {exam_id} <qid> [<qid> ...]"],
            }
        if not exam.get("roster"):
            return "exam_roster", {
                "kind": "organizer_input",
                "reason": f"Choose whether exam {exam_id} should use a roster or one shared open link.",
                "user_message": "Should students receive individual invitations, or should everyone use one shared link?",
                "commands": [
                    f"pruefung exam stats {exam_id} -H",
                    f"pruefung exam roster {exam_id} <roster.csv>",
                    f"pruefung exam deploy {exam_id} --open --dry-run",
                ],
            }
        return "exam_deploy", {
            "kind": "external_mutation",
            "reason": f"Exam {exam_id} has questions and a roster; preview and approve the frozen deployment.",
            "requires_approval": True,
            "user_message": "The exam is ready for a final preview. After you approve it, I can publish it for students.",
            "commands": [
                f"pruefung exam stats {exam_id} -H",
                f"pruefung exam preview {exam_id} --web",
                f"pruefung exam deploy {exam_id} --dry-run",
                f"pruefung exam deploy {exam_id}",
            ],
        }

    exam = exams[-1]
    exam_id = exam["exam_id"]
    gradebook = read_json(state(root) / "gradebooks" / f"{exam_id}.gradebook.json")
    if not gradebook:
        return "responses_and_grading", {
            "kind": "monitoring",
            "reason": f"Exam {exam_id} is deployed and has not been graded yet.",
            "user_message": "I’ll check for new submissions and update the grades.",
            "commands": [f"pruefung status {exam_id}", f"pruefung grade {exam_id}"],
        }
    unresolved = [
        f"{student['email']}:{item['question_name']}"
        for student in gradebook.get("students", [])
        for item in student.get("items", [])
        if item.get("needs_review")
    ]
    if unresolved:
        return "free_text_grading", {
            "kind": "external_execution",
            "reason": f"{len(unresolved)} free-text answer(s) require rubric scoring or professor review.",
            "requires_approval": True,
            "user_message": f"{len(unresolved)} written response(s) still need rubric-based scoring. May I ask the selected models for score recommendations?",
            "answers": unresolved,
            "commands": [f"pruefung grade-make {exam_id}"],
        }
    return "responses_and_grading", {
        "kind": "monitoring",
        "reason": f"Exam {exam_id} is graded and ready for aggregate reporting.",
        "user_message": "Grading is complete. I can prepare the post-exam report with question-by-question results.",
        "commands": [
            f"pruefung grade-report {exam_id} -H",
            f"pruefung post-exam-report {exam_id}",
        ],
    }


@agent_group.command("next")
@human_option
@click.pass_context
def agent_next(ctx: click.Context, human: bool) -> None:
    """Inspect project state and return the next safe action."""
    ctx.find_root().obj.human |= human
    ctx.meta["command"] = "agent next"
    try:
        root = find_root()
    except PruefungError:
        emit(
            ctx,
            {
                "phase": "setup",
                "project": None,
                "action": {
                    "kind": "organizer_input",
                    "reason": "No pruefung project was found from the current directory.",
                    "commands": ['pruefung init --course "<course name>"'],
                },
            },
        )
        return
    phase, action = next_action(root)
    config = read_json(state(root) / "config.json", {})
    emit(ctx, {"phase": phase, "project": {"root": str(root), "course": config.get("course")}, "action": action})


@cli.command("init")
@click.option("--course", required=True)
@human_option
@click.pass_context
def init_cmd(ctx: click.Context, course: str, human: bool) -> None:
    ctx.obj.human |= human
    ctx.meta["command"] = "init"
    directory = Path.cwd() / ".pruefung"
    if directory.exists():
        raise ConflictError(f"already initialized: {directory}")
    for name in ("materials/raw", "materials/md", "concepts", "questions", "exams", "inference", "gradebooks"):
        (directory / name).mkdir(parents=True)
    write_json(
        directory / "config.json",
        {
            "course": course,
            "created_at": now(),
            "next_question_number": 1,
            "retired_question_ids": [],
            "duration_minutes": {"mcq": 1.5, "true_false": 1.5, "checkbox": 2, "free_text": 5},
        },
    )
    write_json(directory / "materials/manifest.json", {"sources": {}})
    emit(ctx, {"root": str(Path.cwd()), "course": course})


@cli.group("source")
def source_group() -> None:
    """Register and render source materials."""


def infer_kind(locator: str) -> str:
    if locator.startswith(("http://", "https://")):
        return "url"
    if Path(locator).suffix.lower() in {".vtt", ".srt"}:
        return "transcript"
    return "file"


@source_group.command("add")
@click.argument("locator")
@click.option("--name")
@click.option("--kind")
@click.option("--rev")
@click.option("--paths")
@click.option("--of-url")
@human_option
@click.pass_context
def source_add(
    ctx: click.Context,
    locator: str,
    name: str | None,
    kind: str | None,
    rev: str | None,
    paths: str | None,
    of_url: str | None,
    human: bool,
) -> None:
    root = setup(ctx, "source add", human)
    kind = kind or infer_kind(locator)
    default_name = Path(locator.rstrip("/")).stem or "source"
    original_name = name or default_name
    name = slugify(original_name)
    validate_id(name, r"[A-Za-z0-9_.-]+", "source name")
    manifest_path = state(root) / "materials/manifest.json"
    manifest = read_json(manifest_path, {"sources": {}})
    if name in manifest["sources"]:
        raise ConflictError(f"source already exists: {name}")
    record = {"name": name, "kind": kind, "locator": locator, "added_at": now(), "renders": []}
    if rev:
        record["rev"] = rev
    if paths:
        record["paths"] = paths
    if of_url:
        record["of_url"] = of_url
    if kind in {"file", "transcript"}:
        source_path = Path(locator).expanduser().resolve()
        if not source_path.is_file():
            raise ValidationError(f"source file not found: {locator}")
        target = state(root) / "materials/raw" / f"{name}{source_path.suffix.lower()}"
        shutil.copyfile(source_path, target)
        record.update(raw=str(target.relative_to(state(root))), raw_hash=canonical_hash(target.read_bytes().hex()))
    manifest["sources"][name] = record
    write_json(manifest_path, manifest)
    emit(
        ctx,
        record,
        warnings=[f"source name normalized from {original_name!r} to {name!r}"] if name != original_name else [],
    )


def render_source(root: Path, name: str, slice_spec: str | None) -> tuple[dict[str, Any], list[str]]:
    manifest_path = state(root) / "materials/manifest.json"
    manifest = read_json(manifest_path, {"sources": {}})
    if name not in manifest["sources"]:
        raise ValidationError(f"unknown source: {name}")
    source = manifest["sources"][name]
    kind = source["kind"]
    if kind == "url":
        raw, markdown = render_url(source["locator"])
        raw_path = state(root) / "materials/raw" / f"{name}.html"
        raw_path.write_bytes(raw)
        source.update(raw=str(raw_path.relative_to(state(root))), fetched_at=now(), raw_hash=canonical_hash(raw.hex()))
    elif kind == "git":
        markdown = render_git(source["locator"], source.get("rev"), source.get("paths"))
    elif kind in {"file", "transcript"}:
        raw_path = state(root) / source["raw"]
        markdown = render_file(raw_path, slice_spec)
    else:
        raise ValidationError(
            f"no converter for kind {kind!r}; implement the converter interface in pruefung.converters"
        )
    render_name = name if not slice_spec else f"{name}@{slice_spec}"
    target = state(root) / "materials/md" / f"{render_name}.md"
    old = next((x for x in source["renders"] if x["name"] == render_name), None)
    target.write_text(markdown, encoding="utf-8")
    record = {
        "name": render_name,
        "slice": slice_spec,
        "path": str(target.relative_to(state(root))),
        "absolute_path": str(target),
        "hash": canonical_hash(markdown),
        "chars": len(markdown),
        "rendered_at": now(),
        "inspect_command": f"pruefung materials --show {render_name}",
    }
    source["renders"] = [x for x in source["renders"] if x["name"] != render_name] + [record]
    write_json(manifest_path, manifest)
    warnings = [f"re-rendered {render_name}: {old['hash'][:12]} -> {record['hash'][:12]}"] if old else []
    return record, warnings


@source_group.command("render")
@click.argument("name")
@click.option("--slice", "slice_spec")
@human_option
@click.pass_context
def source_render(ctx: click.Context, name: str, slice_spec: str | None, human: bool) -> None:
    root = setup(ctx, "source render", human)
    record, warnings = render_source(root, name, slice_spec)
    emit(ctx, record, warnings=warnings)


@cli.command("ingest")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option("--name")
@human_option
@click.pass_context
def ingest_cmd(ctx: click.Context, path: Path, name: str | None, human: bool) -> None:
    root = setup(ctx, "ingest", human)
    original_name = name or path.stem
    name = slugify(original_name)
    manifest_path = state(root) / "materials/manifest.json"
    manifest = read_json(manifest_path, {"sources": {}})
    if name in manifest["sources"]:
        raise ConflictError(f"source already exists: {name}")
    target = state(root) / "materials/raw" / f"{name}{path.suffix.lower()}"
    shutil.copyfile(path, target)
    manifest["sources"][name] = {
        "name": name,
        "kind": infer_kind(str(path)),
        "locator": str(path.resolve()),
        "raw": str(target.relative_to(state(root))),
        "raw_hash": canonical_hash(target.read_bytes().hex()),
        "added_at": now(),
        "renders": [],
    }
    write_json(manifest_path, manifest)
    record, warnings = render_source(root, name, None)
    if name != original_name:
        warnings.append(f"source name normalized from {original_name!r} to {name!r}")
    emit(ctx, {"source": name, "render": record}, warnings=warnings)


@cli.command("materials")
@click.option("--show")
@human_option
@click.pass_context
def materials_cmd(ctx: click.Context, show: str | None, human: bool) -> None:
    root = setup(ctx, "materials", human)
    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    if show:
        for source in manifest["sources"].values():
            for render in source.get("renders", []):
                if render["name"] == show:
                    emit(ctx, {"name": show, "markdown": (state(root) / render["path"]).read_text()})
                    return
        raise ValidationError(f"unknown material render: {show}")
    emit(ctx, {"sources": list(manifest["sources"].values())})


@cli.group("concepts", invoke_without_command=True)
@human_option
@click.pass_context
def concepts_group(ctx: click.Context, human: bool) -> None:
    """Manage the concept catalog."""
    if ctx.invoked_subcommand is None:
        root = setup(ctx, "concepts", human)
        emit(ctx, {"concepts": [read_json(p) for p in concept_files(root)]})


@concepts_group.command("add")
@click.argument("concept_id")
@click.option("--note")
@human_option
@click.pass_context
def concepts_add(ctx: click.Context, concept_id: str, note: str | None, human: bool) -> None:
    root = setup(ctx, "concepts add", human)
    validate_id(concept_id, r"[a-z0-9_]+", "concept id")
    path = state(root) / "concepts" / f"{concept_id}.json"
    if path.exists():
        raise ConflictError(f"concept already exists: {concept_id}")
    value = {"id": concept_id}
    if note:
        value["note"] = note
    value["added_at"] = now()
    write_json(path, value)
    emit(ctx, value)


@concepts_group.command("rm")
@click.argument("concept_id")
@human_option
@click.pass_context
def concepts_rm(ctx: click.Context, concept_id: str, human: bool) -> None:
    root = setup(ctx, "concepts rm", human)
    used = [p.stem for p in question_files(root) if read_json(p).get("meta", {}).get("concept") == concept_id]
    if used:
        raise ConflictError(f"concept {concept_id} is referenced by: {', '.join(used)}")
    path = state(root) / "concepts" / f"{concept_id}.json"
    if not path.exists():
        raise ValidationError(f"unknown concept: {concept_id}")
    path.unlink()
    emit(ctx, {"removed": concept_id})


@concepts_group.command("import")
@click.argument("input_file", type=click.Path(path_type=Path, exists=True))
@click.option("--review", is_flag=True)
@human_option
@click.pass_context
def concepts_import(ctx: click.Context, input_file: Path, review: bool, human: bool) -> None:
    root = setup(ctx, "concepts import", human or review)
    if review and not ctx.find_root().obj.human:
        raise ValidationError("--review requires --human")
    data = read_json(input_file)
    items = data.get("concepts") or data.get("payload", {}).get("suggestions")
    if not isinstance(items, list):
        raise ValidationError("input must contain concepts or payload.suggestions")
    added, skipped = [], []
    for item in items:
        cid = item.get("id", "")
        validate_id(cid, r"[a-z0-9_]+", "concept id")
        path = state(root) / "concepts" / f"{cid}.json"
        if path.exists():
            skipped.append(cid)
            continue
        value = {"id": cid, "note": item.get("note", ""), "added_at": now()}
        write_json(path, value)
        added.append(cid)
    emit(ctx, {"added": added}, warnings=[f"duplicate skipped: {x}" for x in skipped])


@concepts_group.command("suggest")
@click.option("--materials")
@click.option("--models", default="gpt-4o")
@click.option("--force", is_flag=True)
@human_option
@click.pass_context
def concepts_suggest(ctx: click.Context, materials: str | None, models: str, force: bool, human: bool) -> None:
    root = setup(ctx, "concepts suggest", human)
    requested = [x.strip() for x in materials.split(",")] if materials else []
    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    renders = {r["name"]: r for s in manifest["sources"].values() for r in s.get("renders", [])}
    missing = set(requested) - set(renders)
    if missing:
        raise ValidationError(f"unknown material renders: {', '.join(sorted(missing))}")
    selected = requested or sorted(renders)
    text = "\n\n".join((state(root) / renders[x]["path"]).read_text() for x in selected)
    hashes = {f"material:{x}": renders[x]["hash"] for x in selected}
    edsl = __import__("edsl")
    survey = edsl.Survey(
        [
            edsl.QuestionList(
                question_name="suggestions",
                question_text="List the 10-15 most important testable concepts as objects with snake_case id and a short note. Materials: {{ input }}",
            )
        ]
    )
    task_id, created = make_task(
        root,
        "concept_suggest",
        "concepts",
        hashes,
        [{"materials": selected, "text": text}],
        models.split(","),
        survey,
        force,
    )
    emit(
        ctx,
        {
            "task_id": task_id,
            "created": created,
            "run": f"python .pruefung/inference/{task_id}/run.py --yes",
            "requires_approval": True,
        },
    )


@cli.group("question")
def question_group() -> None:
    """Create and modify EDSL-native questions."""


@question_group.group("mix")
def question_mix_group() -> None:
    """Review the item bank's question-type composition."""


@question_mix_group.command("approve")
@click.option("--decision", type=click.Choice(["keep"]), required=True)
@click.option("--note", required=True)
@human_option
@click.pass_context
def question_mix_approve(ctx: click.Context, decision: str, note: str, human: bool) -> None:
    root = setup(ctx, "question mix approve", human)
    questions = [read_json(path) for path in question_files(root)]
    if not questions:
        raise ValidationError("the item bank is empty")
    mix = Counter(question["meta"]["ptype"] for question in questions)
    bank_hash = canonical_hash({question["meta"]["id"]: question["meta"]["content_hash"] for question in questions})
    record = {
        "decision": decision,
        "note": note,
        "type_mix": dict(mix),
        "bank_hash": bank_hash,
        "approved_at": now(),
    }
    write_json(state(root) / "question-mix-review.json", record)
    emit(ctx, record)


def parse_answer(ptype: str, answer: str | None, option_count: int) -> Any:
    if ptype == "free_text":
        return None
    if answer is None:
        raise ValidationError("--answer is required")
    if ptype == "true_false":
        if answer.lower() not in {"true", "false"}:
            raise ValidationError("true_false requires --answer true or --answer false")
        return answer.lower() == "true"
    try:
        value = sorted(set(int(x.strip()) for x in answer.split(","))) if ptype == "checkbox" else int(answer)
    except ValueError as exc:
        raise ValidationError("answer must be a zero-based index, e.g. --answer 2 for the third option") from exc
    values = value if isinstance(value, list) else [value]
    if any(x < 0 or x >= option_count for x in values):
        raise ValidationError(f"answer index out of range; valid zero-based indices are 0-{option_count - 1}")
    return value


@question_group.command("add")
@click.option("--type", "ptype", type=click.Choice(["mcq", "true_false", "checkbox", "free_text"]), required=True)
@click.option("--name", required=True)
@click.option("--text")
@click.option("--text-file", type=click.Path(path_type=Path, exists=True))
@click.option("--option", "options", multiple=True, help="Answer option; repeat in displayed order.")
@click.option(
    "--answer",
    help="MCQ: zero-based option index; checkbox: comma-separated indices; true/false: true or false.",
)
@click.option("--points", type=float, required=True)
@click.option("--concept", required=True)
@click.option("--rubric")
@click.option("--rubric-file", type=click.Path(path_type=Path, exists=True))
@click.option("--explanation", help="Explanation shown in the post-exam report.")
@click.option("--explanation-file", type=click.Path(path_type=Path, exists=True))
@click.option("--partial-credit", type=click.Choice(["none", "per_option"]), default="none")
@click.option("--distractor-notes")
@click.option("--source-material", multiple=True)
@click.option("--id", "qid")
@human_option
@click.pass_context
def question_add(
    ctx: click.Context,
    ptype: str,
    name: str,
    text: str | None,
    text_file: Path | None,
    options: tuple[str, ...],
    answer: str | None,
    points: float,
    concept: str,
    rubric: str | None,
    rubric_file: Path | None,
    explanation: str | None,
    explanation_file: Path | None,
    partial_credit: str,
    distractor_notes: str | None,
    source_material: tuple[str, ...],
    qid: str | None,
    human: bool,
) -> None:
    root = setup(ctx, "question add", human)
    if bool(text) == bool(text_file):
        raise ValidationError("provide exactly one of --text or --text-file")
    text = text if text is not None else text_file.read_text(encoding="utf-8")
    if points <= 0:
        raise ValidationError("points must be positive")
    if not (state(root) / "concepts" / f"{concept}.json").is_file():
        raise ValidationError(f"unknown concept: {concept}")
    validate_id(name, r"[a-z][a-z0-9_]*", "question name")
    existing = [read_json(p) for p in question_files(root)]
    if any(q["edsl"].get("question_name") == name and q["meta"]["id"] != qid for q in existing):
        raise ConflictError(f"question name already exists: {name}")
    if ptype == "mcq" and len(options) != 4:
        raise ValidationError("mcq requires exactly 4 options")
    if ptype == "checkbox" and not 3 <= len(options) <= 6:
        raise ValidationError("checkbox requires 3-6 options")
    if ptype in {"true_false", "free_text"} and options:
        raise ValidationError(f"{ptype} does not accept options")
    rubric = rubric if rubric is not None else (rubric_file.read_text(encoding="utf-8") if rubric_file else None)
    if explanation and explanation_file:
        raise ValidationError("use only one explanation input")
    explanation = explanation or (explanation_file.read_text(encoding="utf-8") if explanation_file else None)
    if ptype == "free_text" and not rubric:
        raise ValidationError("free_text requires --rubric or --rubric-file")
    parsed_answer = parse_answer(ptype, answer, len(options))
    edsl_dict = make_question(ptype, name, text, list(options)).to_dict()
    config_path = state(root) / "config.json"
    config = read_json(config_path, {})
    retired = set(config.get("retired_question_ids", []))
    if qid is None:
        used = {p.stem for p in question_files(root)} | retired
        number = max(1, int(config.get("next_question_number", 1)))
        while f"q{number:03d}" in used:
            number += 1
        qid = f"q{number:03d}"
    validate_id(qid, r"q\d{3,}", "question id")
    path = state(root) / "questions" / f"{qid}.json"
    old = read_json(path)
    if not old and qid in retired:
        raise ConflictError(f"question id {qid} was retired and cannot be reused")
    timestamp = now()
    meta: dict[str, Any] = {
        "id": qid,
        "ptype": ptype,
        "concept": concept,
        "points": points,
        "status": "draft",
        "qc": None,
        "created_at": old.get("meta", {}).get("created_at", timestamp) if old else timestamp,
        "updated_at": timestamp,
    }
    if parsed_answer is not None:
        meta["answer"] = parsed_answer
    if ptype == "checkbox":
        meta["partial_credit"] = partial_credit
    if rubric:
        meta["rubric"] = rubric
    if explanation:
        meta["explanation"] = explanation
    if distractor_notes:
        meta["distractor_notes"] = distractor_notes
    if source_material:
        manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
        known_renders = {
            render["name"] for source in manifest.get("sources", {}).values() for render in source.get("renders", [])
        }
        unknown = set(source_material) - known_renders
        if unknown:
            raise ValidationError(f"unknown material renders: {', '.join(sorted(unknown))}")
        meta["source_materials"] = list(source_material)
    value = {"edsl": edsl_dict, "meta": meta}
    meta["content_hash"] = question_hash(value)
    write_json(path, value)
    number = int(qid[1:])
    if number >= int(config.get("next_question_number", 1)):
        config["next_question_number"] = number + 1
        write_json(config_path, config)
    emit(ctx, {"id": qid, "hash": meta["content_hash"], "edsl": edsl_dict, "replaced": old is not None})


@question_group.command("set")
@click.argument("qid")
@click.argument("field")
@click.argument("value")
@human_option
@click.pass_context
def question_set(ctx: click.Context, qid: str, field: str, value: str, human: bool) -> None:
    root = setup(ctx, "question set", human)
    q = load_question(root, qid)
    mapping = {"distractor-notes": "distractor_notes", "partial-credit": "partial_credit"}
    field = mapping.get(field, field)
    if field not in {"points", "concept", "answer", "rubric", "explanation", "distractor_notes", "partial_credit"}:
        raise ValidationError(f"unsupported field: {field}")
    if field == "points":
        value = float(value)
    elif field == "answer":
        value = parse_answer(q["meta"]["ptype"], value, len(q["edsl"].get("question_options", [])))
    elif field == "concept" and not (state(root) / "concepts" / f"{value}.json").exists():
        raise ValidationError(f"unknown concept: {value}")
    q["meta"][field] = value
    q["meta"].update(status="draft", qc=None, updated_at=now())
    q["meta"]["content_hash"] = question_hash(q)
    write_json(state(root) / "questions" / f"{qid}.json", q)
    emit(ctx, {"id": qid, "field": field, "value": value, "hash": q["meta"]["content_hash"]})


@question_group.command("rm")
@click.argument("qid")
@human_option
@click.pass_context
def question_rm(ctx: click.Context, qid: str, human: bool) -> None:
    root = setup(ctx, "question rm", human)
    exams = []
    for path in (state(root) / "exams").glob("*/exam.json"):
        exam = read_json(path)
        ids = exam.get("members", []) + [x["bank_id"] for x in exam.get("questions", [])]
        if qid in ids:
            exams.append(exam["exam_id"])
    if exams:
        raise ConflictError(f"question {qid} is used by exams: {', '.join(exams)}")
    path = state(root) / "questions" / f"{qid}.json"
    if not path.exists():
        raise ValidationError(f"unknown question: {qid}")
    path.unlink()
    config_path = state(root) / "config.json"
    config = read_json(config_path, {})
    retired = set(config.get("retired_question_ids", []))
    retired.add(qid)
    config["retired_question_ids"] = sorted(retired)
    config["next_question_number"] = max(int(config.get("next_question_number", 1)), int(qid[1:]) + 1)
    write_json(config_path, config)
    emit(ctx, {"removed": qid})


@cli.command("validate")
@human_option
@click.pass_context
def validate_cmd(ctx: click.Context, human: bool) -> None:
    root = setup(ctx, "validate", human)
    errors, changed = [], []
    concepts = {p.stem for p in concept_files(root)}
    names: set[str] = set()
    for path in question_files(root):
        try:
            q = read_json(path)
            meta = q["meta"]
            if meta["id"] != path.stem:
                raise ValidationError("id does not match filename")
            restore_question(q["edsl"])
            if meta["concept"] not in concepts:
                raise ValidationError(f"unknown concept {meta['concept']}")
            name = q["edsl"]["question_name"]
            if name in names:
                raise ValidationError(f"duplicate question_name {name}")
            names.add(name)
            current = question_hash(q)
            if current != meta.get("content_hash"):
                meta.update(content_hash=current, status="draft", qc=None, updated_at=now())
                write_json(path, q)
                changed.append(path.stem)
        except (KeyError, PruefungError) as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        raise ValidationError("; ".join(errors))
    emit(ctx, {"valid": len(question_files(root)), "changed": changed})


@cli.command("ls")
@click.option("--status")
@click.option("--concept")
@click.option("--type", "ptype")
@human_option
@click.pass_context
def ls_cmd(ctx: click.Context, status: str | None, concept: str | None, ptype: str | None, human: bool) -> None:
    root = setup(ctx, "ls", human)
    items = [read_json(p) for p in question_files(root)]
    if status:
        items = [q for q in items if q["meta"]["status"] == status]
    if concept:
        items = [q for q in items if q["meta"]["concept"] == concept]
    if ptype:
        items = [q for q in items if q["meta"]["ptype"] == ptype]
    emit(ctx, {"questions": items})


@cli.command("show")
@click.argument("qid")
@human_option
@click.pass_context
def show_cmd(ctx: click.Context, qid: str, human: bool) -> None:
    root = setup(ctx, "show", human)
    q = load_question(root, qid)
    exams = []
    for p in (state(root) / "exams").glob("*/exam.json"):
        e = read_json(p)
        if qid in e.get("members", []) or qid in [x["bank_id"] for x in e.get("questions", [])]:
            exams.append(e["exam_id"])
    if human:
        meta, edsl_data = q["meta"], q["edsl"]
        click.echo(f"{qid} · {meta['ptype']} · {meta['points']} point(s) · {meta['status']}")
        click.echo(f"Concept: {meta['concept']}")
        click.echo(f"\n{edsl_data['question_text']}")
        for index, option in enumerate(edsl_data.get("question_options", [])):
            answer = meta.get("answer")
            correct = index in answer if isinstance(answer, list) else index == answer
            click.echo(f"  {'✓' if correct else ' '} [{index}] {option}")
        if "answer" in meta and not edsl_data.get("question_options"):
            click.echo(f"Answer: {meta['answer']}")
        if meta.get("rubric"):
            click.echo(f"\nRubric:\n{meta['rubric']}")
        if meta.get("source_materials"):
            click.echo(f"\nSources: {', '.join(meta['source_materials'])}")
        if meta.get("qc"):
            click.echo(f"\nQC:\n{json.dumps(meta['qc'], indent=2, ensure_ascii=False)}")
        click.echo(f"\nExams: {', '.join(exams) if exams else 'none'}")
        return
    emit(ctx, {"question": q, "exams": exams})


@cli.command("coverage")
@human_option
@click.pass_context
def coverage_cmd(ctx: click.Context, human: bool) -> None:
    root = setup(ctx, "coverage", human)
    counts = Counter(read_json(p)["meta"]["concept"] for p in question_files(root))
    emit(ctx, {"concepts": [{"id": p.stem, "questions": counts[p.stem]} for p in concept_files(root)]})


@cli.group("exam")
def exam_group() -> None:
    """Build, inspect, freeze, and deploy exams."""


def require_building(exam: dict[str, Any]) -> None:
    if exam.get("state") != "building":
        raise ConflictError(f"exam {exam['exam_id']} is deployed and immutable")


@exam_group.command("create")
@click.argument("exam_id")
@click.option("--title")
@click.option("--instructions")
@click.option("--instructions-file", type=click.Path(path_type=Path, exists=True))
@human_option
@click.pass_context
def exam_create(
    ctx: click.Context,
    exam_id: str,
    title: str | None,
    instructions: str | None,
    instructions_file: Path | None,
    human: bool,
) -> None:
    root = setup(ctx, "exam create", human)
    if not __import__("re").fullmatch(r"[a-z0-9-]+", exam_id):
        suggestion = exam_id.lower().replace("_", "-")
        raise ValidationError(f"exam IDs allow lowercase letters, digits, and hyphens; try {suggestion or 'quiz-01'}")
    path = state(root) / "exams" / exam_id / "exam.json"
    if path.exists():
        raise ConflictError(f"exam already exists: {exam_id}")
    if instructions and instructions_file:
        raise ValidationError("use only one instructions input")
    value = {
        "exam_id": exam_id,
        "title": title or exam_id,
        "state": "building",
        "created_at": now(),
        "members": [],
        "roster": [],
    }
    instruction_text = instructions or (instructions_file.read_text() if instructions_file else None)
    if instruction_text:
        value["instructions"] = instruction_text
    write_json(path, value)
    emit(ctx, value)


@exam_group.command("add")
@click.argument("exam_id")
@click.argument("qids", nargs=-1, required=True)
@click.option("--at", type=int)
@human_option
@click.pass_context
def exam_add(ctx: click.Context, exam_id: str, qids: tuple[str, ...], at: int | None, human: bool) -> None:
    root = setup(ctx, "exam add", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    duplicates = set(qids) & set(exam["members"])
    if duplicates or len(set(qids)) != len(qids):
        raise ConflictError(f"duplicate membership: {', '.join(sorted(duplicates or set(qids)))}")
    questions = [load_question(root, qid) for qid in qids]
    if at is None:
        exam["members"].extend(qids)
    else:
        if at < 0 or at > len(exam["members"]):
            raise ValidationError("--at is outside the exam")
        exam["members"][at:at] = qids
    write_json(path, exam)
    warnings = [
        f"{q['meta']['id']} status is {q['meta']['status']}" for q in questions if q["meta"]["status"] != "qc_passed"
    ]
    emit(ctx, {"exam_id": exam_id, "members": exam["members"]}, warnings=warnings)


@exam_group.command("remove")
@click.argument("exam_id")
@click.argument("qids", nargs=-1, required=True)
@human_option
@click.pass_context
def exam_remove(ctx: click.Context, exam_id: str, qids: tuple[str, ...], human: bool) -> None:
    root = setup(ctx, "exam remove", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    missing = set(qids) - set(exam["members"])
    if missing:
        raise ValidationError(f"not in exam: {', '.join(sorted(missing))}")
    exam["members"] = [x for x in exam["members"] if x not in qids]
    write_json(path, exam)
    emit(ctx, {"exam_id": exam_id, "members": exam["members"]})


@exam_group.command("reorder")
@click.argument("exam_id")
@click.option("--order", required=True)
@human_option
@click.pass_context
def exam_reorder(ctx: click.Context, exam_id: str, order: str, human: bool) -> None:
    root = setup(ctx, "exam reorder", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    requested = [x.strip() for x in order.split(",")]
    if len(requested) != len(set(requested)) or set(requested) != set(exam["members"]):
        raise ValidationError("order must contain every member exactly once")
    exam["members"] = requested
    write_json(path, exam)
    emit(ctx, {"exam_id": exam_id, "members": requested})


def exam_stats(root: Path, exam: dict[str, Any]) -> dict[str, Any]:
    qs = (
        [load_question(root, x) for x in exam.get("members", [])]
        if exam["state"] == "building"
        else [x["frozen"] for x in exam["questions"]]
    )
    config = read_json(state(root) / "config.json")
    durations = config["duration_minutes"]
    points = sum(float(q["meta"]["points"]) for q in qs)
    by_concept: dict[str, dict[str, float]] = defaultdict(lambda: {"questions": 0, "points": 0})
    for q in qs:
        row = by_concept[q["meta"]["concept"]]
        row["questions"] += 1
        row["points"] += q["meta"]["points"]
    all_concepts = {p.stem for p in concept_files(root)}
    warnings = [
        f"{q['meta']['id']} exceeds 50% of total points" for q in qs if points and q["meta"]["points"] > points / 2
    ]
    return {
        "exam_id": exam["exam_id"],
        "state": exam["state"],
        "question_count": len(qs),
        "total_points": points,
        "estimated_minutes": sum(durations[q["meta"]["ptype"]] for q in qs),
        "type_mix": dict(Counter(q["meta"]["ptype"] for q in qs)),
        "concept_coverage": dict(by_concept),
        "uncovered_concepts": sorted(all_concepts - set(by_concept)),
        "qc": {q["meta"]["id"]: q["meta"]["status"] for q in qs},
        "ready_to_deploy": bool(qs and exam.get("roster") and all(q["meta"]["status"] == "qc_passed" for q in qs)),
        "warnings": warnings,
        "responses": len(read_json(state(root) / "exams" / exam["exam_id"] / "responses.json", [])),
    }


@exam_group.command("stats")
@click.argument("exam_id")
@human_option
@click.pass_context
def exam_stats_cmd(ctx: click.Context, exam_id: str, human: bool) -> None:
    root = setup(ctx, "exam stats", human)
    _, exam = load_exam(root, exam_id)
    data = exam_stats(root, exam)
    emit(ctx, data, warnings=data.pop("warnings"))


@exam_group.command("roster")
@click.argument("exam_id")
@click.argument("roster_file", type=click.Path(path_type=Path, exists=True))
@human_option
@click.pass_context
def exam_roster(ctx: click.Context, exam_id: str, roster_file: Path, human: bool) -> None:
    root = setup(ctx, "exam roster", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    rows, warnings = parse_roster(roster_file)
    exam["roster"] = rows
    write_json(path, exam)
    emit(ctx, {"exam_id": exam_id, "rows": len(rows), "roster": rows}, warnings=warnings)


def freeze_exam(
    root: Path, exam: dict[str, Any], shuffle: bool, seed: int | None = None
) -> tuple[list[dict[str, Any]], Any]:
    rng = random.Random(seed)
    frozen_rows = []
    for index, qid in enumerate(exam["members"], start=1):
        frozen = copy.deepcopy(load_question(root, qid))
        ptype = frozen["meta"]["ptype"]
        permutation = None
        if shuffle and ptype in {"mcq", "checkbox"}:
            old_options = frozen["edsl"]["question_options"]
            permutation = list(range(len(old_options)))
            rng.shuffle(permutation)
            frozen["edsl"]["question_options"] = [old_options[i] for i in permutation]
            inverse = {old: new for new, old in enumerate(permutation)}
            answer = frozen["meta"]["answer"]
            frozen["meta"]["answer"] = (
                sorted(inverse[x] for x in answer) if isinstance(answer, list) else inverse[answer]
            )
        question_name = f"q{index}_{frozen['meta']['concept']}"
        frozen["edsl"]["question_name"] = question_name
        frozen_rows.append(
            {
                "bank_id": qid,
                "bank_content_hash": frozen["meta"]["content_hash"],
                "frozen": frozen,
                "option_permutation": permutation,
                "question_name": question_name,
            }
        )
    survey = make_survey([x["frozen"]["edsl"] for x in frozen_rows], exam.get("instructions"))
    return frozen_rows, survey


@exam_group.command("preview")
@click.argument("exam_id")
@click.option("--web", is_flag=True)
@human_option
@click.pass_context
def exam_preview(ctx: click.Context, exam_id: str, web: bool, human: bool) -> None:
    root = setup(ctx, "exam preview", human)
    path, exam = load_exam(root, exam_id)
    if exam["state"] == "building":
        rows, survey = freeze_exam(root, exam, True, seed=0)
    else:
        rows, survey = (
            exam["questions"],
            make_survey([x["frozen"]["edsl"] for x in exam["questions"]], exam.get("instructions")),
        )
    if not web:
        emit(ctx, {"exam_id": exam_id, "questions": rows})
        return
    try:
        published = survey.humanize(
            human_survey_name=f"{exam_id}-preview",
            survey_visibility="private",
        )
    except Exception as exc:
        raise PruefungError(f"web preview failed: {exc}", code="network_error", exit_code=4) from exc
    record = {"created_at": now(), **publication_record(published)}
    exam.setdefault("previews", []).append(record)
    write_json(path, exam)
    emit(ctx, record)


@exam_group.command("deploy")
@click.argument("exam_id")
@click.option("--dry-run", is_flag=True)
@click.option("--no-shuffle", is_flag=True)
@click.option("--allow-draft", is_flag=True)
@click.option("--open", "open_link", is_flag=True, help="Publish one shared link without a roster.")
@human_option
@click.pass_context
def exam_deploy(
    ctx: click.Context,
    exam_id: str,
    dry_run: bool,
    no_shuffle: bool,
    allow_draft: bool,
    open_link: bool,
    human: bool,
) -> None:
    root = setup(ctx, "exam deploy", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    qs = [load_question(root, qid) for qid in exam["members"]]
    if not qs:
        raise ValidationError("exam has no questions")
    if not exam.get("roster") and not open_link:
        raise ValidationError("exam has no roster; use --open to publish a shared link")
    not_ready = [q["meta"]["id"] for q in qs if q["meta"]["status"] != "qc_passed"]
    if not_ready and not allow_draft:
        raise ConflictError(f"questions have not passed QC: {', '.join(not_ready)}")
    rows, survey = freeze_exam(root, exam, not no_shuffle)
    survey_dict = survey.to_dict()
    leak = json.dumps(survey_dict)
    for q in rows:
        if "rubric" in q["frozen"]["meta"] and q["frozen"]["meta"]["rubric"] in leak:
            raise ValidationError("survey export leaked rubric")
    if dry_run:
        emit(
            ctx,
            {
                "exam_id": exam_id,
                "state": "building",
                "questions": rows,
                "survey": survey_dict,
                "total_points": sum(q["frozen"]["meta"]["points"] for q in rows),
                "enrollment_mode": "open" if open_link else "roster",
            },
            warnings=["draft questions allowed: " + ", ".join(not_ready)] if not_ready else [],
        )
        return
    # A plain roster does not provide an EDSL AgentList/delivery map. Prepend
    # the specified identity question so response-to-roster joins remain fully
    # functional without relying on unavailable per-respondent links.
    edsl = __import__("edsl")
    identity = edsl.QuestionFreeText(
        question_name="pruefung_respondent_email",
        question_text="Your university email address",
    )
    survey = edsl.Survey([identity, *[restore_question(x["frozen"]["edsl"]) for x in rows]])
    survey_dict = survey.to_dict()
    receipt_path = path.parent / "deployment.json"
    receipt = read_json(receipt_path)
    if receipt:
        rows = receipt.get("questions", rows)
        survey_dict = receipt.get("survey", survey_dict)
        published = receipt["published"]
    else:
        try:
            result = survey.humanize(
                human_survey_name=exam_id,
                survey_visibility="private",
            )
            published = publication_record(result)
        except Exception as exc:
            if isinstance(exc, PruefungError):
                raise
            raise PruefungError(
                f"deployment failed before exam state changed: {exc}", code="network_error", exit_code=4
            ) from exc
        # This receipt is deliberately persisted before local finalization. If
        # a later write fails, retry resumes from the existing remote project.
        write_json(
            receipt_path,
            {
                "created_at": now(),
                "published": published,
                "questions": rows,
                "survey": survey_dict,
                "finalized": False,
            },
        )
    exam.pop("members", None)
    exam.update(
        state="deployed",
        deployed_at=now(),
        questions=rows,
        total_points=sum(q["frozen"]["meta"]["points"] for q in rows),
        published={
            **published,
            "links": [],
            "identity_mode": "identity_question",
            "enrollment_mode": "open" if open_link else "roster",
        },
    )
    write_json(path.parent / "survey.edsl.json", survey_dict)
    write_json(path, exam)
    for q in qs:
        q["meta"]["status"] = "deployed"
        write_json(state(root) / "questions" / f"{q['meta']['id']}.json", q)
    write_json(
        receipt_path,
        {
            "created_at": receipt.get("created_at", now()) if receipt else now(),
            "published": published,
            "questions": rows,
            "survey": survey_dict,
            "finalized": True,
        },
    )
    emit(ctx, {"exam_id": exam_id, "state": "deployed", "published": exam["published"]})


@exam_group.command("list")
@human_option
@click.pass_context
def exam_list(ctx: click.Context, human: bool) -> None:
    root = setup(ctx, "exam list", human)
    items = []
    for path in sorted((state(root) / "exams").glob("*/exam.json")):
        exam = read_json(path)
        stats = exam_stats(root, exam)
        items.append({k: stats[k] for k in ("exam_id", "state", "question_count", "total_points", "responses")})
    emit(ctx, {"exams": items})


@exam_group.command("rm")
@click.argument("exam_id")
@human_option
@click.pass_context
def exam_rm(ctx: click.Context, exam_id: str, human: bool) -> None:
    root = setup(ctx, "exam rm", human)
    path, exam = load_exam(root, exam_id)
    require_building(exam)
    shutil.rmtree(path.parent)
    emit(ctx, {"removed": exam_id})


def next_task(root: Path, prefix: str) -> str:
    existing = [p.name for p in (state(root) / "inference").iterdir() if p.is_dir()]
    number = 1
    while f"{prefix}_{number:02d}" in existing:
        number += 1
    return f"{prefix}_{number:02d}"


def write_runner(directory: Path, kind: str, models: list[str]) -> None:
    # The runner intentionally contains no pruefung import, so it can be audited and moved.
    script = f"""# Portable pruefung {kind} runner; models: {", ".join(models)}; calls depend on task size.
import argparse
import json
from pathlib import Path

from edsl import ModelList, ScenarioList, Survey


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Approve the paid model calls described by task.json")
    args = parser.parse_args()
    here = Path(__file__).parent
    task = json.loads((here / "task.json").read_text())
    if not args.yes:
        print(json.dumps({{"ok": False, "requires_approval": True, "kind": task["kind"], "models": task["models"], "estimated_calls": task.get("estimated_calls"), "next_command": "python " + str(Path(__file__)) + " --yes"}}))
        raise SystemExit(2)
    survey = Survey.from_dict(json.loads((here / "survey.edsl.json").read_text()))
    models = ModelList.from_dict(json.loads((here / "models.edsl.json").read_text()))
    if task["kind"] == "qc":
        results = survey.by(models).run()
        names = ["model.model"]
        for item in task["qc_items"]:
            names.extend(["answer." + item["answer_name"], "answer." + item["review_name"]])
        rows = results.select(*names).to_dicts()
        questions = {{}}
        for item in task["qc_items"]:
            reviews = [row.get(item["review_name"]) or {{}} for row in rows]
            verdict = {{
                "panel_answers": [row.get(item["answer_name"]) for row in rows],
                "blocking": [review.get("blocking") for review in reviews],
                "notes": [review.get("notes") for review in reviews],
                "models": [row.get("model") for row in rows],
            }}
            if item["ptype"] == "free_text":
                verdict["rubric_scores"] = [review.get("rubric_score") for review in reviews]
                verdict["rubric_clear"] = [review.get("rubric_clear") for review in reviews]
            questions[item["qid"]] = verdict
        payload = {{"questions": questions}}
    else:
        scenarios = ScenarioList.from_dict(json.loads((here / "scenarios.edsl.json").read_text()))
        results = survey.by(scenarios).by(models).run()
        rows = results.select("model.model", "scenario.*", "answer.*").to_dicts()
        if task["kind"] == "concept_suggest":
            suggestions = []
            for row in rows:
                value = row.get("suggestions", [])
                if isinstance(value, list):
                    suggestions.extend(value)
            payload = {{"suggestions": suggestions, "rows": rows}}
        else:
            payload = {{"rows": rows}}
    envelope = {{"task_id": task["task_id"], "kind": task["kind"], "input_hashes": task["input_hashes"], "created_at": task["created_at"], "payload": payload}}
    (here / "result.json").write_text(json.dumps(envelope, indent=2) + "\\n")


if __name__ == "__main__":
    main()
"""
    (directory / "run.py").write_text(script, encoding="utf-8")


def make_task(
    root: Path,
    kind: str,
    prefix: str,
    hashes: dict[str, str],
    scenarios: list[dict[str, Any]],
    models: list[str],
    survey: Any,
    force: bool = False,
    task_extra: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    for directory in (state(root) / "inference").iterdir():
        task = read_json(directory / "task.json") if directory.is_dir() else None
        if (
            task
            and task.get("kind") == kind
            and task.get("input_hashes") == hashes
            and task.get("models") == models
            and task.get("status") == "pending"
            and not force
        ):
            return task["task_id"], False
    edsl = __import__("edsl")
    if len(models) != len(set(models)):
        raise ValidationError("model panel must contain distinct model names")
    try:
        model_list = edsl.ModelList([edsl.Model(model) for model in models])
    except Exception as exc:
        raise ValidationError(f"invalid model panel: {exc}") from exc
    scenario_list = (
        edsl.ScenarioList.from_list("input", scenarios)
        if hasattr(edsl.ScenarioList, "from_list")
        else edsl.ScenarioList([edsl.Scenario(input=x) for x in scenarios])
    )
    task_id = next_task(root, prefix)
    directory = state(root) / "inference" / task_id
    directory.mkdir()
    estimated_calls = len(models) * max(1, len(scenarios)) * len(survey.questions)
    task = {
        "task_id": task_id,
        "kind": kind,
        "input_hashes": hashes,
        "created_at": now(),
        "status": "pending",
        "models": models,
        "estimated_calls": estimated_calls,
        **(task_extra or {}),
    }
    write_json(directory / "task.json", task)
    write_json(directory / "survey.edsl.json", survey.to_dict())
    write_json(directory / "scenarios.edsl.json", scenario_list.to_dict())
    write_json(directory / "models.edsl.json", model_list.to_dict())
    write_runner(directory, kind, models)
    return task_id, True


@cli.group("qc")
def qc_group() -> None:
    """Create and ingest blind-solve QC panels."""


@qc_group.command("make")
@click.option("--only")
@click.option("--models", default="gpt-4o,gpt-4.1-mini,gemini-2.5-flash")
@click.option("--force", is_flag=True)
@human_option
@click.pass_context
def qc_make(ctx: click.Context, only: str | None, models: str, force: bool, human: bool) -> None:
    root = setup(ctx, "qc make", human)
    selected = set(only.split(",")) if only else None
    questions = [
        read_json(p)
        for p in question_files(root)
        if (selected and p.stem in selected) or (not selected and read_json(p)["meta"]["status"] == "draft")
    ]
    if not questions:
        raise ValidationError("no questions in QC scope")
    edsl = __import__("edsl")
    survey_questions = []
    qc_items = []
    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    renders = {
        render["name"]: render
        for source in manifest.get("sources", {}).values()
        for render in source.get("renders", [])
    }
    material_hashes = {}
    for q in questions:
        qid = q["meta"]["id"]
        answer_question = restore_question(q["edsl"])
        survey_questions.append(answer_question)
        material_text = []
        for name in q["meta"].get("source_materials", []):
            render = renders[name]
            material_hashes[f"material:{name}"] = render["hash"]
            material_text.append((state(root) / render["path"]).read_text(encoding="utf-8"))
        context = "\n\nCourse material excerpts:\n" + "\n\n".join(material_text) if material_text else ""
        review_name = f"qc_review_{qid}"
        rendered_options = "\n".join(
            f"[{index}] {option}" for index, option in enumerate(q["edsl"].get("question_options", []))
        )
        review_text = (
            f"Review this question:\n{q['edsl']['question_text']}\n{rendered_options}\n\n"
            "Does this question have a blocking defect such as ambiguity, answer cues, a likely miskey, or reliance on knowledge outside the supplied course material?"
            + context
        )
        answer_keys = ["blocking", "notes"]
        value_types = ["bool", "str"]
        value_descriptions = [
            "True only when a specific defect prevents fair use of the question; otherwise false.",
            "A brief, question-specific explanation of the decision.",
        ]
        item = {
            "qid": qid,
            "ptype": q["meta"]["ptype"],
            "answer_name": q["edsl"]["question_name"],
            "review_name": review_name,
        }
        if q["meta"]["ptype"] == "free_text":
            answer_keys.extend(["rubric_score", "rubric_clear"])
            value_types.extend(["float", "bool"])
            value_descriptions.extend(
                [
                    f"Score your answer from 0 to {q['meta']['points']} using this rubric: {q['meta']['rubric']}",
                    "True when the rubric is unambiguous and covers answers that deserve credit.",
                ]
            )
        survey_questions.append(
            edsl.QuestionDict(
                question_name=review_name,
                question_text=review_text,
                answer_keys=answer_keys,
                value_types=value_types,
                value_descriptions=value_descriptions,
            )
        )
        qc_items.append(item)
    survey = edsl.Survey(survey_questions)
    hashes = {q["meta"]["id"]: q["meta"]["content_hash"] for q in questions} | material_hashes
    model_names = [name.strip() for name in models.split(",") if name.strip()]
    task_id, created = make_task(
        root, "qc", "qc", hashes, [], model_names, survey, force, task_extra={"qc_items": qc_items}
    )
    emit(
        ctx,
        {
            "task_id": task_id,
            "created": created,
            "models": model_names,
            "question_count": len(questions),
            "estimated_calls": len(model_names) * len(survey_questions),
            "run": f"python .pruefung/inference/{task_id}/run.py --yes",
            "requires_approval": True,
        },
    )


def validate_result(root: Path, task_ref: str, kind: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    result_path = Path(task_ref)
    if not result_path.exists():
        result_path = state(root) / "inference" / task_ref / "result.json"
    result = read_json(result_path)
    task_id = result.get("task_id") if result else task_ref
    task_path = state(root) / "inference" / task_id / "task.json"
    task = read_json(task_path)
    if not result or not task or result.get("kind") != kind or task.get("kind") != kind:
        raise ValidationError("result envelope kind/task is invalid")
    if result.get("input_hashes") != task.get("input_hashes"):
        raise ConflictError("result input_hashes do not exactly match the task manifest")
    if task.get("status") == "ingested":
        return task_path, task, result
    changed = []
    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    material_hashes = {
        f"material:{render['name']}": render["hash"]
        for source in manifest.get("sources", {}).values()
        for render in source.get("renders", [])
    }
    for object_id, expected in result.get("input_hashes", {}).items():
        if object_id.startswith("q") and (state(root) / "questions" / f"{object_id}.json").exists():
            current = load_question(root, object_id)["meta"]["content_hash"]
        elif object_id.startswith("material:"):
            current = material_hashes.get(object_id)
        elif object_id.startswith("response:") and task.get("exam_id"):
            response_input = task.get("response_inputs", {}).get(object_id)
            if response_input:
                email, question_name = response_input["email"], response_input["question_name"]
            else:
                _, email, question_name = object_id.split(":", 2)
            _, exam = load_exam(root, task["exam_id"])
            response = next(
                (
                    row
                    for row in read_json(state(root) / "exams" / task["exam_id"] / "responses.json", [])
                    if row.get("email") == email
                ),
                None,
            )
            question_row = next(
                (row for row in exam.get("questions", []) if row["question_name"] == question_name), None
            )
            current = (
                canonical_hash(
                    {
                        "question": question_row["frozen"]["edsl"],
                        "rubric": question_row["frozen"]["meta"]["rubric"],
                        "answer": response_answer(response, question_name),
                    }
                )
                if response and question_row
                else None
            )
        else:
            current = task["input_hashes"].get(object_id)
        if current != expected:
            changed.append(object_id)
    if changed:
        task.update(status="stale", stale_at=now(), changed_objects=changed)
        write_json(task_path, task)
        raise ConflictError(f"stale result; changed objects: {', '.join(changed)}")
    return task_path, task, result


@qc_group.command("ingest")
@click.argument("task_ref")
@human_option
@click.pass_context
def qc_ingest(ctx: click.Context, task_ref: str, human: bool) -> None:
    root = setup(ctx, "qc ingest", human)
    task_path, task, result = validate_result(root, task_ref, "qc")
    if task["status"] == "ingested":
        emit(ctx, {"task_id": task["task_id"], "ingested": False}, warnings=["result already ingested"])
        return
    payload = result.get("payload", {})
    verdicts = payload.get("questions")
    expected_qids = [key for key in task["input_hashes"] if key.startswith("q")]
    if not isinstance(verdicts, dict):
        raise ValidationError("QC result payload must contain a questions object; raw EDSL results cannot be ingested")
    missing = set(expected_qids) - set(verdicts)
    if missing:
        raise ValidationError(f"QC result is missing verdicts for: {', '.join(sorted(missing))}")
    updated = []
    for qid in expected_qids:
        q = load_question(root, qid)
        verdict = verdicts[qid]
        if (
            not isinstance(verdict, dict)
            or not isinstance(verdict.get("panel_answers"), list)
            or not isinstance(verdict.get("blocking"), list)
        ):
            raise ValidationError(f"QC verdict for {qid} requires panel_answers and blocking arrays")
        answers = [
            normalize_answer(value, q["meta"]["ptype"], q["edsl"].get("question_options", []))
            for value in verdict["panel_answers"]
        ]
        blocking = [normalize_answer(value, "true_false", []) for value in verdict.get("blocking", [])]
        blocking_votes = sum(value is True for value in blocking)
        blocking_majority = blocking_votes > len(blocking) / 2
        if q["meta"]["ptype"] == "free_text":
            scores = [float(value) for value in verdict.get("rubric_scores", []) if value is not None]
            clarity = [normalize_answer(value, "true_false", []) for value in verdict.get("rubric_clear", [])]
            passed = bool(
                scores
                and statistics.mean(scores) >= (2 * q["meta"]["points"] / 3)
                and clarity
                and sum(value is True for value in clarity) > len(clarity) / 2
                and blocking
                and not blocking_majority
            )
        else:
            matches = sum(a == q["meta"].get("answer") for a in answers)
            answer_majority = matches > len(answers) / 2
            passed = answer_majority and blocking and not blocking_majority
        verdict["normalized_answers"] = answers
        verdict["normalized_blocking"] = blocking
        verdict["blocking_votes"] = blocking_votes
        verdict["blocking_majority"] = blocking_majority
        q["meta"].update(
            status="qc_passed" if passed else "qc_failed",
            qc={"task_id": task["task_id"], "passed": bool(passed), "notes": verdict},
            updated_at=now(),
        )
        write_json(state(root) / "questions" / f"{qid}.json", q)
        updated.append({"id": qid, "status": q["meta"]["status"]})
    task["status"] = "ingested"
    task["ingested_at"] = now()
    write_json(task_path, task)
    emit(ctx, {"task_id": task["task_id"], "updated": updated})


@qc_group.command("show")
@click.argument("task_ref")
@human_option
@click.pass_context
def qc_show(ctx: click.Context, task_ref: str, human: bool) -> None:
    root = setup(ctx, "qc show", human)
    directory = state(root) / "inference" / task_ref
    task = read_json(directory / "task.json")
    if not task or task.get("kind") != "qc":
        raise ValidationError(f"unknown QC task: {task_ref}")
    result = read_json(directory / "result.json")
    rows = []
    verdicts = (result or {}).get("payload", {}).get("questions", {})
    for qid in [key for key in task["input_hashes"] if key.startswith("q")]:
        question = load_question(root, qid)
        rows.append(
            {
                "id": qid,
                "status": question["meta"]["status"],
                "expected": question["meta"].get("answer"),
                "verdict": verdicts.get(qid),
            }
        )
    emit(ctx, {"task": task, "questions": rows, "result_available": result is not None})


@qc_group.command("report")
@click.argument("task_ref")
@click.option("--question", "question_ids", multiple=True, help="Limit the report to a question ID.")
@click.option("--status", type=click.Choice(["draft", "qc_passed", "qc_failed", "deployed"]))
@click.option("--blocking-only", is_flag=True, help="Show only questions with a blocking panel vote.")
@human_option
@click.pass_context
def qc_report(
    ctx: click.Context,
    task_ref: str,
    question_ids: tuple[str, ...],
    status: str | None,
    blocking_only: bool,
    human: bool,
) -> None:
    root = setup(ctx, "qc report", human)
    directory = state(root) / "inference" / task_ref
    task = read_json(directory / "task.json")
    if not task or task.get("kind") != "qc":
        raise ValidationError(f"unknown QC task: {task_ref}")
    verdicts = (read_json(directory / "result.json") or {}).get("payload", {}).get("questions", {})
    rows = []
    for qid in [key for key in task["input_hashes"] if key.startswith("q")]:
        question = load_question(root, qid)
        verdict = verdicts.get(qid, {})
        normalized_blocking = [normalize_answer(value, "true_false", []) for value in verdict.get("blocking", [])]
        row = {
            "id": qid,
            "status": question["meta"]["status"],
            "expected": question["meta"].get("answer"),
            "panel_answers": verdict.get("panel_answers", []),
            "blocking": normalized_blocking,
            "notes": verdict.get("notes", []),
            "qc": question["meta"].get("qc"),
        }
        if question_ids and qid not in question_ids:
            continue
        if status and row["status"] != status:
            continue
        if blocking_only and not any(value is True for value in normalized_blocking):
            continue
        rows.append(row)
    if human:
        for row in rows:
            click.echo(f"{row['id']}: {row['status']} (expected: {row['expected']})")
            click.echo(f"  answers: {row['panel_answers']}")
            click.echo(f"  blocking: {row['blocking']}")
            click.echo(f"  notes: {row['notes']}")
        return
    emit(ctx, {"task_id": task_ref, "questions": rows})


@qc_group.command("override")
@click.argument("qid")
@click.option("--decision", type=click.Choice(["pass", "fail"]), required=True)
@click.option("--reason", required=True, help="Professor or reviewer rationale.")
@click.option("--reviewer", default="professor", show_default=True)
@click.option("--professor-approved", is_flag=True, required=True, help="Confirm explicit professor approval.")
@human_option
@click.pass_context
def qc_override(
    ctx: click.Context,
    qid: str,
    decision: str,
    reason: str,
    reviewer: str,
    professor_approved: bool,
    human: bool,
) -> None:
    root = setup(ctx, "qc override", human)
    question = load_question(root, qid)
    if question["meta"]["status"] == "deployed":
        raise ConflictError("cannot override QC after deployment")
    override = {
        "passed": decision == "pass",
        "reason": reason,
        "reviewer": reviewer,
        "professor_approved": professor_approved,
        "at": now(),
    }
    qc = question["meta"].setdefault("qc", {})
    qc.setdefault("overrides", []).append(override)
    qc["override"] = override
    qc["passed"] = override["passed"]
    question["meta"].update(status="qc_passed" if override["passed"] else "qc_failed", updated_at=now())
    write_json(state(root) / "questions" / f"{qid}.json", question)
    emit(ctx, {"id": qid, "status": question["meta"]["status"], "override": override})


@cli.command("inference")
@human_option
@click.pass_context
def inference_cmd(ctx: click.Context, human: bool) -> None:
    root = setup(ctx, "inference", human)
    tasks = []
    manifest = read_json(state(root) / "materials/manifest.json", {"sources": {}})
    material_hashes = {
        f"material:{render['name']}": render["hash"]
        for source in manifest.get("sources", {}).values()
        for render in source.get("renders", [])
    }
    for directory in sorted((state(root) / "inference").iterdir()):
        task = read_json(directory / "task.json") if directory.is_dir() else None
        if task:
            stale = any(
                (
                    object_id.startswith("q")
                    and (state(root) / "questions" / f"{object_id}.json").exists()
                    and load_question(root, object_id)["meta"]["content_hash"] != digest
                )
                or (object_id.startswith("material:") and material_hashes.get(object_id) != digest)
                for object_id, digest in task["input_hashes"].items()
            )
            tasks.append({**task, "status": "stale" if stale and task["status"] == "pending" else task["status"]})
    emit(ctx, {"tasks": tasks})


def response_answer(response: dict[str, Any], name: str) -> Any:
    answers = response.get("answers", response.get("answer", {}))
    return answers.get(name) if isinstance(answers, dict) else None


def deterministic_grade(
    exam: dict[str, Any], responses: list[dict[str, Any]], existing: dict[str, Any] | None, rescore: bool
) -> dict[str, Any]:
    old_by_email = {x["email"]: x for x in (existing or {}).get("students", [])}
    students = []
    unmatched = [response for response in responses if not response.get("email")]
    latest_by_email: dict[str, dict[str, Any]] = {}
    discarded: list[dict[str, Any]] = []
    for response in responses:
        email = str(response.get("email") or "").lower()
        if not email:
            continue
        if email in latest_by_email:
            discarded.append(latest_by_email[email])
        latest_by_email[email] = response
    for response in latest_by_email.values():
        email = (response.get("email") or response.get("identifier") or "").lower()
        old = old_by_email.get(email, {})
        items = []
        for row in exam["questions"]:
            q, name = row["frozen"], row["question_name"]
            previous = next((x for x in old.get("items", []) if x["question_name"] == name), None)
            if previous and previous.get("override"):
                items.append(previous)
                continue
            if previous and previous.get("score") is not None and not rescore:
                items.append(previous)
                continue
            observed = response_answer(response, name)
            meta = q["meta"]
            ptype = meta["ptype"]
            observed = normalize_answer(observed, ptype, q["edsl"].get("question_options", []))
            if ptype == "free_text":
                item = {
                    "question_name": name,
                    "bank_id": row["bank_id"],
                    "answer": observed,
                    "score": previous.get("score") if previous else None,
                    "max_points": meta["points"],
                    "needs_review": not bool(previous and previous.get("score") is not None),
                }
            elif ptype == "checkbox":
                item = {
                    "question_name": name,
                    "bank_id": row["bank_id"],
                    "answer": observed,
                    "score": score_checkbox(
                        meta["answer"], observed or [], meta["points"], meta.get("partial_credit", "none")
                    ),
                    "max_points": meta["points"],
                }
            else:
                item = {
                    "question_name": name,
                    "bank_id": row["bank_id"],
                    "answer": observed,
                    "score": meta["points"] if observed == meta["answer"] else 0,
                    "max_points": meta["points"],
                }
            items.append(item)
        students.append(
            {
                "email": email,
                "name": response.get("name", ""),
                "items": items,
                "score": sum(x["score"] or 0 for x in items),
                "total_points": exam["total_points"],
            }
        )
    return {
        "exam_id": exam["exam_id"],
        "updated_at": now(),
        "students": students,
        "unmatched_responses": unmatched,
        "discarded_duplicate_responses": [row.get("response_id") for row in discarded],
    }


@cli.command("grade")
@click.argument("exam_id")
@click.option("--rescore", is_flag=True)
@human_option
@click.pass_context
def grade_cmd(ctx: click.Context, exam_id: str, rescore: bool, human: bool) -> None:
    root = setup(ctx, "grade", human)
    _, exam = load_exam(root, exam_id)
    if exam["state"] != "deployed":
        raise ConflictError("cannot grade a building exam")
    responses = sync_responses(state(root) / "exams" / exam_id, exam)
    gb_path = state(root) / "gradebooks" / f"{exam_id}.gradebook.json"
    gradebook = deterministic_grade(exam, responses, read_json(gb_path), rescore)
    write_json(gb_path, gradebook)
    csv_path = state(root) / "gradebooks" / f"{exam_id}.gradebook.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["email", "name", "score", "total_points"])
        writer.writerows([[x["email"], x["name"], x["score"], x["total_points"]] for x in gradebook["students"]])
    scores = [x["score"] for x in gradebook["students"]]
    summary = {
        "count": len(scores),
        "mean": statistics.mean(scores) if scores else None,
        "median": statistics.median(scores) if scores else None,
        "range": [min(scores), max(scores)] if scores else None,
    }
    pending = sum(x.get("needs_review", False) for s in gradebook["students"] for x in s["items"])
    emit(
        ctx,
        {
            "gradebook": str(gb_path),
            "csv": str(csv_path),
            "summary": summary,
            "ungraded_free_text": pending,
            "next_commands": [f"pruefung grade-make {exam_id}"] if pending else [],
        },
    )


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    xbar, ybar = statistics.mean(xs), statistics.mean(ys)
    numerator = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    denominator = (sum((x - xbar) ** 2 for x in xs) * sum((y - ybar) ** 2 for y in ys)) ** 0.5
    return numerator / denominator if denominator else None


def display_answer(value: Any, ptype: str, options: list[str]) -> str:
    if value is None:
        return "No response"
    if ptype == "true_false":
        return "True" if value is True else "False"
    if ptype == "mcq" and isinstance(value, int) and 0 <= value < len(options):
        return options[value]
    if ptype == "checkbox" and isinstance(value, list):
        return ", ".join(options[index] for index in value if isinstance(index, int) and 0 <= index < len(options))
    return str(value)


def grade_report_data(exam: dict[str, Any], gradebook: dict[str, Any], anonymize: bool) -> dict[str, Any]:
    students = gradebook.get("students", [])
    scores = [float(student["score"]) for student in students]
    public_students = []
    for index, student in enumerate(students, start=1):
        public_students.append(
            {
                "student": f"student-{index:03d}" if anonymize else student["email"],
                "name": "" if anonymize else student.get("name", ""),
                "score": student["score"],
                "total_points": student["total_points"],
            }
        )
    items = []
    concepts: dict[str, list[float]] = defaultdict(list)
    for row in exam.get("questions", []):
        qid, qname, frozen = row["bank_id"], row["question_name"], row["frozen"]
        student_items = [next((item for item in s["items"] if item["question_name"] == qname), {}) for s in students]
        ratios = [float(item.get("score") or 0) / float(item.get("max_points") or 1) for item in student_items]
        concepts[frozen["meta"]["concept"]].extend(ratios)
        ptype = frozen["meta"]["ptype"]
        options = frozen["edsl"].get("question_options", [])
        answer_counts = Counter(display_answer(item.get("answer"), ptype, options) for item in student_items)
        score_counts = Counter(str(item.get("score")) for item in student_items)
        correct_answer = (
            frozen["meta"].get("rubric")
            if ptype == "free_text"
            else display_answer(frozen["meta"].get("answer"), ptype, options)
        )
        items.append(
            {
                "id": qid,
                "question_name": qname,
                "text": frozen["edsl"]["question_text"],
                "options": options,
                "concept": frozen["meta"]["concept"],
                "type": ptype,
                "points": frozen["meta"]["points"],
                "correct_answer": correct_answer,
                "explanation": frozen["meta"].get("explanation") or "No explanation was provided.",
                "mean_proportion": statistics.mean(ratios) if ratios else None,
                "score_total_correlation": correlation(ratios, scores),
                "answer_counts": dict(answer_counts) if ptype != "free_text" else None,
                "score_counts": dict(score_counts),
                "responses": len(student_items),
            }
        )
    return {
        "exam_id": exam["exam_id"],
        "generated_at": now(),
        "anonymized": anonymize,
        "summary": {
            "count": len(scores),
            "mean": statistics.mean(scores) if scores else None,
            "median": statistics.median(scores) if scores else None,
            "range": [min(scores), max(scores)] if scores else None,
        },
        "students": public_students,
        "items": items,
        "concepts": {key: {"mean_proportion": statistics.mean(values)} for key, values in concepts.items()},
    }


def render_grade_report_html(report: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value))

    question_cards = []
    for item in report["items"]:
        distribution = item["answer_counts"] or item["score_counts"]
        maximum = max(distribution.values(), default=1)
        bars = "".join(
            f'<div class="bar-row"><span>{esc(label)}</span><div class="track"><i style="width:{100 * count / maximum:.1f}%"></i></div><b>{count}</b></div>'
            for label, count in distribution.items()
        )
        options = ""
        if item["options"]:
            options = "<ol>" + "".join(f"<li>{esc(option)}</li>" for option in item["options"]) + "</ol>"
        performance = (
            "No scored responses"
            if item["mean_proportion"] is None
            else f"{100 * item['mean_proportion']:.1f}% of available points earned"
        )
        label = "Rubric" if item["type"] == "free_text" else "Correct answer"
        distribution_label = "Score distribution" if item["type"] == "free_text" else "Answer distribution"
        question_cards.append(
            f"""<section class="question"><div class="kicker">{esc(item["id"])} · {esc(item["concept"])} · {esc(item["points"])} point(s)</div>
<h2>{esc(item["text"])}</h2>{options}<div class="performance">{esc(performance)}</div>
<h3>{distribution_label}</h3>{bars or "<p>No responses.</p>"}
<div class="answer"><strong>{label}:</strong> {esc(item["correct_answer"])}</div>
<div class="explanation"><strong>Explanation:</strong> {esc(item["explanation"])}</div></section>"""
        )
    summary = report["summary"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(report["exam_id"])} post-exam report</title><style>
:root{{--green:#4f812f;--dark:#274119;--pale:#edf5e8;--ink:#192116;--muted:#667061;--line:#dbe4d7}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f1;color:var(--ink);font:16px/1.55 system-ui,sans-serif}}main{{max-width:920px;margin:auto;padding:44px 24px}}header,.question{{background:white;border:1px solid var(--line);border-radius:12px;padding:28px;margin-bottom:22px}}.brand-row{{display:flex;align-items:baseline;justify-content:space-between;gap:18px;padding-bottom:12px;margin-bottom:22px;border-bottom:3px solid var(--green)}}.brand{{color:var(--green);font:600 .95rem Georgia,serif;text-decoration:none}}h1,h2{{font-family:Georgia,serif}}h1{{margin:.2em 0}}h2{{font-size:1.4rem}}h3{{margin-bottom:8px}}.kicker{{color:var(--green);font-weight:750;text-transform:uppercase;font-size:.78rem;letter-spacing:.08em}}.summary{{color:var(--muted)}}.performance,.answer,.explanation{{padding:13px 15px;margin:14px 0;background:var(--pale);border-radius:7px}}.bar-row{{display:grid;grid-template-columns:minmax(120px,2fr) 5fr 35px;gap:10px;align-items:center;margin:8px 0}}.track{{height:15px;background:#e5e9e2;border-radius:20px;overflow:hidden}}.track i{{display:block;height:100%;background:var(--green)}}ol{{padding-left:24px}}footer{{margin-top:36px;padding:18px 0;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem;text-align:center}}footer a{{color:var(--green)}}@media(max-width:600px){{.bar-row{{grid-template-columns:1fr 2fr 28px}}.brand-row{{display:block}}}}</style></head>
<body><main><header><div class="brand-row"><div class="kicker">Pruefung · Post-exam report</div><a class="brand" href="https://www.expectedparrot.com/">E[&#x1f99c;] Expected Parrot</a></div><h1>{esc(report["exam_id"])}</h1><div class="summary">{summary["count"]} graded response(s) · Mean {esc(summary["mean"])} · Median {esc(summary["median"])}</div></header>{"".join(question_cards)}<footer>Generated {esc(report["generated_at"])} by <a href="https://github.com/expectedparrot/pruefung">Pruefung</a> · Expected Parrot</footer></main></body></html>"""


@cli.command("grade-report")
@click.argument("exam_id")
@click.option("--anonymize", is_flag=True, help="Replace student identities with stable labels.")
@click.option("--student", help="Limit the student table to one email address.")
@click.option("--html", "html_path", type=click.Path(path_type=Path), help="Write a self-contained HTML report.")
@human_option
@click.pass_context
def grade_report_cmd(
    ctx: click.Context,
    exam_id: str,
    anonymize: bool,
    student: str | None,
    html_path: Path | None,
    human: bool,
) -> None:
    root = setup(ctx, "grade report", human)
    _, exam = load_exam(root, exam_id)
    gradebook = read_json(state(root) / "gradebooks" / f"{exam_id}.gradebook.json")
    if not gradebook:
        raise ValidationError(f"run `pruefung grade {exam_id}` first")
    if student:
        selected = [row for row in gradebook.get("students", []) if row["email"].lower() == student.lower()]
        if not selected:
            raise ValidationError(f"student not found in gradebook: {student}")
        gradebook = {**gradebook, "students": selected}
    report = grade_report_data(exam, gradebook, anonymize)
    if html_path:
        html_path = html_path.resolve()
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_grade_report_html(report), encoding="utf-8")
        report["html"] = str(html_path)
    if human:
        click.echo(f"{exam_id}: {report['summary']['count']} graded response(s)")
        click.echo(f"Mean: {report['summary']['mean']}  Median: {report['summary']['median']}")
        for item in report["items"]:
            click.echo(
                f"{item['id']} ({item['concept']}): mean={item['mean_proportion']} correlation={item['score_total_correlation']}"
            )
        return
    emit(ctx, report)


@cli.command("post-exam-report")
@click.argument("exam_id")
@click.option("--output", type=click.Path(path_type=Path), help="HTML path; defaults inside .pruefung/reports.")
@human_option
@click.pass_context
def post_exam_report_cmd(ctx: click.Context, exam_id: str, output: Path | None, human: bool) -> None:
    root = setup(ctx, "post-exam report", human)
    _, exam = load_exam(root, exam_id)
    gradebook = read_json(state(root) / "gradebooks" / f"{exam_id}.gradebook.json")
    if not gradebook:
        raise ValidationError(f"run `pruefung grade {exam_id}` first")
    output = (output or state(root) / "reports" / f"{exam_id}.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = grade_report_data(exam, gradebook, anonymize=True)
    output.write_text(render_grade_report_html(report), encoding="utf-8")
    emit(ctx, {"exam_id": exam_id, "html": str(output), "questions": len(report["items"])})


@cli.command("status")
@click.argument("exam_id")
@human_option
@click.pass_context
def status_cmd(ctx: click.Context, exam_id: str, human: bool) -> None:
    root = setup(ctx, "status", human)
    _, exam = load_exam(root, exam_id)
    if exam["state"] != "deployed":
        raise ConflictError("status is available only after deploy")
    responses = sync_responses(state(root) / "exams" / exam_id, exam)
    responded = {
        (x.get("email") or x.get("identifier") or "").lower()
        for x in responses
        if x.get("email") or x.get("identifier")
    }
    roster = {x["email"].lower() for x in exam.get("roster", [])}
    open_enrollment = exam.get("published", {}).get("enrollment_mode") == "open"
    emit(
        ctx,
        {
            "exam_id": exam_id,
            "responded": len(responded) if open_enrollment else len(responded & roster),
            "roster": len(roster),
            "missing": [] if open_enrollment else sorted(roster - responded),
            "unmatched": [] if open_enrollment else sorted(responded - roster),
            "enrollment_mode": "open" if open_enrollment else "roster",
        },
    )


@cli.command("grade-make")
@click.argument("exam_id")
@click.option("--models", default="gpt-4o,gpt-4.1-mini")
@click.option("--force", is_flag=True)
@human_option
@click.pass_context
def grade_make_cmd(ctx: click.Context, exam_id: str, models: str, force: bool, human: bool) -> None:
    root = setup(ctx, "grade make", human)
    _, exam = load_exam(root, exam_id)
    responses = sync_responses(state(root) / "exams" / exam_id, exam)
    scenarios, hashes, response_inputs = [], {}, {}
    for response in responses:
        answer_id = response.get("email") or response.get("identifier", "")
        for row in exam.get("questions", []):
            q = row["frozen"]
            if q["meta"]["ptype"] == "free_text":
                aid = f"{answer_id}:{row['question_name']}"
                object_id = f"response:{canonical_hash(aid)[:20]}"
                hashes[object_id] = canonical_hash(
                    {
                        "question": q["edsl"],
                        "rubric": q["meta"]["rubric"],
                        "answer": response_answer(response, row["question_name"]),
                    }
                )
                response_inputs[object_id] = {"email": answer_id, "question_name": row["question_name"]}
                scenarios.append(
                    {
                        "answer_id": aid,
                        "question": q["edsl"]["question_text"],
                        "rubric": q["meta"]["rubric"],
                        "points": q["meta"]["points"],
                        "answer": response_answer(response, row["question_name"]),
                    }
                )
    if not scenarios:
        raise ValidationError("no free-text answers require grading")
    edsl = __import__("edsl")
    survey = edsl.Survey(
        [
            edsl.QuestionNumerical(
                question_name="score",
                question_text="Score the student answer using the question, rubric, and point maximum in {{ input }}",
                min_value=0,
                max_value=max(float(item["points"]) for item in scenarios),
            ),
            edsl.QuestionFreeText(
                question_name="justification", question_text="Justify the score by citing rubric criteria."
            ),
        ]
    )
    task_id, created = make_task(
        root,
        "rubric_grade",
        f"grade_{exam_id}",
        hashes,
        scenarios,
        [name.strip() for name in models.split(",") if name.strip()],
        survey,
        force,
        task_extra={"exam_id": exam_id, "response_inputs": response_inputs},
    )
    emit(
        ctx,
        {
            "task_id": task_id,
            "created": created,
            "run": f"python .pruefung/inference/{task_id}/run.py --yes",
            "requires_approval": True,
        },
    )


@cli.command("grade-ingest")
@click.argument("exam_id")
@click.argument("task_ref")
@human_option
@click.pass_context
def grade_ingest_cmd(ctx: click.Context, exam_id: str, task_ref: str, human: bool) -> None:
    root = setup(ctx, "grade ingest", human)
    task_path, task, result = validate_result(root, task_ref, "rubric_grade")
    if task.get("exam_id") != exam_id:
        raise ValidationError(f"task {task['task_id']} belongs to exam {task.get('exam_id')}")
    if task["status"] == "ingested":
        emit(ctx, {"task_id": task["task_id"], "ingested": False}, warnings=["result already ingested"])
        return
    rows = result.get("payload", {}).get("rows")
    if not isinstance(rows, list):
        raise ValidationError("rubric result payload requires a rows array")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scenario = row.get("input", row.get("scenario", {}))
        answer_id = scenario.get("answer_id") if isinstance(scenario, dict) else row.get("answer_id")
        if not answer_id or row.get("score") is None or row.get("justification") is None:
            raise ValidationError("every rubric result row requires answer_id, score, and justification")
        grouped[str(answer_id)].append(row)
    gb_path = state(root) / "gradebooks" / f"{exam_id}.gradebook.json"
    gradebook = read_json(gb_path)
    if not gradebook:
        raise ValidationError(f"run `pruefung grade {exam_id}` before ingesting rubric grades")
    updated = []
    for student in gradebook["students"]:
        for item in student["items"]:
            answer_id = f"{student['email']}:{item['question_name']}"
            verdicts = grouped.get(answer_id)
            if not verdicts or item.get("score") is not None or item.get("override"):
                continue
            scores = [float(row["score"]) for row in verdicts]
            if any(score < 0 or score > float(item["max_points"]) for score in scores):
                raise ValidationError(f"rubric score outside 0-{item['max_points']} for {answer_id}")
            threshold = float(item["max_points"]) * 0.25
            item["panel"] = [
                {"model": row.get("model"), "score": float(row["score"]), "justification": row["justification"]}
                for row in verdicts
            ]
            if max(scores) - min(scores) <= threshold:
                item["score"] = statistics.mean(scores)
                item["needs_review"] = False
            else:
                item["needs_review"] = True
            updated.append(answer_id)
        student["score"] = sum(item.get("score") or 0 for item in student["items"])
    write_json(gb_path, gradebook)
    csv_path = state(root) / "gradebooks" / f"{exam_id}.gradebook.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["email", "name", "score", "total_points"])
        writer.writerows(
            [[row["email"], row["name"], row["score"], row["total_points"]] for row in gradebook["students"]]
        )
    task.update(status="ingested", ingested_at=now())
    write_json(task_path, task)
    emit(ctx, {"task_id": task["task_id"], "updated": updated})


SCHEMAS = {
    "concept": (
        {
            "type": "object",
            "required": ["id", "added_at"],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                "note": {"type": "string"},
                "added_at": {"type": "string"},
            },
        },
        {"id": "confidence_intervals", "note": "weeks 5-6", "added_at": "2026-08-15T14:02:11Z"},
    ),
    "roster": (
        {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["email"],
                "properties": {
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "student_id": {"type": "string"},
                },
            },
        },
        [{"email": "student@example.edu", "name": "Ada", "student_id": "1"}],
    ),
    "result": (
        {"type": "object", "required": ["task_id", "kind", "input_hashes", "created_at", "payload"]},
        {
            "task_id": "qc_01",
            "kind": "qc",
            "input_hashes": {"q001": "sha256..."},
            "created_at": "2026-08-15T14:02:11Z",
            "payload": {},
        },
    ),
    "exam": (
        {"type": "object", "required": ["exam_id", "title", "state", "created_at"]},
        {
            "exam_id": "quiz-1",
            "title": "Quiz 1",
            "state": "building",
            "created_at": "2026-08-15T14:02:11Z",
            "members": [],
            "roster": [],
        },
    ),
    "question": (
        {
            "type": "object",
            "required": ["edsl", "meta"],
            "properties": {
                "edsl": {"type": "object"},
                "meta": {"type": "object", "required": ["id", "ptype", "concept", "points", "status", "content_hash"]},
            },
        },
        {
            "edsl": {
                "question_name": "ci",
                "question_text": "...",
                "question_options": ["a", "b", "c", "d"],
                "question_type": "multiple_choice",
            },
            "meta": {
                "id": "q001",
                "ptype": "mcq",
                "concept": "confidence_intervals",
                "points": 2,
                "answer": 2,
                "explanation": "The procedure, not any one realized interval, has 95% coverage.",
                "status": "draft",
                "content_hash": "sha256...",
            },
        },
    ),
}


@cli.command("schema")
@click.argument("object_name", type=click.Choice(sorted(SCHEMAS)))
@human_option
@click.pass_context
def schema_cmd(ctx: click.Context, object_name: str, human: bool) -> None:
    ctx.obj.human |= human
    ctx.meta["command"] = "schema"
    schema, example = SCHEMAS[object_name]
    emit(ctx, {"object": object_name, "schema": schema, "example": example})


def main() -> None:
    try:
        cli(standalone_mode=False)
    except PruefungError as exc:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "command": "pruefung",
                    "data": {},
                    "warnings": [],
                    "errors": [{"code": exc.code, "message": str(exc)}],
                }
            ),
            err=False,
        )
        raise SystemExit(exc.exit_code)
    except click.ClickException as exc:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "command": "pruefung",
                    "data": {},
                    "warnings": [],
                    "errors": [{"code": "usage_error", "message": exc.format_message()}],
                }
            ),
            err=False,
        )
        raise SystemExit(1)
    except Exception as exc:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "command": "pruefung",
                    "data": {},
                    "warnings": [],
                    "errors": [{"code": "internal_error", "message": str(exc)}],
                }
            ),
            err=False,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
