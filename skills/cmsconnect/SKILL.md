---
name: cmsconnect
bundle: condor
description: Run CMSSW jobs on a CMS Connect-style opportunistic pool (a login node that forwards to a wide-area HTCondor pool) instead of a site-local batch system like the LPC. Use whenever the user mentions CMS Connect, a login node that is not where the data or release area lives, submitting over ssh, jobs landing on many different sites, a job that sat idle or was held for site/OS reasons, a GEN-SIM to NanoAOD chain that must cross CMSSW releases inside one job, or Premix pileup read over xrootd from a worker. Also use proactively before submitting to such a pool, because several defaults that work at a site-local scheduler silently do not (forced site list, wall-time cap, no shared filesystem, one core, an older worker OS).
---

# CMSSW on an opportunistic pool (CMS Connect-style)

This complements `condor`, which covers a site-local scheduler. The pool here
is different in ways that change what you submit. Everything below was
**observed at one pool during one campaign** — treat the specifics as things to
re-verify with the probe job (see `condor/references/lpc_environment.md`), not
as guarantees.

## What differs from a site-local scheduler

| | Site-local (LPC-style) | Opportunistic pool |
|---|---|---|
| Where jobs run | the host site | whichever allowed site has a free slot |
| Shared filesystem | submit area visible | **none**: only transferred files, `/cvmfs`, xrootd |
| Cores per job | configurable | observed: 1 core slots; do not rely on multithreading |
| Worker OS | matches your request | observed: an older OS (EL7) on many sites |
| Site / wall-time | yours to choose | the submit wrapper may **force** its own site list and wall-time default |
| Where you run commands | the node you are on | a remote login node, over ssh |

## Submitting over ssh

Use `CMSSWJobSubmitTool(remote_host="user@login.example.org", ssh_key=...,
remote_dir=...)`. It rsyncs a flat job directory, creates `logs/`, and runs
`condor_submit` there. Two things that fail without a clear message:

- **The proxy.** Ship one with `proxy_path`, and make the remote command
  export `X509_USER_PROXY` to the *copied* proxy. Without it the wrapper
  around `condor_submit` may refuse with a generic "failed to create proxy".
  The tool warns if `remote_host` is set and `proxy_path` is not.
- **Key auth only.** Every ssh call uses `BatchMode=yes`; a password prompt
  becomes a failure, not a hang.

Check on the cluster with `CondorJobStatusTool(remote_host=...)`. Useful
options: `wait_seconds` (bounded polling — keep it under your agent's per-call
timeout and call again), `tail_running=N` (live stdout via `condor_tail`;
stdout only, other files are not permitted), and finished-job exit codes and
wall times from `condor_history`. `condor_ssh_to_job` was **not installed** at
the pool observed, so do not plan around it; log progress to stdout instead
(the chain wrapper prints one marker line per step for this reason).

## Site list, wall time, OS

The submit wrapper may add its own `+DESIRED_Sites` and `+MaxWallTimeMins`,
overriding what you wrote. Set them explicitly with `desired_sites`,
`max_wall_time_mins` and `required_os`, and read the submitted JDL back
(`condor_q -l <id>`) to see what was actually applied. A job whose estimate is
longer than the wall-time cap is killed late and silently wasted, so size jobs
from a measured events/hour, not a guess.

**Estimate from a small test, then split.** A 10-event test of each step gives
the per-event cost; GEN-SIM usually dominates. Many short jobs beat a few long
ones on an opportunistic pool (evictions cost only the job that was running),
but every job pays the fixed per-step start-up, so do not go below a few tens
of events per job.

## One job, several CMSSW releases

A GEN-SIM to NanoAOD chain usually crosses releases (an old release for
GEN-SIM/DIGI/RECO, a newer one for NanoAOD). Do not make a cluster per release
and shuttle files through the submit node: use `CMSSWJobSubmitTool(steps=[...])`,
where each step names its cfg, release and arch. The wrapper builds each
release in its own subshell, runs the steps in order, deletes each step's
input once the next has consumed it (scratch is finite), and returns only the
outputs you list.

- If the steps need different OS images, either run natively on a worker OS
  that supports them all (observed to work for a CMSSW_10_x chain on EL7), or
  keep `use_apptainer_image` true and accept one image per job. The tool
  refuses a mixed-OS chain unless you say which.
- Give every step a **different** `process.RandomNumberGeneratorService` seed
  per job and a distinct lumi block; the tool patches the cfg per job. Check
  afterwards that no two events share (run, lumi, event) —
  `NanoAODToJSONLTool` warns when they do.

## Premix pileup from a worker

Premix mixing reads pileup files over xrootd, which needs the grid proxy on
the worker, and:

- **Choose files that are really on disk at the site you read from.** A file
  that is not resident there does not fail fast; it hangs in the redirector
  for minutes ("Operation expired"). Use `PremixFileListTool(dataset, site)`;
  it asks DAS for the dataset's *blocks* at the site and then each block's
  files. The shorter `file dataset=... site=...` query returned nothing for
  datasets that were present, which is easy to misread as "not at that site".
- **Give each job different pileup files.** `premix_list` plus
  `premix_files_per_job` hands job N a distinct slice (stride by process id),
  so jobs do not all read the same few files.
- Some sites' data servers want `XrdSecPROTOCOL=gsi` and
  `XrdSecGSISRVNAMES='*'` in the environment; the chain wrapper sets both.
- `cmsDriver --pileup_input dbs:...` resolves through `dasgoclient` when the
  cfg is *made*. If that is not on `PATH` the list can come out empty with no
  error; check `process.mixData.input.fileNames` in the cfg.

## Site configuration on workers

Workers read the site's CMS configuration from `/cvmfs`. If a site's
configuration is missing or wrong, a job fails to open any file. A private
`SITECONFIG_PATH` fallback is a reasonable defence, but was *not exercised* in
the campaign this skill comes from (every site used had working config), so
test it before relying on it.

## Finishing: from per-job files to a number

1. `CondorJobStatusTool(wait_seconds=...)` until `done` is true and
   `all_succeeded` is true. Exit 0 with an empty output is possible; check
   sizes.
2. Fetch the per-job outputs (flat `NANOAODSIM`, not `NANOEDMAODSIM`).
3. `NanoAODToJSONLTool(input_files=[...])` — one call over all jobs.
4. `NormalizedHistogramTool(input_files=[...], n_generated=<total generated>)`
   — normalise **once** over the merged set. Normalising each job and summing
   mis-weights if jobs generated different numbers of events.
   Keep test-run outputs out of the merge.

## Pitfalls (each exits 0 and looks plausible)

- Identical seeds across jobs: duplicated events, inflated significance.
- Test outputs left in the directory you merge: extra events, wrong N.
- A site list or wall-time forced by the wrapper that is not the one you wrote.
- Pileup list not resident at the chosen site: stalls, not errors.
- Normalising with the number of *selected* events instead of *generated*.

## Related

- `condor` — the site-local counterpart and the measured worker environment.
- `central-production` — resolving the chain you are reproducing.
- `cmssw` — the gridpack and GEN fragment that feed the first step.
