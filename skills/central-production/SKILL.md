---
name: central-production
bundle: mg5
description: Reproduce an existing CMS central Monte Carlo dataset's full production chain locally — find it on GrASP/McM/DAS, pull each step's cmsDriver command from the McM chained request, and run GEN-SIM → DIGI-HLT-RECO (Premix pileup) → MiniAOD → NanoAOD end to end, usually crossing two different CMSSW releases partway through. Use whenever the user mentions McM, GrASP, DAS/dasgoclient, a "chained request", Premix pileup, voms-proxy-init, NANOEDMAODSIM vs NANOAODSIM, or wants to regenerate/reproduce an official/central dataset by name rather than build a private UFO gridpack. Also use proactively before running a multi-step cmsDriver chain copied from McM — the release boundary between steps and the root fragment's built-in filters both fail silently if copied verbatim.
---

# Reproducing a central CMS MC dataset: McM chain → cmsDriver → cmsRun

This is a different starting point from the `cmssw` skill. That one builds a
gridpack from a **private/BSM** UFO model and stops at GEN/LHE/NanoGEN — there
is no officially registered dataset to compare against. Here you start from an
**existing, officially produced** dataset (found by name or by browsing) and
walk its *entire* chain through full detector simulation to NanoAOD, using
CMS's own McM production requests as the source of truth for every
`cmsDriver` command. If your goal is a private model's events, use `cmssw`. If
your goal is "make me events that look like dataset X" or "run the full
central-production chain", this is the one.

```
GrASP (dataset search) ──▶ McM chained request ──▶ per-step cmsDriver command
                                                          │
       root fragment (get_fragment REST API) ────────────┤
                                                          ▼
   cmsDriver.py × N  ──▶  cmsRun × N     (often 2+ CMSSW releases)
   GEN-SIM → DIGI-HLT-RECO (Premix) → MiniAOD → NanoAOD
```

Written against the [CMS PPD short exercise](https://gitlab.cern.ch/cmsdas-hamburg-2025/ppd-short-exercise)
(`ppd-short-exercise/exercise.pdf`, `ppd-short-exercise/mc_prod.sh`). The
exercise's assigned target dataset is
`TTto2L2Nu_HT-500_NJet-7_TuneCP5_13p6TeV_powheg-pythia8`; the `mc_prod.sh`
script it ships is a worked example for a **different** dataset
(`TOP-Run3Summer22EEwmLHEGS-00001`), meant to show the *shape* of the answer,
not to be copied literally — its release names, conditions strings, and where
the release boundary falls are specific to that one chain. Treat every value
below the same way: verified for this worked example, not universal (the
`cmssw` skill's `--conditions`/`--era`/`--geometry` caveat applies here too).

## 1. Find the chain on GrASP, don't hand-assemble it

The index is [GrASP](https://cms-pdmv-prod.web.cern.ch/grasp/) — search by
process-name wildcard (`TTto2L2Nu*`, `DYJetsTo*`, ...). Each result row has
**Root Request**, **MiniAOD Request**, **NanoAOD Request**, and **Chained
Request** columns; the chained request is the one that lists every step in
order. Before touching McM, sanity-check you found the right sample from the
dataset name itself — it's a structured string:

```
/TTto2L2Nu_HT-500_NJet-7_TuneCP5_13p6TeV_powheg-pythia8/RunIISummer20UL18MiniAODv2-106X_upgrade2018_realistic_v16_L1v1-v2/MINIAODSIM
   \___________________ process + cuts __________________/ \_______ campaign _______/ \___ global tag ___/ \version/ \_format_/
```

DAS (`https://cmsweb.cern.ch/das`, or `dasgoclient` from the shell) confirms
the dataset actually exists and lists its files — useful once you have a name
from GrASP/McM and want to check it independently.

Inside McM, open each request and use **Select View → tick "Fragment"** to
get the exact CMSSW config snippet used, and the request's Actions icon to
get the ready-made `cmsDriver` command — don't retype `--conditions`/`--era`
from memory or from a different campaign's exercise notes.

## 2. One chained request is not one cmsDriver call

> "There are 4 requests and 5 cmsDrivers" — `ppd-short-exercise/exercise.pdf`, slide 30

The DIGI-RECO ("DRPremix") request alone expands into **two** separate
`cmsDriver` invocations, not one — confirmed in `mc_prod.sh`:

```bash
## cmsDriver command step 2 — DIGI/L1/HLT
cmsDriver.py --step DIGI,DATAMIX,L1,DIGI2RAW,HLT:2022v14 --procModifiers premix_stage2,siPixelQualityRawToDigi ...

## cmsDriver command step 3 — RECO, same request
cmsDriver.py --step RAW2DIGI,L1Reco,RECO,RECOSIM --procModifiers siPixelQualityRawToDigi ...
```

So: open **every** request in the chain and read its own generated command
rather than assuming request-count equals cmsDriver-count.

## 3. The root fragment carries the original request's filters — strip them for an unfiltered reproduction

The fragment is fetched from McM's public REST API, not written by hand:

```bash
curl -s -k https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_fragment/<request-id> \
  -o Configuration/GenProduction/python/<request-id>-fragment.py
```

`mc_prod.sh` marks the next line with a literal `# HERE YOU NEED TO MODIFY
THE FRAGMENT!` — the script leaves this as a manual step because which filter
to remove is dataset-specific. If the assignment is to reproduce the process
**without** the original selection (as in the PPD exercise's own target:
"Produce 50 NanoAOD v12 events looking like [dataset] **without the cuts
from the central sample**"), you must open the fetched fragment and delete
the kinematic/decay filter block yourself — leaving it in place silently
reproduces the exact original selection instead of a generic sample.

## 4. The chain can cross CMSSW releases mid-way — check every step's own release, and re-point `--filein`

`mc_prod.sh` sets up two releases:

```bash
scram p CMSSW CMSSW_12_4_11_patch3   # GEN-SIM, DIGI, RECO, MiniAOD
...
scram p CMSSW CMSSW_12_6_0_patch1    # NanoAOD only
```

and the NanoAOD step's `--filein` reaches back across that boundary with a
relative path into the *other* release's `src/`:

```bash
--filein file:../../CMSSW_12_4_11_patch3/src/TOP-Run3Summer22EEMiniAODv3-00001.root
```

The general failure mode this guards against: McM's auto-generated command
for a downstream step is written assuming its input comes from a **DBS**
dataset (i.e. official central production reading another official dataset),
not from a file you just produced locally. When chaining your own local
output, `--filein` in the command McM hands you needs to be overridden to
`file:<your local output>.root` — `ppd-short-exercise/exercise.pdf` slide 32
calls this out explicitly for the MiniAOD step of its target chain
("Warning: different CMSSW version! ... change to `--filein
file:yourAODFile.root`"). Note that where exactly the release boundary falls
differs between the exercise's assigned dataset (MiniAOD) and the `mc_prod.sh`
worked example (NanoAOD) — check each request's own recorded CMSSW release in
McM rather than assuming the boundary is always at the same step.

## 5. Premix pileup needs a grid proxy — and fails at run time, not config time

```bash
--pileup_input "dbs:/Neutrino_E-10_gun/Run3Summer21PrePremix-Summer22_124X_mcRun3_2022_realistic_v11-v2/PREMIX" \
--procModifiers premix_stage2,siPixelQualityRawToDigi
```

`cmsDriver.py` happily writes this string into the DIGI config with no proxy
at all — it's just text at that point. The read only happens when `cmsRun`
tries to actually open the DBS-registered pileup files over xrootd, so a
missing proxy surfaces as a file-open failure partway through the DIGI step,
not as a config-time error. Run `voms-proxy-init -voms cms` (check with
`voms-proxy-info`) **before** starting the DIGI step, not after it fails.

## 6. NanoAOD step: EDM wrapper vs. flat ntuple is a deliberate choice

`ppd-short-exercise/exercise.pdf` slide 32: "Change `NANOEDMAODSIM` to
`NANOAODSIM` to get flat ntuples." McM's auto-generated command for a
NanoAOD request will hand you `--eventcontent NANOEDMAODSIM --datatier
NANOEDMAODSIM` by default — the EDM-wrapped variant, meant for further
CMSSW-side processing. For analysis-ready flat ntuples (uproot/RDataFrame),
swap both flags to `NANOAODSIM`, as `mc_prod.sh`'s own NanoAOD step already
does:

```bash
cmsDriver.py --eventcontent NANOAODSIM --datatier NANOAODSIM --step NANO ...
```

## Environment and checklist

On an EL9 host, don't skip the EL8 container just because an early step ran
fine unwrapped — that is not evidence the rest of the chain will. Observed on
`CMSSW_13_0_13` (Run3Summer22EE, `el8_amd64_gcc11`): GEN-SIM and DIGI-RECO
both completed unwrapped, and then the MiniAOD/PAT step pulled in a plugin
needing `libssl.so.1.1`, which EL9 doesn't ship, and died at `cmsRun` time:

```
cmsRun: error while loading shared libraries: libssl.so.1.1: cannot open shared object file
```

Which step trips first — and which library it asks for — is release-specific;
verified for this one chain, not universal. What generalizes is the shape of
the failure: it surfaces several steps in, long after an unwrapped run has
already looked safe. So wrap production commands from the start with
`cmssw-el8 --command-to-run bash <script>`
(`cmssw-el8` lives at `/cvmfs/cms.cern.ch/common/cmssw-el8`).

```bash
# 0. EL8 container + grid proxy — needed before anything else
cmssw-el8
voms-proxy-init -voms cms

# 1. Per release: cmsrel + cmsenv (repeat at each release boundary, §4)
source /cvmfs/cms.cern.ch/cmsset_default.sh
export SCRAM_ARCH=el8_amd64_gcc10
cmsrel CMSSW_X_Y_Z && cd CMSSW_X_Y_Z/src && cmsenv

# 2. Fetch + edit the root fragment (§3), place it, build
mkdir -p Configuration/GenProduction/python
curl -s -k https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_fragment/<id> \
  -o Configuration/GenProduction/python/<id>-fragment.py
$EDITOR Configuration/GenProduction/python/<id>-fragment.py   # strip filters if needed
scram b

# 3. Run every request's cmsDriver command in chain order (§2), --nThreads 8 for speed
cmsDriver.py Configuration/GenProduction/python/<id>-fragment.py ... --step LHE,GEN,SIM ...
cmsRun <cfg>.py
# then DIGI/HLT, RECO, MiniAOD, NanoAOD in turn, watching for the release
# boundary (§4) and re-pointing --filein when it happens

# 4. Sanity-check each output before moving to the next step
edmDumpEventContent file:<output>.root
python3 -c "import ROOT; f=ROOT.TFile('<output>.root'); print(f.Events.GetEntries())"
```

## Reference files

- **`references/mc_prod_example.sh`** — the full `ppd-short-exercise/mc_prod.sh`
  worked example, annotated with which pitfall (§1–§6 above) each block
  demonstrates. Reproduces `TOP-Run3Summer22EEwmLHEGS-00001`, a *different*
  dataset from the exercise's own assigned target — use it for chain shape
  and flag patterns, not literal values.
