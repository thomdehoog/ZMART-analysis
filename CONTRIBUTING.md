# Contributing

Thanks for taking an interest. ZMART Analysis is a small research tool; the
rules are short.

## Set up

```bash
git clone https://github.com/thomdehoog/ZMART-analysis.git
cd ZMART-analysis
pip install -e ".[test]" ruff
```

## Before you open a pull request

```bash
ruff check .
ruff format --check .
pytest -m "not cellpose and not conda_env and not pooch"
```

That is what CI runs. Tests that need a conda environment, Cellpose or
downloaded sample images carry the markers `conda_env`, `cellpose` and
`pooch`; run them when you have what they need.

## What goes where

- `zmart_analysis/` is the engine. Changes there come with a test in `tests/`.
- A workflow lives in `workflows/<name>/` with its recipes, steps,
  environment scripts and tests. [Part 2 of the
  docs](docs/2_implement_an_analysis_step/README.md) says how a step is
  written.
- Docs are plain and short. Say what a thing does, once.

## Building on it

The API is a release candidate. If you build a workflow on it, open an
issue and say so; it helps keep the contract honest.
