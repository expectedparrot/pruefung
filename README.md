# pruefung

<p align="center">
  <img src="assets/pruefung-artwork.png" width="760" alt="Pruefung parrot printing an exam">
</p>

`pruefung` is an agent-first CLI for building, checking, deploying, and grading quizzes and exams with EDSL-native questions. It stores inspectable state in `.pruefung/`, emits JSON envelopes by default, and keeps all model inference behind a make → run → ingest boundary.

[Read the illustrated tutorial](https://expectedparrot.github.io/pruefung/) · [View the specification](pruefung-spec.md)

```bash
uv tool install git+https://github.com/expectedparrot/pruefung.git
pruefung agent next
```

`agent next` inspects the current workspace and returns the next safe action as JSON.
