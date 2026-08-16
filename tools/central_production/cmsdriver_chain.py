"""
# cmsdriver_chain.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Turn a resolved production chain into runnable `cmsDriver.py` commands.

This closes the gap between the two ends of the central-production path:
`MCMChainTool` answers "what are the steps" and `CMSSWJobSubmitTool` runs
cfgs on HTCondor, but nothing turned the first into the second -- that was
hand-written shell, retyped per production, and it is where the campaign
conditions get silently mistyped.

The substantive work is re-pointing the inputs. McM's `--filein` values name
files that exist only inside McM's own validation job (`file:<PREPID>.root`)
or datasets in DBS (`dbs:/.../TIER`); neither exists locally. Each step's
input must become the previous step's output. Doing that by hand is
skills/central-production/SKILL.md section 4, and it is the step most often
got wrong, because a stale `--filein` fails late and blames the file.
"""

import json
import os
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "cmsdriver-chain-1.0"

# Flags that carry McM bookkeeping rather than physics, and that would either
# fight our own event counts or refer to McM-internal filenames.
_DROP_FLAGS = {
    "number", "n", "number_out", "python_filename", "fileout", "filein",
    "no_exec", "customise_commands",
    # Emitted explicitly at the end of every command; passing them through as
    # well would duplicate them on the command line.
    "mc", "data",
}

# Order matters only for readability of the emitted command.
_FLAG_ORDER = [
    "step", "conditions", "beamspot", "era", "eventcontent", "datatier",
    "geometry", "datamix", "procModifiers", "pileup_input", "scenario",
    "customise",
]

_TIER_HINT = {
    "LHE": "LHE", "GEN": "GS", "SIM": "GS", "DIGI": "DR", "RECO": "AOD",
    "PAT": "MiniAOD", "NANO": "NanoAOD",
}


# ====================================================================== #
# ========================= Helper functions =========================== #
# ====================================================================== #

def _short_tier(step_value: str, index: int) -> str:
    """
    A short, human-legible name for a step's output, e.g. GS, DR, MiniAOD.

    Derived from the --step value rather than the prepid, so the filenames say
    what the file is instead of which McM request happened to make it.
    """
    parts = [p.split(":")[0] for p in (step_value or "").split(",")]
    for key in ("NANO", "PAT", "RECO", "DIGI", "SIM", "GEN", "LHE"):
        if key in parts:
            return _TIER_HINT[key]
    return f"step{index + 1}"


def _rewrite_chain(
    steps: List[Dict[str, Any]],
    prefix: str,
    events_per_job: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Re-point every step's input at the previous step's output.

    Returns (cfgs, warnings). The first step keeps whatever fragment it needs
    and gets no --filein; every later step reads the file its predecessor
    writes, by bare relative name, because that is what resolves in a condor
    job's scratch directory (PoolSource does not chdir the way
    ExternalLHEProducer's helper does).
    """
    cfgs: List[Dict[str, Any]] = []
    warnings: List[str] = []
    previous_out: Optional[str] = None

    for idx, step in enumerate(steps):
        flags = dict(step.get("flags") or {})
        # MCMChainTool exposes parsed values at the top level too; prefer those.
        for key in ("step", "conditions", "beamspot", "era", "eventcontent",
                    "datatier", "geometry", "datamix", "procModifiers",
                    "pileup_input", "scenario"):
            if step.get(key) is not None:
                flags[key] = step[key]

        tier = _short_tier(flags.get("step", ""), idx)
        fileout = f"{prefix}_{tier}.root"
        name = f"{prefix}_{tier}_cfg.py"

        original_filein = step.get("filein")
        if idx == 0:
            filein = None
            if original_filein:
                warnings.append(
                    f"Step 1 ({tier}) has --filein {original_filein!r}; it is "
                    "dropped, since the chain root is generated from a "
                    "fragment. If this really is not the root, pass the "
                    "missing upstream steps."
                )
        else:
            filein = f"file:{previous_out}"
            if original_filein and original_filein.startswith("dbs:"):
                warnings.append(
                    f"Step {idx + 1} ({tier}) read {original_filein!r} in "
                    "McM; re-pointed at the local "
                    f"{previous_out}. The upstream dataset is NOT downloaded."
                )

        cfg = {
            "index": idx,
            "tier": tier,
            "python_filename": name,
            "fileout": fileout,
            "filein": filein,
            "cmssw_release": step.get("cmssw_release"),
            "scram_arch": step.get("scram_arch"),
            "needs_fragment": bool(step.get("needs_fragment")),
            "fragment_path": step.get("fragment_path"),
            "flags": flags,
            "events": events_per_job if idx == 0 else -1,
        }
        cfgs.append(cfg)
        previous_out = fileout

    return cfgs, warnings


def _build_cmsdriver_command(
    cfg: Dict[str, Any],
    fragment_path: Optional[str] = None,
) -> str:
    """Render one cmsDriver.py invocation. Always --no_exec: condor runs it."""
    parts = ["cmsDriver.py"]

    if cfg["needs_fragment"]:
        parts.append(fragment_path or cfg.get("fragment_path") or "FRAGMENT.py")

    flags = cfg["flags"]
    for key in _FLAG_ORDER:
        if key in flags and flags[key] is not None:
            parts.append(f"--{key} {shlex.quote(str(flags[key]))}")
    # Anything the campaign carried that is not in the canonical order, so a
    # flag we have not seen before is passed through rather than dropped.
    for key in sorted(flags):
        if key in _FLAG_ORDER or key in _DROP_FLAGS or key.startswith("_"):
            continue
        value = flags[key]
        parts.append(f"--{key}" if value is True
                     else f"--{key} {shlex.quote(str(value))}")

    parts.append(f"--python_filename {cfg['python_filename']}")
    parts.append(f"--fileout file:{cfg['fileout']}")
    if cfg["filein"]:
        parts.append(f"--filein {cfg['filein']}")
    parts.append("--no_exec --mc")
    parts.append(f"-n {cfg['events']}")
    return " \\\n  ".join(parts)


def _group_by_release(cfgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Split the chain at release boundaries, preserving order.

    Steps sharing a release can be generated -- and later run -- in one place;
    a release change means a separate area and a separate condor cluster,
    because a job sets up exactly one CMSSW environment.
    """
    groups: List[Dict[str, Any]] = []
    for cfg in cfgs:
        key = (cfg["cmssw_release"], cfg["scram_arch"])
        if groups and (groups[-1]["cmssw_release"],
                       groups[-1]["scram_arch"]) == key:
            groups[-1]["cfgs"].append(cfg)
        else:
            groups.append({"cmssw_release": cfg["cmssw_release"],
                           "scram_arch": cfg["scram_arch"],
                           "cfgs": [cfg]})
    return groups


def _build_make_cfgs_script(
    groups: List[Dict[str, Any]],
    fragment_path: Optional[str],
    work_dir: str,
) -> str:
    """
    Emit the script that writes every cfg.

    cmsDriver has to run inside a release area, so this sets one up per
    release group. It is deliberately a script rather than something the tool
    executes itself: it needs a CMS environment (and often an EL8 container)
    that is the caller's to provide.
    """
    lines = [
        "#!/bin/bash",
        "# Generated by heptapod CMSDriverChainTool.",
        "# Writes every cfg with --no_exec; running them is the condor bundle's job.",
        "set -euo pipefail",
        "unset PYTHONPATH",
        f"BASE={shlex.quote(work_dir)}",
        "cd $BASE",
        "source /cvmfs/cms.cern.ch/cmsset_default.sh",
        "",
    ]
    for gi, group in enumerate(groups):
        release = group["cmssw_release"] or "CMSSW_UNKNOWN"
        arch = group["scram_arch"] or "el8_amd64_gcc10"
        lines += [
            f"# ---------- release group {gi + 1}: {release} / {arch} ----------",
            "cd $BASE",
            f"export SCRAM_ARCH={arch}",
            f"if [ ! -d {release} ]; then cmsrel {release}; fi",
        ]
        needs_fragment = any(c["needs_fragment"] for c in group["cfgs"])
        if needs_fragment and fragment_path:
            frag = os.path.basename(fragment_path)
            lines += [
                f"mkdir -p {release}/src/Configuration/GenProduction/python",
                f"cp {shlex.quote(fragment_path)} "
                f"{release}/src/Configuration/GenProduction/python/",
            ]
        lines += [
            f"cd {release}/src",
            "eval `scram runtime -sh`",
        ]
        if needs_fragment and fragment_path:
            lines.append("scram b -j 4")
        lines.append("")
        for cfg in group["cfgs"]:
            frag_arg = None
            if cfg["needs_fragment"] and fragment_path:
                frag_arg = ("Configuration/GenProduction/python/"
                            + os.path.basename(fragment_path))
            lines.append(f"# step {cfg['index'] + 1}: {cfg['tier']}")
            lines.append(_build_cmsdriver_command(cfg, frag_arg))
            lines.append("")
    lines.append('echo "=== cfgs written ==="')
    return "\n".join(lines)


# ====================================================================== #
# =============================== Tool ================================= #
# ====================================================================== #

class CMSDriverChainTool(BaseTool):
    """
    Turn a resolved production chain into runnable cmsDriver commands, with
    every step's input re-pointed at the previous step's output.

    Consumes `MCMChainTool` output directly (pass its `steps` list, or the
    JSON it returned), and produces cfg specifications that feed
    `CMSSWJobSubmitTool` -- one condor cluster per release group.

    What it handles that hand-written shell keeps getting wrong:

      * **Re-pointing `--filein`.** McM's values name files that exist only in
        McM's validation job (`file:<PREPID>.root`) or datasets in DBS. Each
        becomes the previous step's local output; a `dbs:` input that is being
        replaced is reported, since that upstream dataset is not downloaded.
      * **Release boundaries.** The chain is split wherever the release or arch
        changes, because one job sets up one CMSSW environment. Each group is
        a separate cluster.
      * **Campaign conditions carried verbatim.** Conditions, beamspot, era,
        eventcontent, datatier, geometry, datamix, procModifiers and
        pileup_input are passed through unmodified -- retyping them by hand is
        how a "same conditions as X" sample quietly stops matching X.
      * **`--no_exec` always**, since running is the condor bundle's job, and
        McM's own `-n`/`--number_out` are dropped in favour of your event count.

    Input:
        steps: the `steps` list from MCMChainTool (or the whole JSON).
        prefix: name stem for the generated cfgs and files, e.g. MySignal.
        fragment_path: GEN fragment for the root step. For an unfiltered
            reproduction this is your edited fragment, not McM's.
        events: events for the root step; later steps use -1 (all input).

    Output: JSON with per-step cfg specs, release groups, the generated
    script's path, and warnings.
    """

    # ======================== Runtime fields ======================== #
    steps: Optional[Any] = RuntimeField(
        default=None,
        description="MCMChainTool's `steps` list, or its full JSON output.",
    )
    prefix: str = RuntimeField(
        default="chain",
        description="Name stem for generated cfgs and output files.",
    )
    fragment_path: Optional[str] = RuntimeField(
        default=None,
        description="GEN fragment for the root step. For a reproduction "
        "without the central sample's filters, this is your edited copy.",
    )
    events: int = RuntimeField(
        default=100,
        description="Events for the root step. Later steps read all input.",
    )
    work_dir: Optional[str] = RuntimeField(
        default=None,
        description="Directory the generated script sets up releases in. "
        "Defaults to base_directory.",
    )
    script_name: str = RuntimeField(
        default="make_cfgs.sh",
        description="Filename for the generated cfg-writing script.",
    )
    # ================================================================ #

    # ========================= State fields ========================= #
    base_directory: str = StateField(
        description="Base sandbox directory for file operations"
    )
    # ================================================================ #

    def _setup(self):
        self.base_directory = os.path.abspath(self.base_directory)

    def _run(self) -> str:
        raw = self.steps
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                return self.format_error(
                    error="steps is not valid JSON", reason=str(e))
        if isinstance(raw, dict):
            raw = raw.get("steps")
        if not raw or not isinstance(raw, list):
            return self.format_error(
                error="Missing required input",
                reason="steps must be MCMChainTool's steps list (or its JSON).",
                suggestion="Run MCMChainTool on the dataset first.",
            )

        work_dir = self.work_dir or self.base_directory
        if not os.path.isabs(work_dir):
            work_dir = os.path.join(self.base_directory, work_dir)
        os.makedirs(work_dir, exist_ok=True)

        cfgs, warnings = _rewrite_chain(raw, self.prefix, self.events)
        groups = _group_by_release(cfgs)

        if any(c["needs_fragment"] for c in cfgs) and not self.fragment_path:
            warnings.append(
                "The root step needs a fragment but fragment_path was not "
                "given; the generated command has a FRAGMENT.py placeholder. "
                "Fetch McM's with get_fragment and edit it -- an unfiltered "
                "reproduction means removing its ProductionFilterSequence "
                "filters, not just its cuts."
            )

        missing_release = [c["tier"] for c in cfgs if not c["cmssw_release"]]
        if missing_release:
            warnings.append(
                f"No CMSSW release resolved for: {', '.join(missing_release)}. "
                "The generated script will not set up a usable area for those."
            )

        script_path = os.path.join(work_dir, self.script_name)
        with open(script_path, "w") as fh:
            fh.write(_build_make_cfgs_script(
                groups, self.fragment_path, work_dir))
        os.chmod(script_path, 0o755)

        result: Dict[str, Any] = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "script": script_path,
            "n_steps": len(cfgs),
            "release_groups": [
                {
                    "cmssw_release": g["cmssw_release"],
                    "scram_arch": g["scram_arch"],
                    "cfgs": [c["python_filename"] for c in g["cfgs"]],
                    "final_output": g["cfgs"][-1]["fileout"],
                }
                for g in groups
            ],
            "cfgs": [
                {k: c[k] for k in ("index", "tier", "python_filename",
                                   "filein", "fileout", "cmssw_release",
                                   "scram_arch", "needs_fragment", "events")}
                for c in cfgs
            ],
            "next_step": (
                "Run the script in a CMS environment (cmssw-elN if the release "
                "predates this host's OS), then submit each release group as "
                "one CMSSWJobSubmitTool cluster, passing that group's cfgs as "
                "cfg_files so intermediates stay on the worker's scratch."
            ),
        }
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, indent=2)
