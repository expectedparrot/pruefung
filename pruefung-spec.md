# pruefung — Specification (v3)

`pruefung` is a Python CLI that helps a professor build, quality-control, administer, and grade quizzes/exams. It maintains a growing **item bank** per course, stores questions as **EDSL-native objects** (`pip install edsl`, Expected Parrot), uses EDSL/Coop for human response collection, and produces gradebooks and item-quality reports.

The approach is adapted from Isley et al. (2025), *Assessing the Quality of AI-Generated Exams* (arXiv:2508.08314). Question *authoring* is performed by the professor or an external coding agent through the CLI — pruefung itself never generates content.

---

## 1. Design principles (normative)

1. **pruefung never calls an LLM.** All inference (QC panels, rubric grading, optional concept suggestion) happens through a make → run → ingest round trip: pruefung writes serialized EDSL inputs plus a thin runner script, the user runs it on their own Expected Parrot account/keys, pruefung ingests the results file. Every object and prompt is inspectable before it runs.
2. **Coop API calls that are deterministic I/O are built in.** `deploy` (humanize/project creation, link generation) and `status`/`grade` fetching call the Coop API directly. The no-magic rule covers inference, not network I/O.
3. **Agent-facing by default.** Every command prints a JSON envelope to stdout by default (§5.0). Human-readable tables/renderings are opt-in via `--human` (`-H`). The primary operator is expected to be a coding agent working on the professor's behalf.
4. **EDSL objects are the native currency.** Questions are validated by instantiating the corresponding EDSL class and persisted with EDSL's own serialization (`to_dict()`); a deployed Survey is assembled by deserializing stored objects, never by re-templating text. Answer keys, rubrics, and points live only in pruefung metadata, never inside EDSL objects, so deployed surveys cannot leak keys by construction.
5. **One file per object.** Each concept, question, and exam is its own small file/directory. Clean diffs, safe concurrent edits. Directory scans are the source of truth; manifests are caches.
6. **Explicit state mutation only.** Nothing enters `concepts/` or `questions/` except via an explicit CLI command (`add`, `import`, `ingest`) or a direct file edit followed by `validate`.
7. **The bank is a growing item library; exams are built up, then frozen at deploy.** While an exam is `building`, it references bank questions by id and always reflects their current content. `deploy` freezes deep copies (with option permutations), attaches the roster, publishes, and sends links. After deploy an exam is immutable.
8. **Idempotent and hash-guarded.** Inference inputs embed content hashes; `ingest` refuses results whose hashes don't match current state. Re-running any command never double-bills, double-sends, or clobbers a manual decision.

## 2. Technology

- Python ≥ 3.10. Package `pruefung`, console entry point `pruefung`.
- CLI framework: `typer` (or `click`). Human rendering: `rich`.
- `edsl` for Question/Survey objects, serialization, Coop client, humanize.
- Content conversion via a pluggable converter interface (§5.1). v1 converters: `pypdf`, `python-pptx` (must include speaker notes), `python-docx`, plain txt/md; `markitdown` (or equivalent) where it improves markdown fidelity; url fetching via `httpx` + readability-style extraction; git via subprocess `git log -p`; transcript passthrough (`.vtt`/`.srt` → markdown with timestamps as headings).
- All state under `.pruefung/` in the working directory; located by walking up from cwd (like git). No database.
- Hashes: sha256 over canonical JSON (sorted keys, no whitespace); 12 hex chars for display, full in files.

## 3. Directory layout

```
.pruefung/
  config.json                    # course name, defaults, created_at
  materials/
    manifest.json                # sources and their renders: kind, locator, hashes, timestamps
    raw/<name>.<ext>             # byte-for-byte copy (file sources; optional for remote kinds)
    md/<name>.md                 # full render, and/or
    md/<name>@<slice>.md         # partial renders, e.g. week1-4@s5-12.md, textbook@p40-55.md
  concepts/
    <concept_id>.json            # one per concept
  questions/
    <qid>.json                   # one per question: {"edsl": ..., "meta": ...}
  exams/
    <exam_id>/
      exam.json                  # state, members (building) or frozen copies (deployed),
                                 # instructions, roster, publish info
      survey.edsl.json           # exported EDSL Survey (written at deploy / exam export)
      responses.json             # fetched human responses cache (append-only)
  inference/
    <task_id>/                   # task_id: qc_01, grade_quiz-1_02, concepts_03, ...
      task.json                  # kind, input ids + hashes, created_at, status
      *.edsl.json                # serialized EDSL inputs (survey, scenarios, models)
      run.py                     # thin runner (§7.0)
      result.json                # envelope written by run.py (§4.5)
  gradebooks/
    <exam_id>.gradebook.json     # graded state incl. overrides (pruefung-managed)
    <exam_id>.gradebook.csv      # export, regenerated from the json
```

`config.json`, `concepts/`, `questions/`, and building-state `exam.json` files are the human/agent-editable surface. `materials/raw|md`, `inference/`, `responses.json`, and `gradebooks/` are pruefung- or runner-managed.

## 4. Data schemas

`pruefung schema <object>` prints the JSON Schema plus one filled example for: `question`, `concept`, `exam`, `roster`, `result`. Coding agents are expected to consult this.

### 4.1 Concept (`concepts/<id>.json`)

```jsonc
{
  "id": "confidence_intervals",     // ^[a-z0-9_]+$, equals filename stem
  "note": "weeks 5-6",              // optional
  "added_at": "2026-08-15T14:02:11Z"
}
```

### 4.2 Question (`questions/<qid>.json`)

```jsonc
{
  "edsl": {
    // EXACTLY the output of the EDSL question object's to_dict().
    // pruefung treats this block as opaque except for reading
    // question_name, question_text, question_options.
    "question_name": "ci_interpretation",
    "question_text": "A 95% confidence interval ...",
    "question_options": ["...", "...", "...", "..."],
    "question_type": "multiple_choice"
  },
  "meta": {
    "id": "q004",                    // ^q\d{3,}$, equals filename stem, never reused
    "ptype": "mcq",                  // "mcq" | "true_false" | "checkbox" | "free_text"
    "concept": "confidence_intervals",  // MUST exist in concepts/
    "points": 2,                     // positive number, REQUIRED for every question
    "answer": 2,                     // mcq: option index; true_false: boolean;
                                     // checkbox: sorted list of indices; free_text: absent
    "partial_credit": "none",        // checkbox only: "none" (default) | "per_option"
    "rubric": "2 pts: ...\n1 pt: ...",  // free_text only, REQUIRED for free_text
    "distractor_notes": "a,d target data-vs-parameter confusion",  // optional
    "source_materials": ["week5-6"], // optional, names from materials manifest
    "status": "draft",               // "draft" | "qc_passed" | "qc_failed" | "deployed"
    "qc": null,                      // written only by `qc ingest`; see §7.2
    "created_at": "...", "updated_at": "...",
    "content_hash": "..."            // sha256 over (edsl block, answer, rubric, points);
                                     // maintained by `validate` and `question add`
  }
}
```

ptype → EDSL class mapping: `mcq` → `QuestionMultipleChoice` (exactly 4 options), `true_false` → `QuestionYesNo`, `checkbox` → `QuestionCheckBox` (3–6 options), `free_text` → `QuestionFreeText`. `answer`/`rubric`/`points` NEVER appear inside the `edsl` block.

Status lifecycle: `draft → (qc) → qc_passed | qc_failed`; any content edit resets to `draft` (detected by hash change in `validate`); inclusion in a deployed exam adds sticky `deployed` (question remains reusable in other exams).

### 4.3 Exam (`exams/<exam_id>/exam.json`)

An exam has `state`: `"building"` or `"deployed"`.

**While building** — a live view over the bank:

```jsonc
{
  "exam_id": "quiz-1",               // ^[a-z0-9-]+$, equals directory name
  "title": "Quiz 1",
  "state": "building",
  "instructions": "You have 20 minutes. Closed book.",  // optional; rendered as an
                                     // EDSL Instruction at the top of the deployed Survey
  "created_at": "...",
  "members": ["q001", "q004", "q005"],   // ordered bank ids; presentation order
  "roster": [                        // attached via `exam roster`; required before deploy
    {"email": "...", "name": "...", "student_id": "..."}
  ]
}
```

**After deploy** — frozen and immutable:

```jsonc
{
  "exam_id": "quiz-1",
  "title": "Quiz 1",
  "state": "deployed",
  "instructions": "...",
  "created_at": "...", "deployed_at": "...",
  "questions": [                     // FROZEN COPIES replacing members[]
    {
      "bank_id": "q004",
      "bank_content_hash": "...",    // hash at freeze time
      "frozen": { /* full question file content, with the option permutation
                     ALREADY APPLIED to edsl.question_options and meta.answer */ },
      "option_permutation": [2,0,3,1],   // null for true_false / free_text
      "question_name": "q1_confidence_intervals"   // unique in exam; stable join key
    }
  ],
  "total_points": 10,
  "roster": [ ... ],                 // as attached at deploy time
  "published": {
    "project_uuid": "...",
    "respondent_url": "...",
    "admin_url": "...",
    "links": [ {"email": "...", "url": "...", "sent_at": "..."} ],
    "identity_mode": "per_link" | "identity_question"
  }
}
```

### 4.4 Roster (input CSV)

Columns: `email` (required), `name`, `student_id` (optional). Lenient parsing (BOM, header case, surrounding whitespace).

### 4.5 Result envelope (`inference/<task_id>/result.json`, written by runners)

```jsonc
{
  "task_id": "qc_01",
  "kind": "qc" | "rubric_grade" | "concept_suggest",
  "input_hashes": { "q004": "...", ... },   // copied verbatim from task.json
  "created_at": "...",
  "payload": { /* kind-specific, §7 */ }
}
```

`ingest` validates: envelope shape; `task_id` exists; every `input_hashes` entry equals the *current* hash of that object. Mismatch → refuse, name the changed objects, exit 3. A result may be ingested once; re-ingest is a no-op with a notice.

## 5. CLI command reference

### 5.0 Output contract

Default stdout for every command is a single JSON envelope:

```jsonc
{ "ok": true, "command": "exam add", "data": { ... },
  "warnings": ["..."], "errors": [] }
```

`--human` / `-H` switches to rich tables/renderings. Exit codes: 0 success, 1 usage error, 2 validation errors, 3 hash/state conflicts, 4 network/Coop errors. Billing-adjacent or destructive actions print intent and require `--yes` or interactive confirmation.

### 5.1 Project & materials

```
pruefung init --course "STAT 101: Intro to Statistics"
```
Creates `.pruefung/`. Fails if present. No other options.

Materials follow a two-step model: **register** a source (cheap — records what and where it is), then **render** it (or a slice of it) to markdown when actually needed. This supports large sources you never fully convert (a 50-slide deck where only weeks 5–6 matter) and non-file sources.

```
pruefung source add <locator> [--name <name>] [--kind <kind>]
```
Registers a source in the manifest. `kind` is inferred from the locator where possible:
- `file` — local path (pdf, pptx, docx, txt, md). The original is copied byte-for-byte to `materials/raw/`.
- `url` — http(s) page. Registration records the URL; content is fetched at render time (deterministic I/O, allowed under §1.2). The fetched bytes are cached in `raw/`.
- `git` — a repo path plus `--rev <ref-or-range>` (e.g. `--rev v1.0..HEAD`, `--rev abc123`). Renders as markdown of the log + diffs, or `--paths src/` to scope.
- `transcript` — a `.vtt`/`.srt`/plain transcript file standing in for audio/video content (record an optional `--of-url` linking to the original video). pruefung does NOT transcribe media itself; transcription is the user's tooling. The `kind` field is an open set so converter plugins (§below) can add more.

```
pruefung source render <name> [--slice <spec>]
```
Produces `md/<name>.md` (no slice) or `md/<name>@<slice>.md`. Slice specs by kind: `s5-12` (slides), `p40-55` (pages), `h2:Sampling` (heading subtree, md/url), git ranges via `--rev` at add time. A source may have many renders; each is listed in the manifest with its own hash, char count, and rendered_at. Re-rendering an existing name+slice overwrites and reports old→new hash. Rendering a `url` re-fetches; the manifest records fetch time and content hash so drift is visible.

```
pruefung ingest <path> [--name <name>]
```
Convenience sugar preserved from earlier versions: `source add` + full `source render` in one step, for the common "small local file" case.

```
pruefung materials [--show <name-or-name@slice>]
```
Lists sources with their renders (kind, locator, slices, chars, hashes, timestamps); `--show` prints a render's markdown.

**Converters are a plugin point.** Implement a small converter interface (`kind` → fetch bytes → markdown, with slice support where meaningful). v1 ships: pdf, pptx (must include speaker notes), docx, txt/md, url (readability-style extraction), git, transcript. Anything else registers fine but errors helpfully at render time, naming the interface to implement.

**Referencing granularity.** Everywhere a material name is accepted — `question add --source-material`, `concepts suggest --materials` — a render name (`week1-4` or `week1-4@s5-12`) is the unit. Inference tasks embed the render's hash, so the hash-guard (§1.8) extends to materials: a QC or concept task built on a render that has since been re-rendered is `stale`.

### 5.2 Concepts

```
pruefung concepts add <id> [--note "..."]
pruefung concepts rm <id>              # refuses if any question references it
pruefung concepts import <file.json> [--review]
pruefung concepts                       # list
```
`import` accepts a plain `{"concepts": [...]}` file or a `concept_suggest` result envelope; `--review` steps through interactively (implies `-H`; an error in JSON mode). Duplicates skipped with a warning. Optional LLM suggestion uses the inference round trip: `pruefung concepts suggest [--materials a,b]` writes an inference task (§7.1); after running it, `concepts import` its result.

### 5.3 Questions (piecewise authoring)

```
pruefung question add
    --type mcq|true_false|checkbox|free_text
    --name <edsl_question_name>          # ^[a-z][a-z0-9_]*$, unique across bank
    --text "..."                          # or --text-file <path>
    --option "..." [--option "..." ...]   # mcq: exactly 4; checkbox: 3-6
    --answer <index|true|false|i,j>       # required unless free_text
    --points <n>                          # required
    --concept <concept_id>                # required, must exist
    [--rubric "..."] | [--rubric-file <path>]   # required for free_text
    [--partial-credit none|per_option]
    [--distractor-notes "..."]
    [--source-material <name>] ...
    [--id q0NN]                           # default: next free id
```

Behavior:
1. Construct the corresponding **EDSL question object** from the inputs. Any EDSL validation error is surfaced verbatim; nothing is written. This is the validity gate — pruefung adds only checks EDSL doesn't do (option-count policy per ptype, answer index in range, rubric presence, concept existence, unique `question_name`).
2. Serialize with `to_dict()` into the `edsl` block; write `questions/<qid>.json` with the meta block and content hash.
3. Envelope `data` includes id, hash, and the round-tripped EDSL dict.

```
pruefung question set <qid> <field> <value>    # points, concept, answer, rubric,
                                               # distractor-notes, partial-credit
pruefung question rm <qid>                     # refuses if in any exam (building or deployed)
pruefung show <qid>                            # full render: text, options, key marker,
                                               # points, rubric, QC verdict, exams using it
pruefung ls [--status ...] [--concept ...] [--type ...]
pruefung coverage                              # bank-wide: concepts × counts × exam usage
pruefung validate
```

Editing question text/options: `question add --id <existing>` (full replace, prints a diff summary, resets status to draft) or direct file edit + `validate`. `validate` re-instantiates every `edsl` block through EDSL (round-trip check), verifies meta invariants, recomputes hashes, resets status to `draft` where content changed, and lists which building exams contain each changed question. It preserves file formatting: 2-space indent, key insertion order.

### 5.4 Exams: build up, inspect, attach students, deploy

Exams are assembled incrementally. While `building`, an exam holds bank ids and always reflects current bank content; nothing is frozen and nothing touches the network.

```
pruefung exam create <exam_id> [--title "..."]
                     [--instructions "..."] [--instructions-file <path>]
```
Creates an empty exam in state `building`.

```
pruefung exam add <exam_id> <qid> [<qid> ...] [--at <position>]
pruefung exam remove <exam_id> <qid> [...]
pruefung exam reorder <exam_id> --order q004,q001,q010,...
```
Membership editing. `add` warns (not errors) when a question's status is not `qc_passed` — the gate is enforced at deploy. Duplicate membership is an error.

```
pruefung exam stats <exam_id>
```
The inspection command; envelope `data` (and `-H` table) includes:
- question count, total points, estimated duration (config default: 1.5 min/mcq-tf, 2/checkbox, 5/free_text; overridable in config.json),
- type mix (counts per ptype),
- concept coverage: per concept in the exam, question count and points; plus course concepts NOT covered by this exam,
- QC readiness: per question, status; a `ready_to_deploy` boolean (all members qc_passed, ≥1 member, roster attached),
- points balance warnings (e.g., one question > 50% of total points),
- for deployed exams: the same plus response counts.

```
pruefung exam roster <exam_id> <roster.csv>
```
Parses and attaches the roster to the exam (replaces any prior roster while building; refused after deploy). Envelope reports rows parsed, duplicates dropped, malformed rows.

```
pruefung exam preview <exam_id>            # terminal student-view rendering + keys marked (-H)
pruefung exam preview <exam_id> --web      # REAL web preview via humanize (see below)
pruefung exam list                         # id, state, title, #questions, points, responses
pruefung exam rm <exam_id>                 # refuses if deployed
```

`--web` builds the survey exactly as deploy step 2–3 would (freeze semantics applied in-memory only — nothing written to the exam file, no bank status changes, a fixed preview seed for the permutations so repeated previews are stable) and creates a **preview** web version through humanize's preview/dry-run support (verify the exact parameter in the EDSL docs; if humanize has no preview flag, create a normal project named `<exam_id>-preview` with visibility private). The envelope returns the preview URL. Preview project uuids are stored under a `previews` list in `exam.json`, never under `published`; `status`/`grade` must ignore them, and any responses submitted to a preview are never fetched. This is the professor's "see exactly what students will see" step before deploy; the real deploy creates a fresh project and does not reuse the preview.

```
pruefung exam deploy <exam_id> [--dry-run] [--no-shuffle] [--allow-draft]
```
The one-shot transition from `building` to `deployed`. Steps, in order, atomic (any failure before the network step leaves the exam untouched; a failure after project creation is recorded so re-running resumes rather than duplicating):
1. **Gate**: every member `qc_passed` (`--allow-draft` overrides, loudly); ≥ 1 member; roster attached and non-empty.
2. **Freeze**: deep-copy each member's question file into `questions[]`; apply one random option permutation per option-bearing question, rewriting both frozen `edsl.question_options` and `meta.answer` (`--no-shuffle` disables); assign `question_name`s `q<N>_<concept>`; compute `total_points`; set members' bank status to `deployed`.
3. **Export**: assemble the EDSL `Survey` by deserializing the frozen `edsl` blocks, prepending `instructions` as an EDSL Instruction; write `survey.edsl.json`. The survey is key-free by construction (§1.4); tests must assert it.
4. **Publish**: `survey.humanize(project_name=exam_id, ...)`; store project uuid/urls.
5. **Links**: **verify at implementation time** whether EDSL/Coop supports per-respondent links and/or API email delivery. If yes: one link per roster row, record in `links`, send if supported, `identity_mode: "per_link"`. If not: write `<exam_id>.links.csv` (email, name, url) for a mail merge; if per-respondent URLs are impossible, auto-prepend an identity `QuestionFreeText` ("Your university email address") to the survey before step 4 and set `identity_mode: "identity_question"`. The fallback must be fully functional — do not stub.
6. Set `state: "deployed"`, `deployed_at`.

`--dry-run` performs steps 1–3, prints everything including a rendered survey summary, touches no network, and leaves the exam in `building`.

`pruefung exam resend <exam_id>` re-emits/re-sends links only for roster emails with no recorded response.

### 5.5 Monitor, grade, review

```
pruefung status <exam_id>
```
Fetches responses from Coop (`get_project_human_responses` or current equivalent), merges into `responses.json` (append/update by respondent, never delete), reports responded/roster counts and missing emails.

```
pruefung grade <exam_id> [--rescore]
```
1. Fetch/refresh responses.
2. Join to roster per `identity_mode`. Unmatched identifiers listed separately; duplicate submissions → keep latest complete, note discards.
3. Deterministic scoring from frozen meta: mcq/true_false exact match; checkbox per `partial_credit` (`none`: all-or-nothing; `per_option`: +points/k per correct selection, −points/k per incorrect, floor 0, k = |correct set|).
4. free_text: if ungraded answers exist and no rubric_grade result ingested → envelope includes the exact next commands (`grade make`, run, `grade ingest`); grade everything else meanwhile.
5. Write gradebook JSON + CSV. **Merge semantics**: manual overrides and resolved `needs_review` entries are never overwritten; new respondents appended; already-graded answers recomputed only with `--rescore`.
6. Report: score distribution (median/mean/range); per-item p-value (proportion of points earned), point-biserial vs. total, top distractor share for mcq (with text snippet); flags: `p > .95` too easy, `p < .10` too hard/possible miskey, point-biserial `< .20` low discrimination, negative point-biserial → "probable miskey" stated plainly. CTT only; no IRT in v1.

```
pruefung review <exam_id> [--student <email>]
```
Interactive (implies `-H`): steps through `needs_review` free-text answers (question, rubric, student answer, both panel scores + justifications); professor enters a final score or accepts a panel score → written with `override: true`. `--student` allows overriding any individual score.

### 5.6 Inference round trip (QC, rubric grading, concept suggestions)

The three tasks that need an LLM share one pattern — pruefung writes the task, the user runs it, pruefung ingests the result:

```
pruefung qc make [--only q003,q008] [--models "m1,m2,m3"]     # default scope: all drafts
python .pruefung/inference/qc_01/run.py
pruefung qc ingest qc_01                                       # task id or path to result.json

pruefung grade make <exam_id> [--models "m1,m2"]               # requires fetched responses
python .pruefung/inference/grade_quiz-1_02/run.py
pruefung grade ingest <exam_id> grade_quiz-1_02

pruefung concepts suggest [--materials <name>,<name>]          # optional convenience
python .pruefung/inference/concepts_03/run.py
pruefung concepts import .pruefung/inference/concepts_03/result.json --review
```

`* make` writes into `inference/<task_id>/`:
- **Serialized EDSL inputs** — the Survey, ScenarioList, ModelList for the task, each as `<thing>.edsl.json` via EDSL's own serialization. These are the complete, inspectable definition of what will run.
- **`task.json`** — kind, input object ids + content hashes, created_at, status `pending`.
- **`run.py`** — a thin, dependency-light runner (~30 lines; imports only `edsl` + stdlib) that deserializes the `.edsl.json` inputs, calls `.run()`, reshapes Results into the payload (§7), wraps it in the envelope with fields copied from `task.json`, and writes `result.json` next to itself. Guarded under `if __name__ == "__main__":`. Top-of-file comment: what it does, which models, rough call count.

Idempotence: identical kind + input-hash set as an existing `pending` task → print that task's id, create nothing (`--force` overrides). `pruefung inference` lists tasks (id, kind, created, status pending|ingested|stale; stale = any input hash drifted).

## 6. EDSL integration notes

- Confirm current API names against docs.expectedparrot.com at implementation time. Anchors: `QuestionMultipleChoice`, `QuestionCheckBox`, `QuestionYesNo`, `QuestionFreeText`, `QuestionList`, `QuestionNumerical`, `Survey`, survey instructions (`Instruction`), `ScenarioList`, `Model`/`ModelList`, `.to_dict()`/`.from_dict()` round-tripping, `survey.humanize()`, `Coop`, project human-responses fetch.
- EP credentials come from the user's environment/EDSL config; pruefung never stores keys.
- Runners must work with only `edsl` installed and EP credentials configured.
- Verify humanize's preview/dry-run support and its exact parameters; `exam preview --web` (§5.4) prefers a native preview mode and falls back to a private `<exam_id>-preview` project.
- Check whether the web survey randomizes option order per respondent; the design assumes NOT (permutation frozen at deploy), which is safe either way.
- Pin the tested `edsl` version in packaging metadata; the library moves quickly.

## 7. Inference task specifications

### 7.0 Runner contract
Deserialize the task's `.edsl.json` inputs → run → reshape Results to `payload` → wrap in envelope (§4.5) → write `result.json`. No other side effects.

### 7.1 `concept_suggest`
One `QuestionList` ("List the 10–15 most important, testable concepts in these course materials; snake_case identifiers; one short note each") over a Scenario carrying the selected materials' **markdown** text. Single model, user-editable in the serialized ModelList.
`payload`: `{"suggestions": [{"id": "...", "note": "..."}]}`

### 7.2 `qc` (blind-solve panel)
Per in-scope question, scenarios carry the question exactly as stored (no key). Per ptype:
- mcq / true_false / checkbox: the panel answers the question itself (deserialized EDSL object), plus one `QuestionFreeText` flag prompt: "Note anything ambiguous, answerable without course knowledge (e.g., option-length cues), or miskeyed-looking; else say 'none'."
- free_text: the panel answers as `QuestionFreeText`; the same task then grades each panel answer against the rubric with `QuestionNumerical` (0..points); plus `QuestionYesNo`: "Is this rubric unambiguous and does it cover the answers that deserve credit?"

Run `.by(ModelList)` with a 3-model default panel.
`payload` per question id: panel answers, flags, and (free_text) rubric scores.

**`qc ingest` effects** — keyed types: `qc_passed` iff ≥ 2/3 panel answers match `meta.answer` and no blocking flags; free_text: `qc_passed` iff panel answers average ≥ ⅔ of points under the rubric AND rubric-clarity unanimous yes. Full verdict written to `meta.qc`; failures keep panel reasoning in `meta.qc.notes` and set `qc_failed`.

### 7.3 `rubric_grade`
Scenarios: (question_name, question text, rubric, points, answer_id, student answer text) for every ungraded free_text answer in the exam's fetched responses. Survey: `QuestionNumerical` (score 0..points) + `QuestionFreeText` (justification citing rubric lines). 2-model panel.
`payload`: per answer_id: both scores + justifications.
**`grade ingest` effects**: scores within 25% of the question's points → record the mean; otherwise `needs_review` with both verdicts attached.

## 8. Testing expectations

- Unit: EDSL round-trip validation in `question add` (each ptype; reject: 3-option mcq, out-of-range answer, free_text without rubric, unknown concept, duplicate question_name, missing points); exam membership rules (duplicate add, remove, reorder); `exam stats` computations (points, duration estimate, coverage, ready_to_deploy logic); deploy freezing (permutation applied consistently to options AND answer; bank statuses flipped; atomicity/resume on simulated failure after project creation); deployed survey contains no key/rubric/points material (string-level assertion); checkbox partial-credit math; hash guard accept/refuse on ingest; gradebook merge (override survives re-grade; new respondent appended; `--rescore`); roster parsing edge cases; envelope output shape on every command.
- End-to-end fixture test with a faked Coop layer: init → ingest a small pdf → source add a pptx fixture + source render with a slide slice (assert raw copy + sliced markdown exist and manifest lists both) → concepts add ×4 → question add ×6 (incl. one free_text) → validate → qc make (assert `.edsl.json` inputs + run.py syntax-check without network) → inject fake qc result → qc ingest → exam create → exam add ×5 → exam stats (assert ready_to_deploy false: no roster) → exam roster → exam stats (true) → exam deploy --dry-run → exam deploy (fake Coop) → inject fake responses → grade → grade make → inject fake rubric result → grade ingest → grade → scripted review → assert gradebook JSON and CSV contents. Additional converter tests: url converter against a local fixture server; git converter against a fixture repo; transcript passthrough; unknown kind errors helpfully at render, not at add.

## 9. Golden path (acceptance walkthrough)

```
pruefung init --course "STAT 101: Intro to Statistics"
pruefung ingest syllabus.pdf
pruefung source add slides/full-semester.pptx --name slides
pruefung source render slides --slice s41-72        # only weeks 5-6 of a 90-slide deck
pruefung source add https://stats101.example.edu/notes/ci-handout --name ci_handout
pruefung source render ci_handout
pruefung materials -H
pruefung concepts add sampling_distributions
pruefung concepts add clt --note "weeks 3-4"
pruefung concepts add confidence_intervals
pruefung concepts add p_values
pruefung question add --type mcq --name ci_interpretation \
  --text "A 95% confidence interval for a population mean is (12.1, 15.9). Which interpretation is correct?" \
  --option "95% of the data fall between 12.1 and 15.9" \
  --option "The population mean is in (12.1, 15.9) with probability 0.95" \
  --option "The procedure produces intervals containing the true mean 95% of the time" \
  --option "95% of sample means fall between 12.1 and 15.9" \
  --answer 2 --points 2 --concept confidence_intervals
#  ... (agent adds q001–q010 similarly, incl. one free_text with --rubric-file)
pruefung validate
pruefung ls -H
pruefung qc make
python .pruefung/inference/qc_01/run.py
pruefung qc ingest qc_01
pruefung ls --status qc_passed

pruefung exam create quiz-1 --title "Quiz 1" \
  --instructions "You have 20 minutes. Closed book. Answer every question."
pruefung exam add quiz-1 q001 q004 q005 q007 q010
pruefung exam stats quiz-1 -H
pruefung exam roster quiz-1 roster.csv
pruefung exam preview quiz-1 -H
pruefung exam preview quiz-1 --web      # professor opens the returned URL, sees the real web survey
pruefung exam deploy quiz-1 --dry-run
pruefung exam deploy quiz-1

pruefung status quiz-1
pruefung grade quiz-1
pruefung grade make quiz-1
python .pruefung/inference/grade_quiz-1_02/run.py
pruefung grade ingest quiz-1 grade_quiz-1_02
pruefung grade quiz-1
pruefung review quiz-1
```

## 10. Out of scope for v1

Simulated (AI agent) test-takers and IRT fitting; Prolific integration; multi-course workspaces; web UI; LaTeX/PDF exam export; LMS gradebook integration. Schemas must not preclude these (the responses cache and gradebook formats should accommodate simulated respondents later).
