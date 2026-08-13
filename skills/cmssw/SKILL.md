---
name: cmssw
bundle: mg5
description: Take a UFO model from a validated MG5 process to CMSSW-showered events — build a MadGraph gridpack with CMS genproductions, shower it inside CMSSW with Pythia8, and validate the output. Use whenever the user mentions gridpacks, genproductions, gridpack_generation.sh, ExternalLHEProducer, cmsDriver.py, GEN-SIM, NanoGEN, Pythia8HadronizerFilter, or running a private/BSM UFO model through the CMS production chain. Also use proactively before writing a CMSSW GEN fragment for a non-stock model — the tutorial path (network-fetched models, cvmfs tarball, SM Rivet routines) silently doesn't generalize to a private UFO model.
---

# UFO → gridpack → CMSSW: showering a private model in the CMS chain

This skill picks up where `mg5` and `feynrules` leave off: you have a UFO model
and a MG5 process that runs cleanly by hand, and now need CMS-style production
— a gridpack, driven through CMSSW's `cmsDriver.py`/Pythia8 GEN step, with a
validation pass at the end. It is written against the [CMSDAS generator
exercise](https://gitlab.cern.ch/cmsdas-hamburg-2025/generator-short-excercise),
but generalized: that exercise hardcodes a stock SM `ttbar` process with an
officially-published cvmfs gridpack and SM Rivet routines, and every one of
those three things has a different, non-obvious answer for a private/BSM UFO
model. This skill covers the differences.

```
UFO/ + proc_card + run_card ──[gridpack_generation.sh]──▶ gridpack .tar.xz
                                                                │
                          CMSSW fragment (cff.py) ─────────────┤
                                                                ▼
                              cmsDriver.py ──▶ cmsRun ──▶ GEN/LHE/NanoGEN + (optional) Rivet .yoda
```

Worth knowing up front, because it explains why this works for *any* model:
the UFO is needed only at gridpack-**build** time. The tarball then carries
the model inside it, and Pythia8 learns your new particles from the
`QNUMBERS` blocks MG5 writes into the LHE header — CMSSW never reads the UFO.
That's also why a PDG code colliding with an SM particle breaks things
silently rather than loudly (pitfall P5).

For authoring/checking the UFO itself, see the `feynrules` skill. For getting
the MG5 process itself right (decay chains, widths, `NP=N`) before it goes
into a gridpack, see the `mg5` skill — do that validation *first*, ideally
with `ValidateProcessTool` or a small interactive `launch`, since a gridpack
build is much more expensive to redo than a MG5 shell session.

## Three things the tutorial's stock-model path hides

### 1. genproductions fetches extra models over the network — a private UFO is not reachable that way

`gridpack_generation.sh` downloads and runs its **own** MG5 install in a
sandbox, unrelated to your `mg5_path`. Its supported hook for a non-stock
model is a file named `<process_name>_extramodels.dat` in the cards
directory, listing archive filenames one per line. The script then does
(verbatim from `gridpack_generation.sh`):

```bash
wget --no-check-certificate https://cms-project-generators.web.cern.ch/cms-project-generators/$model
```

and unpacks each into the sandboxed MG5's `models/` (handling `.zip`,
`.tgz`, `.tar*`). The URL prefix is **hardcoded** — entries are filenames,
not URLs and not local paths. So the mechanism only works for models already
published to that CMS web area. Your freshly-built UFO is not there, and
`_extramodels.dat` cannot reach it.

Two ways forward, depending on whether the model is headed for official
production:

- **Official route** — get the model archive published to the CMS generators
  web area, then list its filename in `<process_name>_extramodels.dat` and
  use the bare model name (the directory name *inside* the archive) in the
  proc card's `import model`.
- **Local/private route** — skip `_extramodels.dat` entirely and put an
  **absolute path** in the proc card:

  ```
  import model /absolute/path/to/my_bsm_model
  ```

  MG5 resolves this itself during the code-generation step, which runs in
  `$WORKDIR` on the machine invoking the script, so a normal filesystem path
  works. The caveat is that it breaks if generation is farmed out to batch
  nodes without a shared filesystem — fine for private studies, not for
  official requests.

Either way the gridpack ends up **self-contained**. `make_tarball` packs
`mgbasedir` (which holds `models/`) and `process` into the `.tar.xz`, so the
model travels inside the tarball and the showering step needs no separate
copy of it.

Verified against [`cms-sw/genproductions`](https://github.com/cms-sw/genproductions/blob/master/bin/MadGraph5_aMCatNLO/gridpack_generation.sh);
the CMSDAS exercise uses the newer `cms-gen/genproductions_scripts` split-off,
where this logic is inherited but worth a one-line confirmation on first use:

```bash
grep -n "extramodels" genproductions_scripts/bin/MadGraph5_aMCatNLO/gridpack_generation.sh
```

### 2. The tarball script names are backwards from what you'd guess — `_cvmfs` is the one that reads a local path

Both `ExternalLHEProducer` helper scripts live in
`GeneratorInterface/LHEInterface/data/`, and the naming misleads:

- **`run_generic_tarball_cvmfs.sh`** takes a single **plain filesystem path**
  (`path=${1}`, then nevt/rnum/ncpu). "cvmfs" is where such tarballs usually
  sit, *not* a requirement — a local scratch path works fine.
- **`run_generic_tarball.sh`** takes the location as **two** arguments
  (`repo=${1}`, `name=${2}`) and fetches via
  `fn-fileget -c $(cmsGetFnConnect frontier://smallfiles) ${repo}/${name}`.
  It has **no local-file code path at all** and cannot read a gridpack off
  disk.

So for a gridpack you just built — the normal private/BSM case — use the
`_cvmfs` script, even though the tarball is not on `/cvmfs`:

```python
externalLHEProducer = cms.EDProducer('ExternalLHEProducer',
  scriptName = cms.FileInPath("GeneratorInterface/LHEInterface/data/run_generic_tarball_cvmfs.sh"),
  outputFile = cms.string("cmsgrid_final.lhe"),
  numberOfParameters = cms.uint32(1),
  args = cms.vstring('/absolute/path/to/your_model_gridpack.tar.xz'),
  nEvents = cms.untracked.uint32(10000)
)
```

Getting this backwards fails in a way that does not point at the cause: the
path becomes `repo`, `ExternalLHEProducer`'s appended nEvents becomes `name`,
and the job tries to fetch `<tarball>.tar.xz/100` over frontier, ending in
`FileOpenError ... Failed to open the file 'cmsgrid_final.lhe'` — several
minutes in, after Geant4 starts. Full symptom and the quoted source of both
scripts are in `references/pitfalls.md` P2.

### 3. The tutorial's Rivet routines are SM-ttbar-specific; they will not validate a BSM sample

`rivetAnalyzer.AnalysisNames` in the example fragments (`CMS_2016_I1491950`,
`MC_TTBAR`, ...) are ttbar analyses. For an arbitrary UFO model there is
usually no matching published routine. Pick one:

- **Generic building-block analyses** for a first sanity pass — `MC_XS`,
  `MC_FSPARTICLES`, `MC_GENERIC` — these book histograms of any final state
  without assuming a physics channel.
- **heptapod's own analysis chain**, which is model-agnostic by construction
  and doesn't require a Rivet routine to exist: `LHEToJSONL` →
  `EventJSONLToNumpy` → `ApplyCuts`/`CalculateInvariantMass`/`GetHardestN` on
  the LHE the gridpack produces, *before* spending CMSSW compute on a full
  GEN-SIM chain. Do this first — it's the fast, cheap check.
- **A custom Rivet routine** if you need per-event acceptance/efficiency
  histograms wired into the CMSSW chain itself — mechanically straightforward
  (`init()`/`analyze()`/`finalize()`, then `scram b -j8` inside the `Rivet`
  checkout), but only worth it once the model-agnostic checks above pass.

## Before building a gridpack

1. **Does the process run cleanly by hand first?** A gridpack build repeats
   the full diagram generation + integration-grid setup with no interactive
   feedback loop — debug the process interactively (`mg5` skill) before
   spending the time here.
2. **How will the model be resolved at build time** — bare name (stock, or
   published + `_extramodels.dat`) or absolute path (private, local)? Pitfall
   1. Decide before writing the proc card; it's the line that most often
   fails first.
3. **Will Pythia8 know what the new particles are?** It learns them from the
   `QNUMBERS` blocks MG5 writes into the LHE header, not from the UFO. Codes
   colliding with SM particles are silently ignored (pitfall P5).
4. **Do any BSM states need a decay that MG5 didn't compute?** If a state is
   left stable in the LHE on purpose (NLSP-style, decay handled downstream),
   you need a Pythia decay-table fragment — `BuildDecayTableTool` (`bsm`
   bundle) builds one from a parent PDG id, mass, and product list. Its output
   is `.cmnd`-syntax Pythia commands; paste those lines into the fragment's
   `processParameters = cms.vstring(...)` block (they are the same syntax,
   just wrapped differently for CMSSW vs. standalone Pythia8).
5. **Where will the tarball actually live** when `ExternalLHEProducer` reads
   it — local disk (pitfall 2, use the non-cvmfs script) or officially
   published to cvmfs?
6. **What will validate the sample** — an existing Rivet routine, the generic
   analyses, heptapod's own LHE analysis chain, or a custom routine (pitfall
   3)? Decide before running the full CMSSW chain, since that's the expensive
   step to repeat.

## Running

```bash
# 0. Environment — a fresh shell needs both; after a dropped ssh session,
#    re-running cmsenv from src/ is enough (no need to redo cmsrel)
source /cvmfs/cms.cern.ch/cmsset_default.sh
cmsrel CMSSW_X_Y_Z && cd CMSSW_X_Y_Z/src && cmsenv

# 1. Validate the process interactively first (mg5 skill) — do not skip this
#    ./bin/mg5_aMC, or heptapod's MadGraphFromRunCardTool / ValidateProcessTool

# 2. Assemble the cards directory (see references/proc_card_template.dat; and
#    pitfall 1 for how the model is resolved — absolute path in the proc card
#    for a private UFO, or _extramodels.dat if it's published to the CMS area)
mkdir -p cards/<process_name>
$EDITOR cards/<process_name>/<process_name>_proc_card.dat
# Don't hand-write the run_card: `output <process_name>` in an interactive
# MG5 session writes a fully-populated, process-appropriate default to
# Cards/run_card.dat — copy that in and edit only the knobs you need
# (nevents, iseed, ebeam1/2, cuts). NLO and LO run cards have different keys.
cp path/to/interactive/output/Cards/run_card.dat \
   cards/<process_name>/<process_name>_run_card.dat

# 3. Build the gridpack (outside any CMSSW environment)
#    CERN GitLab (needs an ssh key), or the public GitHub mirror-of-record:
#      git clone https://github.com/cms-sw/genproductions.git
git clone ssh://git@gitlab.cern.ch:7999/cms-gen/genproductions_scripts.git
cd genproductions_scripts/bin/MadGraph5_aMCatNLO
# The cards dir argument is RELATIVE to this directory, not absolute: the
# script does `CARDSDIR=${PRODHOME}/${carddir}` with no leading-slash check,
# so an absolute path is concatenated onto PRODHOME and reported as
# "<PRODHOME>//abs/path  does not exist!". Easiest is to keep the cards under
# this tree, as the repo's own cards/production/... layout does.
./gridpack_generation.sh <process_name> cards/<process_name>

# 4. Wire the gridpack into a CMSSW fragment (references/gridpack_fragment_template.py)
#    — pick the local-path or cvmfs ExternalLHEProducer script (pitfall 2)
cd $CMSSW_BASE/src
mkdir -p Configuration/GenProduction/python
cp fragment.py Configuration/GenProduction/python/
scram b -j4

# 5. Build and run the cfg — adjust --conditions/--era/--geometry to your
#    campaign; these are the values the tutorial cards were validated against,
#    not universal constants
cmsDriver.py Configuration/GenProduction/python/fragment.py \
  --python_filename gridpack_cfg.py \
  --eventcontent RAWSIM,LHE,NANOAODGEN --datatier GEN,LHE,NANOAOD \
  --fileout file:events.root \
  --conditions 140X_mcRun3_2024_realistic_v26 --beamspot DBrealistic \
  --step LHE,GEN,NANOGEN --geometry DB:Extended --era Run3_2024 \
  --no_exec --mc -n 5000
cmsRun gridpack_cfg.py

# 6. Validate (pitfall 3) — Rivet, or heptapod's own chain on the LHE first:
#    LHEToJSONLTool → EventJSONLToNumpyTool → ApplyCutsTool / GetHardestNTool
rivet-mkhtml --mc-errs *.yoda   # only if a matching routine exists
```

## Reference files

- **`references/pitfalls.md`** — the full pitfall list, with the relevant
  `gridpack_generation.sh` code quoted: `_extramodels.dat` being network-only
  and the two workarounds, the misleadingly-named tarball scripts (`_cvmfs` is
  the local-path one), MLM jet-matching double-counting when adding extra
  partons at LO, stable BSM states surviving the LHE, `QNUMBERS` blocks and
  silently-ignored colliding PDG codes, SCRAM_ARCH/genproductions-MG5-version
  mismatches, and the empty-`processParameters` segfault.
- **`references/proc_card_template.dat`** — a minimal genproductions-style
  proc card (no `launch`; `import model`/`generate`/`output` only), with all
  three `import model` cases from pitfall 1 spelled out.
- **`references/gridpack_fragment_template.py`** — a CMSSW GEN fragment
  template for the gridpack step, with both `ExternalLHEProducer` script
  variants (pitfall 2), the Pythia8 CP5-tune block, and a commented insertion
  point for a `BuildDecayTableTool` decay-table fragment.
- **`references/external_lhe_fragment_template.py`** — the lighter-weight
  fragment for showering an LHE file directly (no gridpack), useful as a fast
  local sanity check of the shower step before committing to a full gridpack
  build.
