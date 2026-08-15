# pruefung

<p align="center">
  <img src="assets/pruefung-artwork.png" width="760" alt="Pruefung parrot printing an exam">
</p>

`pruefung` is an agent-first CLI for building, checking, deploying, and grading quizzes and exams with EDSL-native questions. It stores inspectable state in `.pruefung/`, emits JSON envelopes by default, and keeps all model inference behind a make → run → ingest boundary.

```bash
python -m pip install -e .
pruefung init --course "STAT 101"
pruefung concepts add confidence_intervals
pruefung question add --type mcq --name ci_interpretation --text "Which is correct?" \
  --option A --option B --option C --option D --answer 2 --points 2 \
  --concept confidence_intervals
pruefung validate
```

See [pruefung-spec.md](pruefung-spec.md) for the complete workflow and data contract.
