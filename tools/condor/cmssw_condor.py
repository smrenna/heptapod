"""
# cmssw_condor.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Prepare (and optionally submit) a CMSSW `cmsRun` job as an HTCondor cluster.

Batch is the default execution mode for CMSSW work: `use_condor` defaults to
True, and setting it False emits an equivalent local run script instead. See
skills/condor/SKILL.md for the procedure and the measured facts about the
worker-node environment this encodes.

The static checks here exist because the corresponding failures are silent or
badly misreported at run time:

  * a submit directory the *schedd* cannot read fails as a missing-file error;
  * an input path under /uscms_data, $HOME or POSIX EOS does not exist on the
    worker at all, because the job runs in an apptainer container started with
    --contain;
  * an unseeded multi-job cluster produces N identical copies of the same
    events and never errors.
"""

import errno
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "cmssw-condor-1.0"

# SCRAM_ARCH prefix -> (apptainer image, human-readable OS). The image is what
# puts the matching libc/libssl in front of the release; getting it wrong is
# the batch-side form of the EL8-on-EL9 failure documented in
# skills/central-production/SKILL.md.
_ARCH_IMAGE_MAP: Dict[str, Tuple[str, str]] = {
    "slc7": ("/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel7", "EL7"),
    "el8": ("/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel8", "EL8"),
    "el9": ("/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel9", "EL9"),
}

# Filesystems local to the login node. The schedd runs on a different host, so
# a submit directory here cannot be read at submission time.
_NONSHARED_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/", "/scratch/")

# Visible from the login node, absent inside the job's container.
_WORKER_INVISIBLE_PREFIXES = (
    "/uscms_data/", "/uscms/home/", "/uscms/homes/", "/eos/uscms/", "/afs/",
)

# Absolute paths appearing in a cfg or fragment. Deliberately loose: it only
# feeds a reachability warning, so a false positive costs a sentence.
_ABS_PATH_RE = re.compile(r"['\"](/[^'\"\s]+)['\"]")

_DEFAULT_SEED = 1000


# ====================================================================== #
# ========================= Helper functions =========================== #
# ====================================================================== #

def _image_for_arch(scram_arch: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Map a SCRAM_ARCH to its apptainer image, e.g. el8_amd64_gcc10 -> cms:rhel8.

    Returns (image, os_tag), or (None, None) for an unrecognized arch -- the
    caller reports that rather than guessing, since a wrong image produces
    missing-library errors that do not name the image.
    """
    if not scram_arch:
        return None, None
    prefix = scram_arch.split("_", 1)[0]
    image, os_tag = _ARCH_IMAGE_MAP.get(prefix, (None, None))
    return image, os_tag


def _check_submit_dir(job_dir: str) -> Optional[str]:
    """
    Reject a submit directory the schedd cannot read.

    HTCondor submission at a typical CMS site is load-balanced across schedd
    hosts distinct from the interactive node, so `transfer_input_files` is
    read by the schedd, not by your shell. A path under /tmp then fails with
    "(errno 2) No such file or directory", which reads as a missing file.
    """
    resolved = os.path.abspath(job_dir)
    for prefix in _NONSHARED_PREFIXES:
        if resolved == prefix.rstrip("/") or resolved.startswith(prefix):
            return (
                f"Submit directory {resolved!r} is on a filesystem local to this "
                "node. The schedd runs on a different host and reads "
                "transfer_input_files itself, so submission fails there with a "
                "misleading '(errno 2) No such file or directory'. Use a shared "
                "filesystem (e.g. your /uscms_data or home area)."
            )
    return None


def _scan_unreachable_inputs(
    cfg_text: str,
    transferred: List[str],
) -> List[str]:
    """
    Find absolute paths in a cfg that the worker node will not be able to open.

    The job runs under `apptainer --contain` with only /cvmfs, /etc/hosts and
    /etc/grid-security bound, so /uscms_data, $HOME and POSIX /eos/uscms are
    absent. Such a path is fine only if it is also transferred in, in which
    case the cfg should refer to its basename.
    """
    transferred_basenames = {os.path.basename(p) for p in transferred}
    findings: List[str] = []
    for match in sorted(set(_ABS_PATH_RE.findall(cfg_text))):
        if not match.startswith(_WORKER_INVISIBLE_PREFIXES):
            continue
        if os.path.basename(match) in transferred_basenames:
            findings.append(
                f"{match} is transferred in, so the job patch will rewrite it "
                "to its absolute location in the scratch dir at run time -- no "
                "action needed. (Do NOT 'fix' it to a bare filename: "
                "ExternalLHEProducer's helper cds into lheevent/ before "
                "untarring, so a relative path breaks.)"
            )
        else:
            findings.append(
                f"{match} does not exist on the worker node (the job runs in a "
                "--contain'd container; /uscms_data, $HOME and POSIX EOS are "
                "not bound). Add it to transfer_input_files, stage it on "
                "/cvmfs, or read it over xrootd."
            )
    return findings


def _build_seed_patch() -> str:
    """
    Python appended to the cfg inside the job. Two fixups, both silent failures
    if omitted.

    **Seeding.** Seeds every module registered with RandomNumberGeneratorService
    rather than naming `generator`/`externalLHEProducer` explicitly, so it stays
    correct across cfgs with different module sets. Without this, `queue N`
    yields N identical copies of the same events with no error anywhere.

    Distinct physics is not enough: the outputs also need distinct **event
    IDs**. An `EmptySource` cfg numbers events (run 1, lumi 1, event 1..N)
    regardless of RNG seeds, so N jobs produce N files whose events collide.
    Nothing complains until the files are chained into a later step, where
    PoolSource's duplicate check drops everything but the first file --
    reporting it as an information-level "some events were skipped because of
    duplicate checking" and a correct-looking job. `firstLuminosityBlock` is
    therefore set per job, and unconditionally, since cmsDriver does not write
    the parameter for a hasattr guard to find.

    **Gridpack path.** `ExternalLHEProducer` args must be ABSOLUTE at job run
    time. Its helper `run_generic_tarball_cvmfs.sh` does

        mkdir lheevent; cd lheevent
        tar -xaf ${path}

    so a relative path resolves inside `lheevent/` and the untar fails; while an
    absolute path from the submitting node points at a filesystem the worker
    cannot see at all. Either way the job dies minutes in, during Geant4 init,
    with a `FileOpenError` on `cmsgrid_final.lhe` that names neither cause.
    Rewriting each arg whose basename was transferred into the scratch dir to
    that file's absolute location covers both. Args naming something not
    transferred (a genuine /cvmfs gridpack) are left alone.
    """
    return '''
# --- per-job fixups injected by heptapod CMSSWJobSubmitTool ---
import FWCore.ParameterSet.Config as cms
_seed = int(os.environ["HEPTAPOD_SEED"])
if hasattr(process, "RandomNumberGeneratorService"):
    _rng = process.RandomNumberGeneratorService
    for _name in _rng.parameterNames_():
        _mod = getattr(_rng, _name)
        if hasattr(_mod, "initialSeed"):
            _mod.initialSeed = cms.untracked.uint32(_seed)
if hasattr(process, "source") and process.source.type_() == "EmptySource":
    # Set unconditionally, NOT under hasattr: cmsDriver does not write this
    # parameter, so a hasattr guard skips it and every job emits event IDs
    # (run 1, lumi 1, event 1..N). PoolSource then drops all but the first
    # file's events as duplicates when the outputs are chained downstream.
    process.source.firstLuminosityBlock = cms.untracked.uint32(_seed)
    print("[heptapod] source.firstLuminosityBlock =", _seed)
if os.environ.get("HEPTAPOD_PREMIX_LIST") and hasattr(process, "mixData"):
    # Premix pileup: give each job its own few files out of a pre-made list
    # (one per line), so N jobs do not all hammer the same files.
    _pl = [_l.strip() for _l in open(os.environ["HEPTAPOD_PREMIX_LIST"]) if _l.strip()]
    _n = max(1, min(int(os.environ.get("HEPTAPOD_PREMIX_N", "4")), len(_pl)))
    _proc = int(os.environ.get("HEPTAPOD_PROC", "0"))
    _stride = max(1, len(_pl) // _n)
    _pre = os.environ.get("HEPTAPOD_PREMIX_PREFIX", "")
    _pick = [_pl[(_proc * _n + _k * _stride) % len(_pl)] for _k in range(_n)]
    _pick = [_f if "://" in _f else _pre + _f for _f in _pick]
    process.mixData.input.fileNames = cms.untracked.vstring(*_pick)
    print("[heptapod] mixData.input.fileNames ->", _pick)
if hasattr(process, "externalLHEProducer"):
    _args = []
    for _a in process.externalLHEProducer.args:
        _local = os.path.basename(_a)
        _args.append(os.path.abspath(_local) if os.path.isfile(_local) else _a)
    process.externalLHEProducer.args = cms.vstring(*_args)
    print("[heptapod] externalLHEProducer.args ->", _args)
'''


def _build_wrapper(
    cfg_files: List[str],
    cmssw_release: str,
    scram_arch: str,
    output_files: List[str],
    first_seed: int,
    output_eos_dir: Optional[str] = None,
    n_threads: Optional[int] = None,
    local: bool = False,
    suffix_outputs: bool = True,
    premix_list: Optional[str] = None,
    premix_n: int = 4,
    premix_prefix: str = "",
) -> str:
    """
    Emit the bash the job (or the local run) executes.

    `cfg_files` run in order in a single job, which is what you want for the
    cheap tail of a production chain: intermediate files stay on the worker's
    scratch disk instead of making a round trip through the schedd, and the
    release area is set up once rather than once per step. Splitting steps
    across clusters is only worth it when they need different releases.
    """
    proc_expr = '"$1"' if not local else "0"
    threads_arg = f" -n {n_threads}" if n_threads else ""

    lines = [
        "#!/bin/bash",
        "# Generated by heptapod CMSSWJobSubmitTool -- see skills/condor/SKILL.md",
        "set -euo pipefail",
        "",
        f"PROC={proc_expr}",
        f"SEED=$(( {first_seed} + PROC ))",
        'echo "[heptapod] proc=$PROC seed=$SEED host=$(hostname)"',
        "",
        "source /cvmfs/cms.cern.ch/cmsset_default.sh",
        f"export SCRAM_ARCH={scram_arch}",
    ]

    if local:
        lines += [
            "# Local mode: reuse the release area in the current directory if",
            "# one is already set up, so repeated runs do not rebuild it.",
            f"if [ ! -d {cmssw_release} ]; then cmsrel {cmssw_release}; fi",
        ]
    else:
        lines += [
            "# Condor scratch starts empty; the release area is built per job.",
            f"cmsrel {cmssw_release}",
        ]

    lines += [
        f"cd {cmssw_release}/src",
        "eval `scramv1 runtime -sh`",
        "cd -",
        "",
        "export HEPTAPOD_SEED=$SEED HEPTAPOD_PROC=$PROC",
    ]
    if premix_list:
        lines += [
            f'export HEPTAPOD_PREMIX_LIST="$PWD/{os.path.basename(premix_list)}"',
            f"export HEPTAPOD_PREMIX_N={int(premix_n)}",
        ]
        if premix_prefix:
            lines.append(
                f"export HEPTAPOD_PREMIX_PREFIX={shlex.quote(premix_prefix)}")

    # Each cfg gets its own seeded copy and runs in order. A step that fails
    # aborts the job (set -e), so a later step never runs on a truncated input.
    for idx, cfg in enumerate(cfg_files):
        cfg_base = os.path.basename(cfg)
        job_cfg = f"job_cfg_{idx}.py"
        lines += [
            "",
            f"# --- step {idx + 1}/{len(cfg_files)}: {cfg_base} ---",
            f'echo "[heptapod] step {idx + 1}/{len(cfg_files)}: {cfg_base}"',
            f"cp {cfg_base} {job_cfg}",
            f"cat >> {job_cfg} <<'HEPTAPOD_SEED_PATCH'",
            "import os",
            _build_seed_patch().strip(),
            "HEPTAPOD_SEED_PATCH",
            f"cmsRun{threads_arg} {job_cfg}",
        ]

    lines.append("")

    # A single-job cluster keeps cmsRun's own filenames: the _<Process> suffix
    # exists only to keep N jobs' outputs apart, and renaming here would break
    # the next stage, whose cfg names the file cmsDriver was told to write.
    if suffix_outputs:
        for out in output_files:
            stem, ext = os.path.splitext(out)
            lines.append(f'mv "{out}" "{stem}_${{PROC}}{ext}"')

    if output_eos_dir:
        eos_dir = output_eos_dir.rstrip("/")
        lines.append("")
        lines.append("# Stage out over xrootd; POSIX /eos/uscms is not mounted here.")
        for out in output_files:
            stem, ext = os.path.splitext(out)
            name = f"{stem}_${{PROC}}{ext}" if suffix_outputs else out
            lines.append(f'xrdcp -f "{name}" "{eos_dir}/{name}"')

    lines.append("")
    return "\n".join(lines)


def _build_chain_wrapper(
    steps: List[Dict[str, str]],
    first_seed: int,
    n_threads: Optional[int] = None,
    premix_list: Optional[str] = None,
    premix_n: int = 4,
    premix_prefix: str = "",
    keep_outputs: Optional[List[str]] = None,
    suffix_outputs: bool = True,
    local: bool = False,
) -> str:
    """
    Emit the bash for ONE job that runs several steps, each in its own CMSSW
    release (e.g. a UL production chain crossing five releases).

    Why one job rather than one cluster per release: intermediates (GEN-SIM
    and DIGI files are GB-scale) stay on the worker's scratch instead of making
    a round trip through the schedd, and the job's seed / lumi block / premix
    selection is decided once.

    Every step runs in a SUBSHELL. `scramv1 runtime -sh` mutates PATH,
    LD_LIBRARY_PATH and PYTHONPATH, and a later release evaluated on top of an
    earlier one's environment picks up the wrong libraries without any
    message. The subshell makes each step start from the job's clean
    environment. Each step's input is deleted once the next step has consumed
    it, to keep scratch use bounded.

    `steps` items: {"cfg", "cmssw_release", "scram_arch", "input", "output"}.
    `input`/`output` are the intermediate filenames (optional); when given,
    `input` is removed after the step succeeds. The last step's output is kept.
    """
    proc_expr = '"$1"' if not local else "0"
    threads_arg = f" -n {n_threads}" if n_threads else ""
    lines = [
        "#!/bin/bash",
        "# Generated by heptapod CMSSWJobSubmitTool (multi-release chain)",
        "set -uo pipefail",
        "",
        f"PROC={proc_expr}",
        f"SEED=$(( {first_seed} + PROC ))",
        'echo "[heptapod] chain job proc=$PROC seed=$SEED host=$(hostname)"',
        "WORK=$PWD",
        "export HEPTAPOD_SEED=$SEED HEPTAPOD_PROC=$PROC",
    ]
    if premix_list:
        lines += [
            f'export HEPTAPOD_PREMIX_LIST="$WORK/{os.path.basename(premix_list)}"',
            f"export HEPTAPOD_PREMIX_N={int(premix_n)}",
        ]
        if premix_prefix:
            lines.append(f"export HEPTAPOD_PREMIX_PREFIX={shlex.quote(premix_prefix)}")
    lines += [
        "",
        "# Pileup and stage-out read over xrootd; some sites' data servers need",
        "# the GSI server-name check relaxed (otherwise 'Auth failed').",
        'if [ -z "${X509_USER_PROXY:-}" ] && [ -f "$WORK/x509up" ]; then '
        'export X509_USER_PROXY="$WORK/x509up"; fi',
        "export X509_CERT_DIR=${X509_CERT_DIR:-/cvmfs/grid.cern.ch/etc/grid-security/certificates}",
        "export X509_VOMS_DIR=${X509_VOMS_DIR:-/cvmfs/grid.cern.ch/etc/grid-security/vomsdir}",
        "export XrdSecPROTOCOL=gsi XrdSecGSISRVNAMES='*' XRD_REQUESTTIMEOUT=120",
        "source /cvmfs/cms.cern.ch/cmsset_default.sh",
        "",
        "run_step() {   # $1=release $2=arch $3=cfg $4=log",
        "  ( export SCRAM_ARCH=$2",
        "    [ -d \"$WORK/$1\" ] || scramv1 project CMSSW $1 >/dev/null || exit 101",
        "    cd \"$WORK/$1/src\" && eval `scramv1 runtime -sh` && cd \"$WORK\" || exit 102",
        f"    cmsRun{threads_arg} $3 > $4 2>&1 )",
        "  rc=$?",
        '  echo "[heptapod]   rc=$rc $(date +%T)"',
        '  if [ $rc -ne 0 ]; then echo "---- tail of $4"; tail -40 "$4"; exit $rc; fi',
        "}",
    ]
    for idx, st in enumerate(steps):
        cfg_base = os.path.basename(st["cfg"])
        job_cfg = f"job_cfg_{idx}.py"
        lines += [
            "",
            f"# --- step {idx + 1}/{len(steps)}: {cfg_base} in {st['cmssw_release']} ---",
            f'echo "[heptapod] step {idx + 1}/{len(steps)}: {cfg_base} '
            f'({st["cmssw_release"]}) $(date +%T)"',
            f"cp {cfg_base} {job_cfg}",
            f"cat >> {job_cfg} <<'HEPTAPOD_SEED_PATCH'",
            "import os",
            _build_seed_patch().strip(),
            "HEPTAPOD_SEED_PATCH",
            f"run_step {st['cmssw_release']} {st['scram_arch']} {job_cfg} "
            f"step{idx + 1}.log",
        ]
        if st.get("input"):
            lines.append(f"rm -f {shlex.quote(st['input'])}")
    lines.append("")
    if suffix_outputs:
        for out in keep_outputs or []:
            stem, ext = os.path.splitext(out)
            lines.append(f'mv "{out}" "{stem}_${{PROC}}{ext}"')
    lines += ['echo "[heptapod] chain done $(date)"', ""]
    return "\n".join(lines)


def _build_jdl(
    executable: str,
    transfer_input_files: List[str],
    output_files: List[str],
    image: str,
    n_jobs: int,
    request_cpus: int,
    request_memory_mb: int,
    request_disk_kb: int,
    proxy_path: Optional[str],
    stage_out_to_eos: bool,
    max_retries: int = 3,
    suffix_outputs: bool = True,
    extra_classads: Optional[Dict[str, str]] = None,
    flat_inputs: bool = False,
    use_apptainer_image: bool = True,
) -> str:
    """
    Emit the submit description.

    `extra_classads` become `+Name = value` lines (e.g. REQUIRED_OS,
    DESIRED_Sites, MaxWallTimeMins: sites such as CMS Connect require some of
    these). `flat_inputs` writes transfer_input_files as bare basenames, for
    remote submission where the files are rsynced flat into the submit dir.
    `use_apptainer_image=False` omits +ApptainerImage, for sites whose workers
    run the release natively (CMS Connect EL7 workers) and that select the OS
    through REQUIRED_OS instead.

    `transfer_output_files` is always written explicitly. Left unset, HTCondor
    returns every new file in the job's scratch directory -- which, because the
    wrapper runs `cmsrel` there, means copying a multi-GB release area back to
    the submit directory.
    """
    per_job_outputs = (
        "" if stage_out_to_eos
        else ", ".join(
            (f"{os.path.splitext(o)[0]}_$(Process){os.path.splitext(o)[1]}"
             if suffix_outputs else o)
            for o in output_files
        )
    )

    lines = [
        "# Generated by heptapod CMSSWJobSubmitTool -- see skills/condor/SKILL.md",
        "universe                = vanilla",
        f"executable              = {executable}",
        "arguments               = $(Process)",
        "",
        "should_transfer_files   = YES",
        "when_to_transfer_output = ON_EXIT",
    ]
    if transfer_input_files:
        _ins = ([os.path.basename(f) for f in transfer_input_files]
                if flat_inputs else transfer_input_files)
        lines.append("transfer_input_files    = " + ", ".join(_ins))
    # Explicit even when empty: the default would return the whole scratch dir.
    lines.append(f'transfer_output_files   = {per_job_outputs}')
    lines += [
        "",
        "output                  = logs/job.$(Cluster).$(Process).out",
        "error                   = logs/job.$(Cluster).$(Process).err",
        "log                     = logs/job.$(Cluster).log",
        "",
        f"request_cpus            = {request_cpus}",
        f"request_memory          = {request_memory_mb}",
        f"request_disk            = {request_disk_kb}",
        "",
    ]
    if use_apptainer_image:
        lines.append(f'+ApptainerImage         = "{image}"')
    for _k, _v in (extra_classads or {}).items():
        lines.append(f"+{_k:<22}= {_v}")
    if proxy_path:
        lines.append(f"x509userproxy           = {proxy_path}")
    lines += [
        "",
        f"max_retries             = {max_retries}",
        f"queue {n_jobs}",
        "",
    ]
    return "\n".join(lines)


# ====================================================================== #
# =============================== Tools ================================ #
# ====================================================================== #

class CMSSWJobSubmitTool(BaseTool):
    """
    Prepare a `cmsRun` job for HTCondor -- the default execution mode for
    CMSSW work in this toolkit -- writing a submit description and a job
    wrapper, and optionally submitting them.

    Set use_condor=False to get an equivalent local run script instead. That
    is the documented escape hatch for a quick few-event check; anything past
    a few hundred events belongs in batch, where the per-job wall time and the
    node's memory are not shared with an interactive session.

    What the generated files encode, all of which are silent failures when
    done by hand (skills/condor/SKILL.md):

      * per-job random seeds, so `queue N` produces N *different* samples;
      * an explicit transfer_output_files, so the release area built inside
        the job is not copied back;
      * the apptainer image matching SCRAM_ARCH, so an EL8 release does not
        land on an EL9 worker;
      * a reachability check on absolute paths in the cfg, since the worker
        sees neither /uscms_data nor $HOME nor POSIX EOS.

    Nothing is submitted unless submit=True. The default writes the files and
    returns the exact command to run, so the submission stays an explicit act.

    Input:
        cfg_file: path to the cmsRun python config (as produced by
            `cmsDriver.py --no_exec --python_filename ...`).
        cmssw_release / scram_arch: the release to set up in the job, and the
            arch that selects the container image.
        n_jobs: size of the cluster; each job gets seed first_seed + Process.
        output_files: files cmsRun produces that you want back, suffixed with
            _<Process> per job.

    Output: JSON with the written paths, the resolved image, the submit
    command, and any warnings.
    """

    # ======================== Runtime fields ======================== #
    cfg_file: Optional[str] = RuntimeField(
        default=None,
        description="Path to the cmsRun python cfg to run (e.g. step1_cfg.py). "
        "Single-cfg shorthand for cfg_files.",
    )
    cfg_files: Optional[List[str]] = RuntimeField(
        default=None,
        description="Several cfgs to run IN ORDER inside one job, e.g. "
        "['digi_cfg.py', 'reco_cfg.py']. Preferred over separate clusters for "
        "consecutive steps sharing a release: intermediates stay on the "
        "worker's scratch instead of round-tripping through the schedd. Steps "
        "needing different CMSSW releases must still be separate clusters.",
    )
    cmssw_release: Optional[str] = RuntimeField(
        default=None,
        description="CMSSW release to set up inside the job, e.g. CMSSW_12_4_8",
    )
    scram_arch: Optional[str] = RuntimeField(
        default=None,
        description="SCRAM_ARCH of that release, e.g. el8_amd64_gcc10. Selects "
        "the apptainer image (el8_* -> cms:rhel8).",
    )
    n_jobs: int = RuntimeField(
        default=1,
        description="Number of jobs in the cluster. Each gets a distinct seed.",
    )
    first_seed: int = RuntimeField(
        default=_DEFAULT_SEED,
        description="Seed for Process 0; job N uses first_seed + N.",
    )
    output_files: Optional[List[str]] = RuntimeField(
        default=None,
        description="Output filenames cmsRun writes, e.g. ['step1.root']. Each "
        "is renamed to <stem>_<Process><ext> before transfer.",
    )
    transfer_input_files: Optional[List[str]] = RuntimeField(
        default=None,
        description="Extra files the job needs (gridpack tarball, fragment, "
        "...). The cfg itself is added automatically.",
    )
    output_eos_dir: Optional[str] = RuntimeField(
        default=None,
        description="Optional xrootd destination, e.g. "
        "root://cmseos.fnal.gov//store/user/<you>/<dir>. If set, outputs are "
        "staged out with xrdcp instead of transferred back.",
    )
    request_cpus: int = RuntimeField(
        default=1, description="Cores per job; must cover cmsRun's -n threads.")
    request_memory_mb: int = RuntimeField(
        default=4000, description="Memory per job in MB (GEN-SIM ~4000, "
        "DIGI/RECO with premix wants ~8000).")
    request_disk_kb: int = RuntimeField(
        default=10_000_000, description="Scratch per job in KB.")
    n_threads: Optional[int] = RuntimeField(
        default=None,
        description="Pass -n to cmsRun. Must be <= request_cpus.",
    )
    proxy_path: Optional[str] = RuntimeField(
        default=None,
        description="Grid proxy to ship with the job. Defaults to "
        "$X509_USER_PROXY when set. Needed for xrootd reads (premix pileup) "
        "and EOS stage-out.",
    )
    job_dir: Optional[str] = RuntimeField(
        default=None,
        description="Directory to write the submit files into, relative to "
        "base_directory unless absolute. Must be on shared storage, not /tmp.",
    )
    use_condor: bool = RuntimeField(
        default=True,
        description="Run on HTCondor (default). False emits an equivalent "
        "local run script instead -- use only for short interactive checks.",
    )
    allow_nonshared_submit_dir: bool = RuntimeField(
        default=False,
        description="Override the shared-filesystem check on job_dir. That "
        "check is a path heuristic (rejects /tmp, /var/tmp, ...) and is right "
        "wherever the schedd is a separate host; set True at a site where the "
        "schedd shares your node's local disk.",
    )
    submit: bool = RuntimeField(
        default=False,
        description="Actually run condor_submit. Default False writes the "
        "files and returns the command.",
    )
    steps: Optional[List[Dict[str, str]]] = RuntimeField(
        default=None,
        description="Multi-release chain run INSIDE ONE JOB, instead of "
        "cfg_files. List of {cfg, cmssw_release, scram_arch, input?, output?} "
        "in order, each step in its own release and its own subshell "
        "(a clean environment per release). `input` is deleted once its step "
        "succeeds, bounding scratch use. cmssw_release/scram_arch/cfg_file(s) "
        "are not needed in this mode; output_files names the files to return "
        "(normally the last step's). All steps must share one OS "
        "(e.g. all slc7) unless use_apptainer_image=False.",
    )
    premix_list: Optional[str] = RuntimeField(
        default=None,
        description="File with one premix pileup file per line. Each job picks "
        "premix_files_per_job of them (by job index) and writes them into "
        "process.mixData.input.fileNames of any cfg that has a mixData, and "
        "ships the list with the job. Use this when cmsDriver could not "
        "resolve a dbs: pileup dataset. Lines without '://' get premix_prefix.",
    )
    premix_files_per_job: int = RuntimeField(
        default=4, description="Pileup files per job (see premix_list).")
    premix_prefix: Optional[str] = RuntimeField(
        default=None,
        description="Prefix for premix_list lines that are bare /store paths, "
        "e.g. root://cmsxrootd.fnal.gov/",
    )
    required_os: Optional[str] = RuntimeField(
        default=None,
        description="Value for +REQUIRED_OS (e.g. rhel7) at pools that match "
        "the worker OS that way (CMS Connect / OSPool).",
    )
    desired_sites: Optional[List[str]] = RuntimeField(
        default=None,
        description="Value for +DESIRED_Sites, e.g. ['T1_US_FNAL']. Some "
        "pools inject a long default list if this is omitted.",
    )
    max_wall_time_mins: Optional[int] = RuntimeField(
        default=None,
        description="Value for +MaxWallTimeMins; pools that require it "
        "apply a default if omitted.",
    )
    use_apptainer_image: bool = RuntimeField(
        default=True,
        description="Write +ApptainerImage for the SCRAM_ARCH. Set False on "
        "pools whose workers run the release natively and select the OS with "
        "required_os instead.",
    )
    max_retries: int = RuntimeField(
        default=3, description="max_retries for the cluster.")
    remote_host: Optional[str] = RuntimeField(
        default=None,
        description="user@host of a remote submit node (e.g. a CMS Connect "
        "login node). Files are written locally, rsynced FLAT to remote_dir, "
        "and condor_submit is run over ssh with BatchMode (key auth only; "
        "no password prompt). proxy_path then names a proxy file ON THE "
        "REMOTE HOST: this tool does not copy credentials for you.",
    )
    remote_dir: Optional[str] = RuntimeField(
        default=None,
        description="Submit directory on remote_host (must be on storage the "
        "remote schedd can read, normally the remote home). Default "
        "~/heptapod_jobs.",
    )
    ssh_key: Optional[str] = RuntimeField(
        default=None, description="Private key for remote_host (optional).")
    # ================================================================ #

    # ========================= State fields ========================= #
    base_directory: str = StateField(
        description="Base sandbox directory for file operations"
    )
    # ================================================================ #

    def _setup(self):
        self.base_directory = os.path.abspath(self.base_directory)

    def _run(self) -> str:
        chain = list(self.steps or [])
        raw_cfgs = ([st.get("cfg", "") for st in chain] if chain else
                    list(self.cfg_files or ([self.cfg_file] if self.cfg_file else [])))
        if chain:
            bad = [i for i, st in enumerate(chain)
                   if not (st.get("cfg") and st.get("cmssw_release")
                           and st.get("scram_arch"))]
            if bad:
                return self.format_error(
                    error="Incomplete steps",
                    reason=f"steps {bad} lack cfg, cmssw_release or scram_arch.",
                    suggestion="Each step needs {cfg, cmssw_release, scram_arch}.",
                )
            if self.cfg_file or self.cfg_files:
                return self.format_error(
                    error="Conflicting inputs",
                    reason="steps cannot be combined with cfg_file/cfg_files.",
                )
            release_for_image, arch_for_image = (
                chain[0]["cmssw_release"], chain[0]["scram_arch"])
            missing = []
        else:
            release_for_image, arch_for_image = (
                self.cmssw_release, self.scram_arch)
            missing = [n for n in ("cmssw_release", "scram_arch")
                       if not getattr(self, n)]
            if not raw_cfgs:
                missing.insert(0, "cfg_file or cfg_files (or steps)")
        if missing:
            return self.format_error(
                error="Missing required input",
                reason=f"{', '.join(missing)} must be provided.",
                suggestion="cmsDriver.py --no_exec --python_filename <cfg> "
                           "writes the cfg; scram_arch is the one the release "
                           "was built for.",
            )

        cfg_paths: List[str] = []
        for cfg in raw_cfgs:
            path = cfg if os.path.isabs(cfg) else os.path.join(
                self.base_directory, cfg)
            if not os.path.isfile(path):
                return self.format_error(
                    error="cfg not found",
                    reason=f"{path!r} does not exist.",
                    suggestion="Pass paths to existing cmsRun cfgs.",
                )
            cfg_paths.append(path)

        # Distinct basenames: they are transferred into one flat scratch dir,
        # so two cfgs with the same name would silently clobber each other.
        basenames = [os.path.basename(p) for p in cfg_paths]
        if len(set(basenames)) != len(basenames):
            dupes = sorted({b for b in basenames if basenames.count(b) > 1})
            return self.format_error(
                error="Duplicate cfg filenames",
                reason=f"{', '.join(dupes)} appears more than once.",
                suggestion="Condor flattens transferred files into one scratch "
                           "directory, so cfg basenames must be unique even if "
                           "their source directories differ.",
            )

        image, os_tag = _image_for_arch(arch_for_image)
        if chain:
            tags = {_image_for_arch(st["scram_arch"])[1] for st in chain}
            if None in tags:
                return self.format_error(
                    error="Unrecognized SCRAM_ARCH",
                    reason="A step has a SCRAM_ARCH with no known image.",
                    context=f"Known prefixes: {', '.join(sorted(_ARCH_IMAGE_MAP))}.",
                )
            if len(tags) > 1 and self.use_apptainer_image:
                return self.format_error(
                    error="Steps span several operating systems",
                    reason=f"Steps need {sorted(tags)}, but one job runs in "
                           "one container image.",
                    suggestion="Split the chain into one cluster per OS "
                               "(central_production's release groups already "
                               "do), or set use_apptainer_image=False if the "
                               "pool launches each step natively.",
                )
        if image is None:
            return self.format_error(
                error="Unrecognized SCRAM_ARCH",
                reason=f"No container image known for {arch_for_image!r}.",
                context=f"Known prefixes: {', '.join(sorted(_ARCH_IMAGE_MAP))}.",
                suggestion="A wrong image fails with missing-library errors "
                           "that never name the image, so this is not guessed.",
            )

        if self.n_threads and self.n_threads > self.request_cpus:
            return self.format_error(
                error="Thread/slot mismatch",
                reason=f"n_threads={self.n_threads} exceeds "
                       f"request_cpus={self.request_cpus}.",
                suggestion="Raise request_cpus to match, or the job "
                           "oversubscribes its slot and runs slower than "
                           "single-threaded.",
            )

        job_dir = self.job_dir or "condor_jobs"
        if not os.path.isabs(job_dir):
            job_dir = os.path.join(self.base_directory, job_dir)
        job_dir = os.path.abspath(job_dir)

        warnings: List[str] = []
        if self.use_condor and not self.allow_nonshared_submit_dir:
            dir_error = _check_submit_dir(job_dir)
            if dir_error:
                return self.format_error(
                    error="Submit directory not on shared storage",
                    reason=dir_error,
                    suggestion="If your site's schedd really does share this "
                               "node's local disk, set "
                               "allow_nonshared_submit_dir=True.",
                )

        os.makedirs(os.path.join(job_dir, "logs"), exist_ok=True)

        output_files = list(self.output_files or ["step1.root"])
        transfer_in = list(self.transfer_input_files or [])

        # Each cfg is scanned; a later step's --filein usually names an
        # intermediate produced by an earlier step in the same job, which is
        # a relative name and so never trips the absolute-path check.
        for cfg_path in cfg_paths:
            try:
                with open(cfg_path) as fh:
                    cfg_text = fh.read()
            except OSError as e:  # unreadable cfg is a warning, not a hard stop
                warnings.append(
                    f"Could not read {os.path.basename(cfg_path)} for path "
                    f"checking: {e}")
                continue
            for finding in _scan_unreachable_inputs(cfg_text, transfer_in):
                warnings.append(f"{os.path.basename(cfg_path)}: {finding}")

        if self.n_jobs > 1 and self.first_seed <= 0:
            warnings.append(
                "first_seed <= 0: Pythia8 treats seed 0 as 'pick from the "
                "clock', which makes the run irreproducible."
            )

        proxy = self.proxy_path or os.environ.get("X509_USER_PROXY")
        if not proxy:
            warnings.append(
                "No grid proxy found (proxy_path unset and X509_USER_PROXY "
                "not in the environment). Jobs reading premix pileup over "
                "xrootd, or staging out to EOS, will fail without one; run "
                "voms-proxy-init first."
            )

        premix_list_path = None
        if self.premix_list:
            premix_list_path = (self.premix_list if os.path.isabs(self.premix_list)
                                else os.path.join(self.base_directory, self.premix_list))
            if not os.path.isfile(premix_list_path):
                return self.format_error(
                    error="premix_list not found",
                    reason=f"{premix_list_path!r} does not exist.",
                )
            if premix_list_path not in transfer_in:
                transfer_in.append(premix_list_path)

        wrapper_name = "run_cmssw.sh" if self.use_condor else "run_cmssw_local.sh"
        wrapper_path = os.path.join(job_dir, wrapper_name)
        if chain:
            wrapper_text = _build_chain_wrapper(
                steps=[dict(st, cfg=cfg_paths[i]) for i, st in enumerate(chain)],
                first_seed=self.first_seed,
                n_threads=self.n_threads,
                premix_list=premix_list_path,
                premix_n=self.premix_files_per_job,
                premix_prefix=self.premix_prefix or "",
                keep_outputs=output_files,
                suffix_outputs=self.n_jobs > 1,
                local=not self.use_condor,
            )
        else:
            wrapper_text = _build_wrapper(
                cfg_files=cfg_paths,
                cmssw_release=self.cmssw_release,
                scram_arch=self.scram_arch,
                output_files=output_files,
                first_seed=self.first_seed,
                output_eos_dir=self.output_eos_dir,
                n_threads=self.n_threads,
                local=not self.use_condor,
                suffix_outputs=self.n_jobs > 1,
                premix_list=premix_list_path,
                premix_n=self.premix_files_per_job,
                premix_prefix=self.premix_prefix or "",
            )
        with open(wrapper_path, "w") as fh:
            fh.write(wrapper_text)
        os.chmod(wrapper_path, 0o755)

        written = {"wrapper": wrapper_path}
        result: Dict[str, Any] = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "mode": "condor" if self.use_condor else "local",
            "container_image": image,
            "container_os": os_tag,
            "seeds": [self.first_seed + i for i in range(self.n_jobs)][:10],
            "n_jobs": self.n_jobs if self.use_condor else 1,
        }

        if not self.use_condor:
            result["written"] = written
            result["run_command"] = f"cd {job_dir} && ./{wrapper_name}"
            result["note"] = (
                "Local mode: no seeding parallelism, one sample only. This is "
                "the escape hatch, not the default -- prefer use_condor=True "
                "for anything past a short check."
            )
            if warnings:
                result["warnings"] = warnings
            return json.dumps(result, indent=2)

        all_inputs = cfg_paths + transfer_in
        extra: Dict[str, str] = {}
        if self.required_os:
            extra["REQUIRED_OS"] = f'"{self.required_os}"'
        if self.desired_sites:
            extra["DESIRED_Sites"] = '"' + ",".join(self.desired_sites) + '"'
        if self.max_wall_time_mins:
            extra["MaxWallTimeMins"] = str(int(self.max_wall_time_mins))
        remote = bool(self.remote_host)
        if remote and not proxy:
            warnings.append(
                "remote_host set but proxy_path is empty: pass the path of a "
                "proxy file ON THE REMOTE HOST (this tool does not copy "
                "credentials).")
        jdl_path = os.path.join(job_dir, "submit.jdl")
        jdl_text = _build_jdl(
            executable=wrapper_name,
            transfer_input_files=all_inputs,
            output_files=output_files,
            image=image,
            n_jobs=self.n_jobs,
            request_cpus=self.request_cpus,
            request_memory_mb=self.request_memory_mb,
            request_disk_kb=self.request_disk_kb,
            proxy_path=proxy,
            stage_out_to_eos=bool(self.output_eos_dir),
            max_retries=self.max_retries,
            suffix_outputs=self.n_jobs > 1,
            extra_classads=extra,
            flat_inputs=remote,
            use_apptainer_image=self.use_apptainer_image,
        )
        with open(jdl_path, "w") as fh:
            fh.write(jdl_text)
        written["jdl"] = jdl_path

        if remote:
            rdir = self.remote_dir or "~/heptapod_jobs"
            ssh = _ssh_base(self.remote_host, self.ssh_key)
            submit_command = (
                f"rsync -a <files> {self.remote_host}:{rdir}/ && "
                + " ".join(shlex.quote(c) for c in ssh)
                + f" 'cd {rdir} && "
                + (f"export X509_USER_PROXY={shlex.quote(proxy)} && " if proxy else "")
                + "mkdir -p logs && condor_submit submit.jdl'"
            )
        else:
            submit_command = f"cd {job_dir} && condor_submit submit.jdl"
        result["written"] = written
        result["submit_command"] = submit_command
        if remote:
            result["remote"] = {"host": self.remote_host,
                                "dir": self.remote_dir or "~/heptapod_jobs"}

        if self.submit:
            try:
                if remote:
                    completed = _remote_submit(
                        self.remote_host, self.remote_dir or "~/heptapod_jobs",
                        self.ssh_key, proxy,
                        [wrapper_path, jdl_path] + all_inputs)
                else:
                    completed = _run_condor_cmd(
                        ["condor_submit", "submit.jdl"],
                        cwd=job_dir, capture_output=True, text=True, timeout=180,
                    )
            except FileNotFoundError:
                result["submitted"] = False
                warnings.append(
                    ("ssh or rsync not found on PATH" if remote
                     else "condor_submit not found on PATH")
                    + " -- files were written but not submitted."
                )
            except subprocess.TimeoutExpired:
                result["submitted"] = False
                warnings.append("submission timed out.")
            else:
                result["submitted"] = completed.returncode == 0
                result["submit_stdout"] = completed.stdout.strip()
                if completed.returncode != 0:
                    result["submit_stderr"] = completed.stderr.strip()
                else:
                    cluster = _parse_cluster_id(completed.stdout)
                    if cluster:
                        result["cluster_id"] = cluster
        else:
            result["submitted"] = False

        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, indent=2)


def _ssh_base(host: str, key: Optional[str] = None) -> List[str]:
    """ssh argv prefix. BatchMode: a password prompt would hang the agent."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]
    if key:
        cmd += ["-i", os.path.expanduser(key)]
    return cmd + [host]


def _remote_submit(host: str, rdir: str, key: Optional[str],
                   proxy: Optional[str], files: List[str]
                   ) -> subprocess.CompletedProcess:
    """
    rsync the job files FLAT into `rdir` on `host`, then condor_submit there.

    Flat because the generated JDL names transfer_input_files by basename.
    `condor_submit` at some pools (CMS Connect) refuses to run in a
    non-interactive shell unless X509_USER_PROXY already points at a valid
    proxy -- it tries to create one and fails with "Failed to create proxy" --
    so the export is part of the same remote command.
    """
    ssh = _ssh_base(host, key)
    prep = subprocess.run(
        ssh + [f"mkdir -p {rdir}/logs"],
        capture_output=True, text=True, timeout=60)
    if prep.returncode != 0:
        return prep
    seen, uniq = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    rsh = " ".join(shlex.quote(c) for c in ssh[:-1])
    sync = subprocess.run(
        ["rsync", "-a", "-e", rsh] + uniq + [f"{host}:{rdir}/"],
        capture_output=True, text=True, timeout=900)
    if sync.returncode != 0:
        return sync
    export = f"export X509_USER_PROXY={shlex.quote(proxy)} && " if proxy else ""
    return subprocess.run(
        ssh + [f"cd {rdir} && {export}condor_submit submit.jdl"],
        capture_output=True, text=True, timeout=180)


def _run_condor_cmd(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """
    Run an HTCondor client command, tolerating shebang-less wrapper scripts.

    Some sites (FNAL LPC among them) ship `condor_submit`/`condor_q` as scripts
    whose FIRST LINE is a shell `exec` rather than a `#!` line, to work around
    LD_LIBRARY_PATH:

        exec env LD_LIBRARY_PATH='' /usr/bin/python3 -Ex "$0" "$@"

    A shell runs these fine, but execve() rejects them with ENOEXEC ("Exec
    format error"), so a plain subprocess call fails on a perfectly working
    installation. Retry through /bin/sh -- passing the script as an argument,
    not as a shell string, so no argument quoting is involved.
    """
    try:
        return subprocess.run(cmd, **kwargs)
    except OSError as e:
        if e.errno != errno.ENOEXEC:
            raise
        resolved = shutil.which(cmd[0]) or cmd[0]
        return subprocess.run(["/bin/sh", resolved] + list(cmd[1:]), **kwargs)


_CLUSTER_RE = re.compile(r"submitted to cluster (\d+)")


def _parse_cluster_id(stdout: str) -> Optional[str]:
    """Pull the cluster id out of condor_submit's confirmation line."""
    match = _CLUSTER_RE.search(stdout or "")
    return match.group(1) if match else None


class CondorJobStatusTool(BaseTool):
    """
    Report the state of an HTCondor cluster: per-status counts, hold reasons,
    and the tail of a failing job's log.

    Read-only. Note that at sites which load-balance across schedd hosts, a
    plain `condor_q <cluster>` may not find a job submitted through a
    different schedd -- pass schedd_name (condor_submit reports which one it
    used) when a known-live cluster comes back empty.

    Input:
        cluster_id: the cluster to query.
        schedd_name: schedd host, if the cluster is not on the default one.
        job_dir: directory holding logs/, for tailing a held job's stderr.
    """

    # ======================== Runtime fields ======================== #
    cluster_id: Optional[str] = RuntimeField(
        default=None, description="Cluster id returned by condor_submit")
    schedd_name: Optional[str] = RuntimeField(
        default=None,
        description="Schedd host, e.g. lpcschedd6.fnal.gov. Needed at sites "
        "that spread submissions across several schedds.",
    )
    job_dir: Optional[str] = RuntimeField(
        default=None,
        description="Job directory containing logs/, for error-log tails.")
    # ================================================================ #

    # ========================= State fields ========================= #
    base_directory: str = StateField(
        description="Base sandbox directory for file operations"
    )
    # ================================================================ #

    _STATUS_NAMES = {
        "1": "idle", "2": "running", "3": "removed",
        "4": "completed", "5": "held", "6": "transferring_output",
        "7": "suspended",
    }

    def _setup(self):
        self.base_directory = os.path.abspath(self.base_directory)

    def _run(self) -> str:
        if not self.cluster_id:
            return self.format_error(
                error="Missing required input",
                reason="cluster_id must be provided.",
            )

        cmd = ["condor_q"]
        if self.schedd_name:
            cmd += ["-name", self.schedd_name]
        cmd += [str(self.cluster_id), "-af", "ProcId", "JobStatus", "HoldReason"]

        try:
            completed = _run_condor_cmd(
                cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            return self.format_error(
                error="condor_q not found",
                reason="HTCondor client tools are not on PATH.",
                suggestion="Run this from a submit node.",
            )
        except subprocess.TimeoutExpired:
            return self.format_error(
                error="condor_q timed out", reason="No response within 120 s.")

        if completed.returncode != 0:
            return self.format_error(
                error="condor_q failed",
                reason=completed.stderr.strip(),
                context=" ".join(shlex.quote(c) for c in cmd),
            )

        counts: Dict[str, int] = {}
        holds: List[Dict[str, str]] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            proc, status = parts[0], parts[1]
            reason = parts[2].strip() if len(parts) > 2 else ""
            name = self._STATUS_NAMES.get(status, f"unknown({status})")
            counts[name] = counts.get(name, 0) + 1
            if name == "held" and reason and reason != "undefined":
                holds.append({"proc": proc, "reason": reason})

        result: Dict[str, Any] = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "cluster_id": str(self.cluster_id),
            "counts": counts,
        }
        if not counts:
            result["note"] = (
                "No jobs in the queue for this cluster: it either finished "
                "(check condor_history) or lives on another schedd (pass "
                "schedd_name)."
            )
        if holds:
            result["held_jobs"] = holds[:20]

        job_dir = self.job_dir
        if job_dir:
            if not os.path.isabs(job_dir):
                job_dir = os.path.join(self.base_directory, job_dir)
            err_dir = os.path.join(job_dir, "logs")
            if os.path.isdir(err_dir):
                tails = {}
                for fname in sorted(os.listdir(err_dir)):
                    if not fname.endswith(".err"):
                        continue
                    fpath = os.path.join(err_dir, fname)
                    try:
                        if os.path.getsize(fpath) == 0:
                            continue
                        with open(fpath) as fh:
                            tails[fname] = "".join(fh.readlines()[-20:])
                    except OSError:
                        continue
                    if len(tails) >= 5:
                        break
                if tails:
                    result["error_log_tails"] = tails

        return json.dumps(result, indent=2)
