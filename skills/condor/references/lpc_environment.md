# Running cmsRun on HTCondor (measured at FNAL LPC)

Every statement here was verified by submitting probe jobs and reading what
came back, not recalled from documentation. Re-verify with the probe at the
bottom if you are on a different site or the site changes.

## The execution environment is not your shell

This is the fact that dictates the whole job design. An LPC worker node runs
your executable inside an apptainer container launched with `--contain`:

```
apptainer exec --pid --ipc --contain \
  --bind /cvmfs --bind /etc/hosts --bind /etc/grid-security \
  --home <execute_dir>:/srv --pwd /srv \
  /cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel9 ./your_script.sh
```

Measured consequences:

| | |
|---|---|
| `/cvmfs` | **present** — `cmsset_default.sh`, `scram`, `cmsRun`, gridpacks on cvmfs all work |
| `/uscms_data/d1`, `d2`, `d3` | **absent** |
| `/uscms/home/<user>` | **absent** |
| `/eos/uscms` (POSIX) | **absent** — reach EOS over xrootd instead |
| `$PWD` = `/srv` | condor scratch; 3.5 TB free on the node tested |
| `$X509_USER_PROXY` | `/srv/x509up_uNNNN`, transferred in, usable |

So a job **cannot** read your work area. Anything it needs — gridpack,
fragment, cfg — arrives via `transfer_input_files`, is pulled from `/cvmfs`,
or is fetched over xrootd. `cmsrel` in `/srv` works fine, so building a
release area inside the job is a normal thing to do.

## Three things that will bite you

**1. The schedd is a different machine from your login node.** LPC submission
is load-balanced across `lpcschedd5/6/...`, and `condor_submit` reports which
one it picked. Your submit directory must be on a filesystem *the schedd* can
read. Submitting from `/tmp` on `cmslpcNNN` fails with:

```
Transfer input files failure ... reading from file /tmp/.../probe.sh:
(errno 2) No such file or directory
```

which reads like a missing file but means "not shared." Submit from
`/uscms_data/...` or `/uscms/home/...`. It also means you must name the schedd
when querying a specific job: `condor_q -name lpcschedd6.fnal.gov <cluster>`.

**2. Default `transfer_output_files` will drag back the entire scratch dir.**
If you `cmsrel` inside the job, that is a multi-GB CMSSW area copied to your
submit directory. Always name the outputs explicitly:

```
transfer_output_files = <the>.root, <files>.log
```

or write to EOS with `xrdcp` and set `transfer_output_files = ""`.

**3. The EL8/EL9 mismatch shows up here too, but with a different fix.**
Default image is `cms:rhel9` (AlmaLinux 9.8). A gridpack or release built for
`el8_amd64_gcc10` needs EL8, and interactively that is `cmssw-el8`; in a job it
is a ClassAd:

```
+ApptainerImage = "/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel8"
```

Verified: the job then reports AlmaLinux 8.10, and `/usr/lib64/libssl.so.1.1`
— the library whose absence breaks EL8 releases on EL9 — is present. That is
the batch-side cure for the same failure documented in the
`central-production` skill.

## A working submit file

Tested end to end on LPC. `queue N` gives `$(Process)` = 0..N-1, which is the
natural per-job seed offset.

```
universe                = vanilla
executable              = run_step.sh
arguments               = $(Process)

should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
transfer_input_files    = <fragment_or_cfg>.py, <gridpack>.tar.xz
transfer_output_files   = out_$(Process).root

output                  = job.$(Cluster).$(Process).out
error                   = job.$(Cluster).$(Process).err
log                     = job.$(Cluster).log

request_cpus            = 1
request_memory          = 4000        # MB; GEN-SIM fits, DIGI-RECO wants more
request_disk            = 10000000    # KB

x509userproxy           = $ENV(X509_USER_PROXY)
+ApptainerImage         = "/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel8"

max_retries             = 3
queue 100
```

Note `request_cpus = 1` really does give the job `nproc == 1`. If you pass
`-n`/`--nThreads` to `cmsDriver.py`, raise `request_cpus` to match or the job
oversubscribes its slot.

And the wrapper it runs:

```bash
#!/bin/bash
set -e
PROC=$1

source /cvmfs/cms.cern.ch/cmsset_default.sh
export SCRAM_ARCH=el8_amd64_gcc10          # must match the gridpack
cmsrel CMSSW_12_4_8                        # ~1 min in /srv, verified
cd CMSSW_12_4_8/src
eval `scramv1 runtime -sh`
cd /srv

# Distinct seeds per job, or every job generates the SAME events.
SEED=$((1000 + PROC))
python3 - "$SEED" <<'PY'
import sys
seed = int(sys.argv[1])
cfg = open("step1_cfg.py").read()
cfg += f"""
process.RandomNumberGeneratorService.generator.initialSeed = {seed}
process.RandomNumberGeneratorService.externalLHEProducer.initialSeed = {seed}
process.source.firstLuminosityBlock = cms.untracked.uint32({seed})
"""
open("step1_seeded_cfg.py","w").write(cfg)
PY

cmsRun step1_seeded_cfg.py
mv <whatever>.root out_${PROC}.root
```

The seed handling is the part most often skipped, and it fails silently: N
identical jobs produce N copies of the same events, and the merged sample just
looks like it has better statistics than it does. `firstLuminosityBlock` also
needs to differ, or downstream merging sees duplicate lumi IDs.

## Getting output out

For modest files (our NanoAODv12 was 3 MB, GEN-SIM 113 MB per 100 events),
plain `transfer_output_files` back to the submit directory is simplest.

For anything larger, write to EOS from inside the job:

```bash
xrdcp -f out_${PROC}.root root://cmseos.fnal.gov//store/user/$USER/<dir>/out_${PROC}.root
```

Read back interactively with `xrdfs root://cmseos.fnal.gov ls -l /store/user/...`,
or POSIX via `/eos/uscms/store/user/...` from the login node (not from a job).

### Two kinds of proxy, and a trap between them

Writing to *your own* LPC EOS area works with a **plain grid proxy** — no VOMS
extension — because that endpoint authorizes on the certificate DN alone.
Verified.

Do not generalize that. Reading CMS **central data** over xrootd
(`/store/mc/...`, e.g. the premix pileup library a DIGI step needs) authorizes
on **CMS VO membership**, and a proxy without VOMS attributes is rejected:

```
[FATAL] Auth failed
Failed to open the file 'root://cmsxrootd.fnal.gov//store/mc/.../PREMIX/...root'
----- Begin Fatal Exception ----- ... class=PreMixingModule label='mixData'
```

The trap is that the EOS check passes, so the proxy looks fine right up until
a premix job dies eight minutes in. Distinguish the two cases before
submitting a long chain:

```bash
voms-proxy-info -all | grep -E "^VO|attribute"     # want: VO : cms
# and a direct read of what the job will actually open:
xrdfs root://cmsxrootd.fnal.gov stat /store/mc/<the premix file>
```

`voms-proxy-init` needs a TTY for the passphrase, so it cannot be driven from
a non-interactive agent session — run it yourself:

```bash
voms-proxy-init -rfc -voms cms -valid 192:00
```

Note it writes to `$X509_USER_PROXY` when that is set and `/tmp/x509up_u<uid>`
otherwise, so a proxy made in one shell may not be where another shell looks;
pass `-out <path>` to pin it. Watch the lifetime too — a proxy with hours left
will strand jobs that sit idle overnight.

## Splitting a full production chain

The chain steps have very different costs — from the S1 leptoquark run, per
100 events: GEN-SIM 3896 s, DIGI-HLT 411 s, RECO 574 s, MiniAOD and NanoAOD
negligible. So:

- **Split GEN-SIM by seed**, one job per few hundred events. This is the step
  that actually needs the farm.
- **Chain the cheap steps inside one job** rather than paying `cmsrel` and
  file transfer five times. One wrapper doing GEN-SIM → DIGI → RECO → MiniAOD
  → NanoAOD on its own slice, emitting only the NanoAOD, is usually the right
  granularity.
- **DIGI with Premix needs network** — the pileup library is read over xrootd
  from remote sites, so those jobs need the proxy and are sensitive to a slow
  connection. Give them more memory (8 GB is a reasonable start) and expect
  wall-time scatter.
- **Merge at the end** with `hadd` (or `edmCopyPickMerge` for EDM files).

## Debugging

```bash
condor_q                                        # your jobs, all schedds
condor_q -name lpcschedd6.fnal.gov <cluster>    # a specific one
condor_q -name <schedd> <cluster> -af JobStatus HoldReason
#   JobStatus: 1 idle  2 running  3 removed  4 completed  5 held
condor_q -better-analyze -name <schedd> <cluster>   # why is it not matching?
condor_history <cluster>
condor_ssh_to_job <cluster>.<proc>              # shell into a RUNNING job
```

`condor_ssh_to_job` is the fastest way to understand an environment problem —
it drops you in `/srv` inside the same container.

Held jobs stay held; `condor_release` after fixing, or `condor_rm` and
resubmit. `max_retries` handles transient failures automatically.

## Probe job

To re-verify any of the above, or to check a site this skill was not written
against, submit a script that just reports its environment:

```bash
#!/bin/bash
hostname; cat /etc/redhat-release
pwd; df -h . | tail -1
echo "IMAGE=$SINGULARITY_CONTAINER"; nproc
for p in /cvmfs /uscms_data/d2 /uscms/home/$USER /eos/uscms; do
  [ -d "$p" ] && echo "PRESENT $p" || echo "ABSENT  $p"
done
echo "PROXY=$X509_USER_PROXY"; voms-proxy-info -all 2>&1 | head -8
```

Two minutes and it settles what would otherwise be a long argument with the
documentation.
