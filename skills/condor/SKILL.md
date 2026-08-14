---
name: condor
bundle: condor
description: Run CMSSW work on HTCondor rather than interactively — the default for any cmsRun job past a quick check. Use whenever the user mentions condor, condor_submit, condor_q, batch jobs, JDL/submit files, grid/farm submission, splitting a production across jobs, or asks why a batch job is held, idle, or produced identical events. Also use proactively before running cmsRun for more than a few hundred events, before a multi-step GEN-SIM → NanoAOD chain, and whenever a job needs a file from a personal work area — the worker node cannot see /uscms_data, $HOME, or POSIX EOS, which is the single most common cause of batch failures that never occur interactively.
---

# Running CMSSW on HTCondor

**Batch is the default.** For anything beyond a short interactive check, CMSSW
work belongs on HTCondor: `cmsRun` is single-threaded by default, GEN-SIM
dominates the cost of a production chain, and the work splits cleanly by random
seed. Running it interactively ties up a login node for hours and does not
scale past one sample.

Use `CMSSWJobSubmitTool` (bundle `condor`), which defaults to `use_condor=True`
and encodes the failure modes below. Setting `use_condor=False` emits an
equivalent local run script — the documented escape hatch, for a five-event
sanity check or when debugging the cfg itself.

```
cmsDriver.py --no_exec --python_filename step1_cfg.py ...
        │
        ▼
CMSSWJobSubmitTool ──▶ submit.jdl + run_cmssw.sh   (+ optional condor_submit)
        │
        ▼
CondorJobStatusTool ──▶ counts, hold reasons, error-log tails
```

## The one fact that dictates job design

The worker node is not your shell. The job runs inside an apptainer container
started with `--contain`, which binds only `/cvmfs`, `/etc/hosts` and
`/etc/grid-security`:

| | |
|---|---|
| `/cvmfs` | present — `cmsset_default.sh`, `scram`, `cmsRun`, cvmfs gridpacks all work |
| `/uscms_data`, `$HOME`, POSIX `/eos` | **absent** |
| `$PWD` | condor scratch, empty at start, TB-scale free |
| `$X509_USER_PROXY` | present if you ship a proxy |

So a job cannot open anything in your work area. Every input arrives via
`transfer_input_files`, from `/cvmfs`, or over xrootd — and a cfg holding an
absolute path to a gridpack in your scratch area will fail on the worker
having run fine interactively. `CMSSWJobSubmitTool` scans the cfg for exactly
this and warns before you submit.

Measured details, and a probe job for re-verifying them at another site, are in
`references/lpc_environment.md`.

## Four silent failures the tool exists to prevent

**1. Unseeded jobs.** `queue 100` without per-job seeds gives 100 identical
copies of the same events. Nothing errors; the merged sample simply has a
hundredth of the statistics it appears to. The generated wrapper derives
`SEED = first_seed + $(Process)` and applies it to *every* module registered
with `RandomNumberGeneratorService` — `generator`, `externalLHEProducer`,
`VtxSmeared`, whatever the cfg has — plus `firstLuminosityBlock`, so merged
output has no duplicate lumi IDs.

**2. Unset `transfer_output_files`.** The default returns every new file in the
job's scratch directory. Since the wrapper runs `cmsrel` there, that is a
multi-GB release area copied back per job. The generated JDL always sets the
directive explicitly, and sets it *empty* rather than omitting it when staging
out to EOS.

**3. Wrong container for the release.** Default image is EL9. An
`el8_amd64_gcc10` release needs EL8, and the symptom is a missing-library error
that never mentions the image. Interactively the fix is `cmssw-el8`; in a job
it is a ClassAd, `+ApptainerImage`. The tool derives it from `scram_arch` and
refuses an arch it does not recognize rather than guessing.

**4. A submit directory the schedd cannot read.** Submission is often
load-balanced across schedd hosts separate from your login node, and the schedd
reads `transfer_input_files` itself. A submit directory on node-local disk
(`/tmp`) fails with `(errno 2) No such file or directory` — which reads as a
missing file and is not. The tool rejects such a `job_dir`;
`allow_nonshared_submit_dir=True` overrides it where the heuristic is wrong.

## Splitting a production chain

Costs are not uniform. Representative wall times for 100 events of a private
BSM sample: GEN-SIM ~3900 s, DIGI-HLT ~410 s, RECO ~570 s, MiniAOD and NanoAOD
negligible. Therefore:

- **Split GEN-SIM by seed** — one job per few hundred events. This is the step
  that needs the farm.
- **Chain the cheap steps inside one job** rather than paying `cmsrel` and file
  transfer five times over. One wrapper running GEN-SIM → … → NanoAOD on its
  own slice, emitting only the NanoAOD, is usually the right granularity.
- **Premix DIGI needs the network.** The pileup library is read over xrootd, so
  those jobs need a valid proxy and show wide wall-time scatter. Start at
  `request_memory_mb=8000`.
- **Merge at the end** with `hadd`, or `edmCopyPickMerge` for EDM files.

## Before submitting

1. **Does the cfg run at all?** Run one job locally with `use_condor=False` and
   a handful of events first. A cfg bug costs one job interactively and N jobs
   on the farm.
2. **Is every input reachable from the worker?** Gridpack, fragment, and any
   input file: transferred, on `/cvmfs`, or via xrootd. Heed the tool's
   reachability warnings — they are the commonest batch-only failure.
3. **Is the arch right?** `scram_arch` must be the one the release was built
   for, and must match the gridpack's (pitfall P6 in the `cmssw` skill).
4. **Is there a proxy, of the right kind, with enough life?** A plain grid
   proxy is enough to write your own EOS area but is **rejected** for reading
   CMS central data such as the premix pileup library, which needs VOMS
   attributes. Check `voms-proxy-info -all | grep ^VO` shows `cms`, not just
   that a proxy exists — the EOS test passes either way, so the failure only
   appears once a premix job is eight minutes in. `voms-proxy-init` needs a
   TTY, so it cannot be run from an agent session:
   `voms-proxy-init -rfc -voms cms -valid 192:00`.
5. **Where does output go?** Small outputs come back through the schedd;
   anything large should `xrdcp` to EOS via `output_eos_dir`, which also
   empties `transfer_output_files`.
6. **Do the seeds differ?** Automatic here, but verify if you hand-edit the
   wrapper. Also avoid seed 0, which Pythia8 reads as "seed from the clock".

## Running

```python
# Default: HTCondor. Writes submit.jdl + run_cmssw.sh, submits nothing.
CMSSWJobSubmitTool(
    cfg_file="step1_cfg.py",
    cmssw_release="CMSSW_12_4_8",
    scram_arch="el8_amd64_gcc10",     # -> cms:rhel8 image
    n_jobs=50,
    first_seed=1000,                  # jobs get 1000..1049
    output_files=["step1.root"],
    transfer_input_files=["model_gridpack.tar.xz"],
    job_dir="condor_jobs/gensim",     # must be on shared storage
    request_memory_mb=4000,
)
```

Then submit, either by passing `submit=True` or by running the returned
command yourself. Submission is left explicit by default because it is the
step that consumes shared resources.

```python
CondorJobStatusTool(cluster_id="30052257", job_dir="condor_jobs/gensim")
```

An empty result is ambiguous — the cluster either finished or lives on another
schedd — so the tool says so rather than implying success. Pass `schedd_name`
(reported by `condor_submit`) to disambiguate.

Debugging by hand:

```bash
condor_q -name <schedd> <cluster> -af JobStatus HoldReason
#   1 idle  2 running  3 removed  4 completed  5 held
condor_q -better-analyze -name <schedd> <cluster>   # why is it not matching?
condor_ssh_to_job <cluster>.<proc>                  # shell into a RUNNING job
condor_history <cluster>
```

`condor_ssh_to_job` is the fastest way to settle an environment question — it
drops you in the job's scratch directory inside the same container.

## Reference files

- **`references/lpc_environment.md`** — the measured worker-node environment
  (what is and is not mounted, the image ClassAd, EOS stage-out with a plain
  grid proxy), the hand-written JDL and wrapper equivalents of what the tool
  generates, and a probe job for re-verifying all of it at another site.

## Related skills

- **`cmssw`** — building the gridpack and writing the GEN fragment that
  produce the cfg submitted here, plus the pitfalls that make a cfg wrong
  before batch is even involved.
- **`central-production`** — reproducing an official dataset's chain; its
  per-step `cmsDriver` commands are the natural input to this skill, one
  cluster per step.
