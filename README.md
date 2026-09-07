# AIND Experiment: VrForaging + FIP

> [!WARNING]
> This repository is an **internal deployment** for the Allen Institute. It wires together
> specific rig configurations, network paths, and Allen-Institute-only
> services (Dataverse, AIND watchdog/data-transfer, waterlog). It is unlikely to be directly
> useful outside of the building without significant adaptation.

An repository for an experiment that acquires data from VrForaging and FIP.

The plan is to keep adding physiology submodules (e.g. ephys, other photometry/imaging rigs)
combined with VrForaging the same way FIP is. **Every physiology submodule pinned here is assumed
to be compatible with the single pinned VrForaging version** — one known-good combination, not a
matrix of versions. If you find a pinned combination that's actually incompatible, please open an
issue.

## Architecture

This repository is a thin composition layer, not a standalone codebase. It stitches together
independent Bonsai/Python projects (one behavior repo, one-or-more physiology repos), plus a small
amount of glue code:

- **`Aind.Behavior.VrForaging/`** and **`Aind.Physiology.Fip/`** — git submodules pointing at
  pinned releases of the [VrForaging](https://github.com/AllenNeuralDynamics/Aind.Behavior.VrForaging)
  (behavior) and [FIP](https://github.com/AllenNeuralDynamics/Aind.Physiology.Fip) (physiology)
  repositories. Additional physiology modalities are added as further submodules alongside
  `Aind.Physiology.Fip/`. Each submodule provides its own Bonsai workflow (`src/main.bonsai`),
  rig/task-logic schemas, and data mappers, and is developed, tested, and released independently
  of this repository.
- **`common/`** — a small, uninstalled local package (no `pyproject.toml`, just plain Python
  importable because `main.py` runs from the repo root) holding launcher-orchestration helpers
  shared across experiments: curriculum evaluation, session confirmation, data QC, data transfer,
  data mappers, and the manipulator-position modifier.
- **`main.py`** — the actual launcher script. It defines one or more `@experiment()`-decorated
  functions (see [clabe](https://github.com/AllenNeuralDynamics/Aind.Clabe)'s experiment model) that
  each describe one runnable protocol. Currently defined experiments:
  - `vr-foraging` — run VrForaging on its own, without FIP.
  - `vr-foraging-fip` — run VrForaging and FIP concurrently as a single combined session.
  - `calibration` — run only the VrForaging rig for calibration; nothing is recorded.
  - `recover-session` — reprocess (curriculum/mappers/QC/transfer) a session whose Bonsai
    workflow(s) already completed, e.g. after a launcher crash.
- **`clabe`** (`aind-clabe`, installed as a dependency) is the framework that provides the
  `Launcher`, pickers (rig/session/trainer-state selection), data-transfer services, curriculum
  runner, and the generic multi-experiment CLI used to run `main.py`.
- **`pyproject.toml`** / **`uv.lock`** define the single Python environment (managed by
  [`uv`](https://docs.astral.sh/uv/)) that all of the above run in. `[tool.ruff]` excludes the
  submodules from linting — they are linted by their own CI.

## Getting started

1. Ensure the requirements of both repositories [Aind.Behavior.VrForaging](https://github.com/AllenNeuralDynamics/Aind.Behavior.VrForaging) and [Aind.Physiology.Fip](https://github.com/AllenNeuralDynamics/Aind.Physiology.Fip/) are fulfilled. This repository requires uv to be installed!
2. Clone this repository
3. Run `./scripts/deploy.cmd`
4. Ensure `clabe.yml` is defined in your system. There is an example in `examples/clabe.yml` that can be used to get you going by copying it into `./local`.

## Launching an experiment

`main.py` defines the available experiments (see [Architecture](#architecture)). Launch one with
the generic `clabe` CLI:

```powershell
uv run clabe run main.py
```

If more than one experiment is defined, you will be prompted to pick which one to run. You can
also pass any `clabe` launcher flags (e.g. `--frontend`, `--debug-mode`) after `main.py`. Run
`uv run clabe run --help` to see all available options.

## Testing changes

This repository has no automated tests of its own — the actual behavior lives in the
submodules, which have their own test suites and CI. To validate a change here (e.g. bumping a
submodule to a new release, or editing `main.py`/`common/`):

1. Open a new branch off `main`.
2. If you need to test against a new submodule release, update the pointer(s):
   ```powershell
   cd Aind.Behavior.VrForaging   # or Aind.Physiology.Fip, or any other physiology submodule
   git fetch --tags
   git checkout <tag>
   cd ..
   ```
   (Note: a scheduled workflow, `.github/workflows/submodule-update.yml`, already opens a PR
   automatically whenever a submodule has a newer GitHub release. It updates each submodule
   independently — it does **not** verify that the resulting VrForaging + physiology combination
   is actually compatible, so treat its PRs as a starting point, not a guarantee.)
3. Run `uv sync` to pick up any dependency changes.
4. Manually run the affected experiment(s) with `uv run clabe run main.py` on a rig (or with a
   local/dev configuration) to confirm the VrForaging + physiology combination behaves correctly
   — there is no substitute for an end-to-end run here.
5. Open a pull request back to `main`. CI (`.github/workflows/lint.yml`) will run `ruff format
   --check` and `ruff check` against the root-level code (`main.py`, `common/`); it does not lint
   the submodules.
