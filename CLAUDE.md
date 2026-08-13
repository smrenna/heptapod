# heptapod

Agentic HEP toolkit (MCP tools served via [toolbase](https://github.com/alexr314/toolbase))
for integrating LLM agents into high-energy physics workflows: symbolic amplitudes,
Monte Carlo event generation, and analysis. See `README.md` for the full picture;
this file is environment/repo notes for working on the codebase itself.

## Layout

- `tools/<bundle>/` — one directory per toolkit bundle (`pdg`, `mg5`, `feynrules`,
  `nda`, `eda`, `pythia`, `sherpa`, `analysis`, `inspire`, `units`, ...). Bundles
  and their dependencies/gating (`requires: [mg5_path]` etc.) are declared in
  `toolkit.yaml`.
- `skills/` — Claude Code / Codex skills shipped with the toolkit
  (`cmssw`, `central-production`, `mg5`, `feynrules`). These are the source of
  truth for physics-workflow procedure; don't duplicate their content here.
- `llm/` — harness-side test/eval helpers (e.g. `test_ollama_basic.py`).
- `examples/<name>/` — worked end-to-end examples, each with its own
  `prompts/system_prompt.md` + `task_prompt.md` and a `launch.py` that wires up
  sandbox, bundles, and harness. These are session prompts for *using* the
  served tools on a physics task, not repo-dev guidance — don't confuse them
  with this file.
- `test_runner.py` — repo test runner (`python test_runner.py --help` for flags).

## Local dev environment (this checkout, FNAL LPC)

- The venv for this checkout is the **sibling** directory `../heptadoodle`
  (outside the repo), not a `.venv` inside `heptapod/` — Python 3.13.11 from
  the CVMFS `LCG_110` SFT view. Activate it before running `tb`, `pytest`, or
  anything under `tools/`:
  ```bash
  source ../heptadoodle/bin/activate
  ```
- Installed via `tb install -e .` (editable) against that venv.

## Two distinct MC-production skills — don't conflate them

- `skills/cmssw` — build a gridpack from a **private/BSM** UFO model, stop at
  GEN/LHE/NanoGEN. No official dataset to compare against.
- `skills/central-production` — **reproduce an existing official CMS dataset's**
  full chain (GEN-SIM → DIGI-HLT-RECO/Premix → MiniAOD → NanoAOD) by pulling
  each step's real `cmsDriver` command from its McM chained request. This is
  the one for "make me events that look like dataset X."

## Gotchas specific to this account/cluster (not covered in the skills, which are written to be portable)

- **Home quota is razor-thin.** `homesrv01.fnal.gov:/uscms` (this user's
  `$HOME`) has only a **3072M** quota. `curl`/installers fail partway through
  with `Failed writing body` when it's hit, not a clear quota error. Check
  with `quota -s`; the biggest reclaimable offenders are usually `~/.cache`
  and `~/.npm` (safe to clear — caches, not data).
- **EL8-on-EL9 library mismatch bites *some* steps, not others, misleadingly.**
  This host runs EL9, but the CMS central-production chain needs
  `SCRAM_ARCH=el8_amd64_gcc1x` releases. GEN-SIM/DIGI-RECO steps can run
  unwrapped and succeed — but the MiniAOD/PAT step pulls in a plugin that
  needs `libssl.so.1.1`, which EL9 doesn't ship, and fails at `cmsRun` time
  with:
  ```
  cmsRun: error while loading shared libraries: libssl.so.1.1: cannot open shared object file
  ```
  Don't take an early unwrapped success as proof the whole chain is safe to
  run outside the container. Always wrap production commands:
  ```bash
  cmssw-el8 --command-to-run bash <script>
  ```
  (`cmssw-el8` lives at `/cvmfs/cms.cern.ch/common/cmssw-el8`; see
  `skills/central-production/SKILL.md` §0 checklist.)
- **`cmssw-el8` does not forward the `/uscms_data/d1` bind mount.** `d1` and
  `d2` are the same underlying storage outside the container (bind mount), so
  scripts/paths can use either — *except* inside `cmssw-el8`, where only `d2`
  is visible. Scripts meant to run inside the container must `cd` into the
  `/uscms_data/d2/...` path, not `d1`, or they'll fail with a plain "No such
  file or directory" that has nothing to do with the actual EL8 issue above.
- **Interactive shell is bash via handoff, not the account's real login shell.**
  The account's `passwd` entry is `/bin/tcsh` (NIS-managed; self-service
  `chsh`/`ypchsh` isn't available here). `~/.cshrc` execs into `bash` for
  *interactive* sessions only (guarded by `$?prompt` + a `BASH_TAKEOVER` env
  var) — non-interactive `tcsh -c "..."` invocations (batch/condor job
  wrappers) are left untouched. `~/.local/bin` (where the `claude` CLI lives)
  is only added to `PATH` in `~/.bashrc`, i.e. only reachable via that
  handoff, not from a raw non-interactive tcsh.
- **Grid proxy needed before Premix pileup, fails at run time not config
  time** — already documented in `skills/central-production/SKILL.md` §5,
  worth repeating because it's easy to only remember after the fact: run
  `voms-proxy-init -voms cms` before starting the DIGI step, not after
  `cmsRun` dies partway through it.
