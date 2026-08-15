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
3. Attach and verify the roster.
4. Run `exam preview` and `exam deploy --dry-run` before a real deployment.
5. Obtain confirmation before the networked, one-shot `exam deploy` action.

A deployed exam is frozen and immutable. Never edit its frozen questions,
survey export, roster, option permutations, or publication metadata by hand.

## EDSL execution boundary

Pruefung never runs model inference. It creates inspectable EDSL task packages:

```bash
pruefung qc make
python .pruefung/inference/qc_01/run.py
pruefung qc ingest qc_01
```

The same make → run → ingest boundary applies to concept suggestions and rubric
grading. Before running a generated script, inspect `task.json`, the serialized
EDSL inputs, selected models, question count, and expected call count. Obtain
approval before paid inference. Never modify hashes in a result envelope to
bypass a stale-task rejection; rebuild the task against current content.

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
