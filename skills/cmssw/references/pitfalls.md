# Pitfalls: UFO → gridpack → CMSSW

Detailed version of the failure modes summarized in `SKILL.md`. Sourced from
generalizing the [CMSDAS generator
exercise](https://gitlab.cern.ch/cmsdas-hamburg-2025/generator-short-excercise)
(a stock-SM-ttbar, officially-published-gridpack tutorial) to an arbitrary,
private UFO model.

## P1 — `_extramodels.dat` fetches over the network; it cannot see a local UFO

`gridpack_generation.sh` downloads and runs its own pinned MG5 install in a
sandbox, separate from any `mg5_path` you've configured for interactive work.
Its supported hook for a non-stock model is `<process_name>_extramodels.dat`
in the cards directory — but that hook is network-only. The actual code:

```bash
if [ -e $CARDSDIR/${name}_extramodels.dat ]; then
  sed 's:#.*$::g' $CARDSDIR/${name}_extramodels.dat | while read -r model || [ -n "$model" ]
  do
    if [[ $model = *[!\ ]* ]]; then
      wget --no-check-certificate https://cms-project-generators.web.cern.ch/cms-project-generators/$model
      cd models
      if   [[ $model == *".zip"* ]]; then unzip ../$model
      elif [[ $model == *".tgz"* ]]; then tar zxvf ../$model
      elif [[ $model == *".tar"* ]]; then tar xavf ../$model
      fi
      cd ..
    fi
  done
fi
```

The URL prefix is hardcoded, so each line is a **filename hosted in the CMS
generators web area** — not a URL, not a local path. Comments (`#`) are
stripped, blank lines skipped. Models land in the sandboxed MG5's `models/`,
where the proc card's `import model <name>` then resolves `<name>` against the
directory name *inside* the archive.

**Fix (official production)**: get the model archive published to the CMS
generators web area, then list its filename in `_extramodels.dat`.

**Fix (private/local studies)**: skip `_extramodels.dat` and give the proc
card an absolute path —

```
import model /absolute/path/to/my_bsm_model
```

MG5 resolves this during the code-generation step, which runs in `$WORKDIR` on
the machine invoking the script, so a plain filesystem path works. This breaks
if generation is farmed out to batch nodes without a shared filesystem, so
it's suitable for private studies and not for official requests.

**Either way the gridpack is self-contained.** `make_tarball` packs
`mgbasedir` (which contains `models/`) alongside `process`:

```bash
XZ_OPT="$XZ_OPT" tar -cJpf ${PRODHOME}/${name}_${scram_arch}_${cmssw_version}_tarball.tar.xz \
    mgbasedir process runcmsgrid.sh gridpack_generation*.log InputCards $EXTRA_TAR_ARGS
```

so the model travels inside the tarball. The model only has to be resolvable
at **build** time; the CMSSW showering step needs no separate copy.

Verified against [`cms-sw/genproductions`](https://github.com/cms-sw/genproductions/blob/master/bin/MadGraph5_aMCatNLO/gridpack_generation.sh).
The CMSDAS exercise uses the newer `cms-gen/genproductions_scripts` split-off,
which inherits this logic; confirm on first use with
`grep -n "extramodels" .../gridpack_generation.sh`.

## P2 — `run_generic_tarball_cvmfs.sh` vs. `run_generic_tarball.sh`

The tutorial's `ExternalLHEProducer` fragment uses
`GeneratorInterface/LHEInterface/data/run_generic_tarball_cvmfs.sh`. This
script expects `args` to be a path that resolves on `/cvmfs` — i.e. the
gridpack has already gone through CMS's official publication pipeline. A
gridpack you just built with `gridpack_generation.sh` sits on local disk and
is not there.

Using the `_cvmfs` script against a local path is a **quiet** failure mode:
the job doesn't complain about a malformed config, it just can't resolve the
tarball at run time, and the error can look like an infrastructure problem
rather than a wrong-script-choice problem.

**Fix**: for a locally-built gridpack, use
`GeneratorInterface/LHEInterface/data/run_generic_tarball.sh` (no `_cvmfs`
suffix), pointed at the tarball's actual filesystem path. Switch to the
`_cvmfs` variant only once the tarball has actually gone through official
publication — a separate CMS process, out of scope here.

## P3 — MLM jet matching is required, not optional, once you add an extra parton at LO

If the proc card generates both the base process and `+1 jet` at LO (`add
process p p > ... j`), the parton shower's own radiation and the explicit
matrix-element jet double-count the same phase-space region unless you turn
on MLM matching in the Pythia8 fragment. Skipping this doesn't error — it
silently produces a jet-multiplicity spectrum that's wrong (double-counted
soft/collinear jets), which is much harder to notice than a crash.

**Fix**: uncomment the `JetMatching:*` block in
`gridpack_fragment_template.py`. Key knobs to actually tune per-process:

- `JetMatching:qCut` — the merging scale; should sit comfortably above your
  generation-level `ptj` cut from the run card, scaled to the process's
  typical hard-scale.
- `JetMatching:nJetMax` — must match the maximum number of extra partons in
  the proc card (1 for `+1j`, etc.) or the matching accounting is wrong.
- `JetMatching:nQmatch` — 4 vs. 5 flavor scheme; matches whatever the run
  card's PDF set assumes.

This is orthogonal to any BSM content in the model — it's purely about the
number of light partons in the hard process.

## P4 — A BSM state left stable in the LHE needs an explicit decay downstream

If the UFO model's process leaves a new particle stable at the MG5 level
(deliberately — e.g. an NLSP whose decay you want computed with a specific
kinematic model rather than MG5's phase space), CMSSW's Pythia8 step will
also leave it stable unless told otherwise. This doesn't error either — the
sample just contains a stable exotic particle with no downstream physics,
which is easy to miss until someone asks why the final state looks wrong.

**Fix**: build a decay-table fragment with heptapod's `BuildDecayTableTool`
(`bsm` bundle) — give it the parent PDG id, mass, and product PDG ids. Its
output is `.cmnd`-syntax Pythia8 commands (the same syntax CMSSW's
`processParameters` block uses, just wrapped as a Python vstring list rather
than a bare `.cmnd` file). Paste the lines directly into
`processParameters = cms.vstring(...)`.

## P5 — Pythia8 learns your BSM particles from the LHE's QNUMBERS blocks — and silently ignores one whose PDG code collides with the SM

Pythia8 has no knowledge of your UFO model. It learns new states from
**`QNUMBERS` blocks** in the SLHA header that MG5 writes into the LHE: these
carry the SM quantum numbers (spin, charge, colour, self-conjugacy) that
Pythia8 needs for QED/QCD showering and colour-flow tracing. This is why the
gridpack path works at all without shipping the UFO to CMSSW.

Two ways this goes wrong, both quiet:

1. **PDG code collides with an SM particle.** Pythia8 treats everything in a
   `QNUMBERS` block as BSM *unless that PDG code is already assigned to an SM
   particle* — in which case the block is ignored and Pythia8 uses its
   built-in SM properties instead. Your "new" state then showers with the
   wrong mass, charge, and colour, with no error. The `feynrules` skill's
   rule of picking codes from 9000001 upward is what prevents this; if it was
   skipped upstream, the symptom appears here, two tools downstream of the
   mistake.
2. **The block is missing entirely** — e.g. an LHE assembled by hand, or a
   pipeline step that rewrote the header. Pythia8 then has no properties for
   the code at all.

**Check before blaming the fragment.** The header is plain text at the top of
the LHE (`cmsgrid_final.lhe` from `ExternalLHEProducer`, or the gridpack's
own test-run LHE):

```bash
grep -A5 -i "QNUMBERS" cmsgrid_final.lhe | head -40
```

Every BSM PDG code in your process should appear with sensible quantum
numbers. If a supposedly-new state shows implausible mass/charge in the GEN
output, verify its code here before debugging the CMSSW fragment.

See also P4: `QNUMBERS` tells Pythia8 *what the particle is*, not *how it
decays*. A state with a correct `QNUMBERS` block and no decay table is still
left stable.

## P6 — SCRAM_ARCH / genproductions-MG5 version mismatch

`gridpack_generation.sh` downloads a MG5 build matched to the CMSSW release's
`SCRAM_ARCH` (visible in the tarball naming convention, e.g.
`..._el9_amd64_gcc11_CMSSW_13_2_9_tarball.tar.xz`). Building a gridpack from
one CMSSW environment and trying to run `ExternalLHEProducer` for it inside a
CMSSW release on a different architecture will fail. Keep the `cmsenv` you
built the gridpack under and the one you shower it in on the same
`SCRAM_ARCH` unless you know the gridpack is architecture-portable (LHE
generation is compiled Fortran/C++, not necessarily portable).
