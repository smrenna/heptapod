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

## Two distinct MC-production skills — don't conflate them

- `skills/cmssw` — build a gridpack from a **private/BSM** UFO model, stop at
  GEN/LHE/NanoGEN. No official dataset to compare against.
- `skills/central-production` — **reproduce an existing official CMS dataset's**
  full chain (GEN-SIM → DIGI-HLT-RECO/Premix → MiniAOD → NanoAOD) by pulling
  each step's real `cmsDriver` command from its McM chained request. This is
  the one for "make me events that look like dataset X."

## CMS-internal material: ask before reading, never quote into this repo

This repo is a **public** fork. Some CMS sources are access-controlled, and
reading one into an agent session copies it into a conversation transcript
that leaves this machine — an irreversible step that no later edit undoes.

Treat as internal, and **ask before reading**, anything under:

- a clone of a `gitlab.cern.ch` repository (`genproductions_cards`, analysis
  repos, CMSDAS material) — the clone URL in `git remote -v` is the tell;
- CMS-internal web services and their local caches — McM beyond its public
  read-only REST API, CADI, the CMS TWiki;
- any directory whose contents imply an unpublished analysis: production
  cards with specific mass points, unreleased datasets, private ntuples.

By contrast, `github.com/cms-sw/genproductions`, the public McM REST API, and
DAS query output are public and fine to read freely. When in doubt, the
question is cheap and the read is not reversible.

**When internal material informs a skill, write down the technique, not the
artifact.** Conventions, mechanisms, failure modes, and aggregate counts are
portable and carry no analysis content. Verbatim cards, process and dataset
names, model archive paths, coupling values, and mass points identify what CMS
is producing and must not land in tracked files — use a synthetic, internally
consistent example instead (see `skills/cmssw/references/pitfalls.md` P4 for
the pattern). Reference clones stay gitignored and out of the tree.

## Gotchas specific to this account/cluster (not covered in the skills, which are written to be portable)

- **`cmssw-el8` does not forward the `/uscms_data/d1` bind mount.** `d1` and
  `d2` are the same underlying storage outside the container (bind mount), so
  scripts/paths can use either — *except* inside `cmssw-el8`, where only `d2`
  is visible. Scripts meant to run inside the container must `cd` into the
  `/uscms_data/d2/...` path, not `d1`, or they'll fail with a plain "No such
  file or directory" — unrelated to the EL8/EL9 library mismatch documented in
  `skills/central-production/SKILL.md`, which fails the same command for an
  entirely different reason.
