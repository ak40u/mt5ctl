# Contributing

Bug reports and pull requests are welcome. Please open an issue first for
anything larger than a small fix, so the design can be agreed before the code.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install pre-commit && .venv/bin/pre-commit install   # optional
```

## Before you push

CI runs all four; run them locally first:

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy
```

## Ground rules

- **Pure core, thin edges.** Parsing, planning and script generation are pure
  functions with unit tests. Only `operations.py` and `ssh.py` touch hosts.
- **One parser.** Remote scripts ship raw bytes back; all parsing happens in
  Python with the same code that reads bundles. Do not parse chart files in
  shell.
- **Shell quoting.** Every value embedded in a generated script goes through
  `sh_quote` (single quotes). Never interpolate a value into a double-quoted
  shell context. Free-form remote output (file names, contents) travels
  base64-encoded so it cannot break the line protocol. Any new script needs a
  test that runs it with real `bash` against file names containing spaces,
  quotes, `$`, backticks and `|`.
- **Fail closed.** When state is ambiguous (an unreadable manifest, a chart whose
  expert cannot be resolved), refuse or keep files; never guess towards deleting.
- **Chart files are binary-safe.** Read and write them through `ChartFile`; a
  change that breaks the byte-exact round trip of the fixtures is a bug.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/).
