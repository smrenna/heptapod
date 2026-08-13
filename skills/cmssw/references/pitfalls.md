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

## P2 — `run_generic_tarball_cvmfs.sh` vs. `run_generic_tarball.sh` — the `_cvmfs` one is what you want for a LOCAL gridpack

The names are actively misleading. Despite the suffix, **`_cvmfs` is the
variant that takes a plain filesystem path**, and the un-suffixed script is the
one that cannot read local disk at all. Both scripts are short; read them in
your release rather than trusting the names:

`run_generic_tarball.sh` takes the gridpack location as **two** arguments and
fetches it through the CMS frontier *smallfiles* service — there is no local-file
code path in it:

```bash
repo=${1}; name=${2}; nevt=${3}; rnum=${4}; ncpu=${5}
fn-fileget -c `cmsGetFnConnect frontier://smallfiles` ${repo}/${name}
```

`run_generic_tarball_cvmfs.sh` takes a single path and uses it directly:

```bash
path=${1}; nevt=${2}; rnum=${3}; ncpu=${4}
```

The `cvmfs` in that name describes where such tarballs *usually* live, not a
requirement — any mounted path works, including scratch/`/uscms_data`.

**Symptom if you get it backwards** (verified on CMSSW_12_4_11_patch3, with
`numberOfParameters = 1` and one local path in `args`): the path lands in
`repo`, and `ExternalLHEProducer`'s automatically-appended nEvents becomes
`name`, so the job tries to fetch the literal string `<tarball>.tar.xz/100`
over frontier:

```
%MSG-MG5 gridpack = 100
Error getting data for /path/to/..._tarball.tar.xz/100: [frontier.c:1136]: No more proxies.
  ... smallfiles Exception: Cannot read file /path/to/..._tarball.tar.xz/100
tar: Error is not recoverable: exiting now
run_generic_tarball.sh: line 46: ./runcmsgrid.sh: No such file or directory
----- Begin Fatal Exception -----
An exception of category 'FileOpenError' ... Failed to open the file 'cmsgrid_final.lhe'
```

The `FileOpenError` on `cmsgrid_final.lhe` is a downstream symptom — the LHE was
never produced because the fetch failed. Note this surfaces only *after* Geant4
initialisation, several minutes into the job.

**Fix**: use `run_generic_tarball_cvmfs.sh` with the tarball's absolute
filesystem path, whether or not it is on `/cvmfs`. Reserve
`run_generic_tarball.sh` for gridpacks actually published to the frontier
smallfiles repository, and give it both `repo` and `name` arguments.

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

## P7 — An EMPTY `processParameters` block segfaults cmsRun with no error message

If your model needs none of the optional Pythia settings — the usual case when
the BSM states are decayed in the matrix element and no PDG code collides
(P4/P5 both inapplicable) — it is tempting to leave the block in place with
everything commented out:

```python
processParameters = cms.vstring(
    # nothing applies to this model
),
...
parameterSets = cms.vstring('pythia8CommonSettings',
                            'pythia8CP5Settings',
                            'processParameters')   # <-- still listed
```

That crashes. `ParameterCollector` walks each block named in `parameterSets`
and runs off the end of an empty one:

```
Module: Pythia8HadronizerFilter:generator (crashed)
A fatal system signal has occurred: segmentation violation

#5 gen::ParameterCollector::const_iterator::next()
#6 gen::Py8InterfaceBase::readSettings(int)
#7 edm::HadronizerFilter<Pythia8Hadronizer, gen::ExternalDecayDriver>::beginLuminosityBlockProduce(...)
```

There is no exception, no configuration error, and no hint that a parameter
block is to blame — just a signal and a ~250-byte ROOT stub. Because it fires
at `beginLuminosityBlock`, it happens *after* Geant4 initialisation, so several
minutes of the job look healthy first.

**Fix**: delete the block from the `PSet` **and** remove its name from
`parameterSets`. Present-and-empty is the broken state; absent is fine.

**While debugging any of this**: a segfault writes no "Fatal Exception" line
and still leaves a small non-empty output file, so `[ -s output.root ]` and
"grep for Fatal Exception" both report success on a crashed job. If you wrap
these steps in a script, gate on a completed `TimeReport> Time report complete`
plus a plausible file size, and grep for `segmentation violation` too.
