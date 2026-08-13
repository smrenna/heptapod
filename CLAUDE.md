# heptapod

Agentic HEP toolkit (MCP tools served via [toolbase](https://github.com/alexr314/toolbase))
for integrating LLM agents into high-energy physics workflows: symbolic amplitudes,
Monte Carlo event generation, and analysis. See `README.md` for the full picture;
this file is environment/repo notes for working on the codebase itself.

## Layout

- `skills/` is the source of truth for physics-workflow procedure — don't
  duplicate its content here.
- `examples/<name>/` prompts are session prompts for *using* the served
  tools on a physics task, not repo-dev guidance — don't confuse them with
  this file.

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
