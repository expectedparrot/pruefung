# Agent Guide for Pruefung

Pruefung is an agent-first CLI for building, quality-controlling, administering,
and grading quizzes and exams. Use the CLI and the files under `.pruefung/` as
the workflow source of truth. Commands emit one JSON envelope by default; use
`--human` only when a professor needs a terminal rendering.

Use the CLI itself as the state-aware source of truth:

```bash
pruefung agent next
```

Run `pruefung agent next` after each completed stage and follow the returned
action. Use `pruefung <command> --help` for the installed interface rather than
guessing arguments from examples. Consult `pruefung schema <object>` when the
next action requires structured input. Keep stable question IDs and EDSL
`question_name` values intact. Prefer CLI mutations; if a question file is
edited directly, immediately run `pruefung validate` and report every question
whose content hash changed or whose QC status returned to `draft`.

## Professor-facing communication

Pruefung is an implementation detail. Operate it quietly as the professor's
assessment assistant. Do not mention Pruefung, CLI commands, task IDs, JSON,
hashes, manifests, ingestion, deployment internals, workflow phase names, or a
"QC panel" unless the professor explicitly asks for technical details. Never
copy the `reason` or `commands` fields from `agent next` into a user-facing
message. When `user_message` is present, use its plain-language substance and
adapt it naturally to the conversation.

Talk about the work in the professor's terms: course materials, learning goals,
question wording, answer choices, correct answers, explanations, exam format,
students, grading, and reports. Ask only for decisions they need to make. For
example:

- Say “The questions are drafted. May I have independent reviewers check them
  for ambiguity and answer-key problems?” — not “The QC panel is ready.”
- Say “The reviewers disagreed about question 4; would you like to revise it or
  keep it as written?” — not “q004 failed QC; use an override.”
- Say “Should students receive individual invitations, or should everyone use
  one shared link?” — not “The exam needs a roster or open deployment.”
- Say “Two written responses received inconsistent suggested scores and need
  your judgment.” — not “The rubric task exceeded the panel threshold.”

Keep routine progress reports outcome-focused and brief. Technical command
output is for the agent's working context, not the professor's conversation.

## Authoring and exam lifecycle

Register materials before rendering only the slices needed. Concepts must exist
before questions can reference them. Every question requires positive points,
a concept, and a valid EDSL representation; answer keys, points, and rubrics
belong in Pruefung metadata and must never be inserted into the EDSL block.

Build exams incrementally and inspect `pruefung exam stats <exam-id>` before
deployment. Treat these as hard checkpoints:

1. Review question wording, options, answer keys, rubrics, points, and coverage.
2. Run and ingest QC, or explicitly tell the professor when draft questions
   would require `--allow-draft`.
3. Ask whether distribution should use a roster or one shared open link.
4. Run `exam preview` and `exam deploy --dry-run` before a real deployment.
5. Obtain confirmation before the networked, one-shot `exam deploy` action.

A deployed exam is frozen and immutable. Never edit its frozen questions,
survey export, roster, option permutations, or publication metadata by hand.

## EDSL execution boundary

Pruefung never runs model inference. It creates inspectable EDSL task packages:

```bash
pruefung qc make
python .pruefung/inference/qc_01/run.py --yes
pruefung qc ingest qc_01
```

The same make → run → ingest boundary applies to concept suggestions and rubric
grading. Before running a generated script, inspect `task.json`, the serialized
EDSL inputs, selected models, question count, and expected call count. Obtain
approval before paid inference. Never modify hashes in a result envelope to
bypass a stale-task rejection; rebuild the task against current content.

Use `pruefung qc report <task-id> -H` to investigate a failed panel. Never
rewrite a result payload or reset an ingested task to manufacture a passing
verdict. Model benchmarking against deployed exams is intentionally outside the
v1 workflow; do not improvise it by running models against managed exam files.

When three or more questions all use one format, `agent next` pauses for the
professor to choose whether to keep it or add a mix. Record a deliberate choice
with `pruefung question mix approve --decision keep --note "..."`; a bank change
makes that approval stale. Preserve panel evidence when using the explicit
`pruefung qc override <qid> --decision pass|fail --reason "..."
--professor-approved` escape hatch. Never use that confirmation flag unless the
professor explicitly made the decision in the conversation.

Use `exam deploy --open` for a shared URL without a roster. Use
`pruefung grade-report <exam-id> --anonymize` for item and concept statistics
without reading or exposing protected gradebook files.
After grading, use `pruefung post-exam-report <exam-id>` to write a
self-contained aggregate HTML report. Author explanations with `question add
--explanation ...`; they remain private metadata and never enter the deployed
EDSL survey.

For exams with free-text items, follow the complete post-exam sequence:

```bash
pruefung grade <exam-id>
pruefung grade-make <exam-id>
python .pruefung/inference/<task-id>/run.py --yes
pruefung grade-ingest <exam-id> <task-id>
pruefung post-exam-report <exam-id>
pruefung student-reports <exam-id>
```

Inspect the model scores and rubric-specific feedback before ingestion. If
the panel disagreement exceeds the managed threshold, leave the item marked
`needs_review` for professor judgment; do not present an unresolved report as
final. Regenerate the post-exam report after any manual grading decision.
Use `student-report <exam-id> <email-or-name>` for one detailed instructor copy,
or `student-reports <exam-id>` for printable cut sheets covering every student.
These reports contain student responses and must remain private. Include the
native EDSL score comment as grader feedback; do not create a redundant model
question solely to request a justification.

Coop calls for Humanize deployment and response retrieval are network I/O, not
model inference. Let EDSL manage credentials. Never print, copy, store, or
commit API keys, `.env` files, student responses, rosters, or gradebooks.

## Grading and privacy

Treat `.pruefung/exams/*/responses.json` and `.pruefung/gradebooks/` as protected
student records. Do not publish them. Preserve manual grading overrides and
`needs_review` decisions. Report unmatched identities and duplicate submissions
instead of silently guessing. Free-text panel disagreement requires professor
review; model scores are recommendations, not final authority.

## Repository checks

Before committing code or documentation, run:

```bash
ruff check pruefung tests
pytest -q
python -m compileall -q pruefung
python -m build
git diff --check
```

The public tutorial belongs at `docs/index.html`. Keep its commands synchronized
with the CLI, its examples free of real student data, and its artwork available
from repository-relative paths so GitHub Pages can serve it.
