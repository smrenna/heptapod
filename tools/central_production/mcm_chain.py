"""
# mcm_chain.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Resolve a central CMS dataset (or a McM request prepid) into the ordered,
structured chain of production steps that actually produced it, by walking
McM's own get_setup scripts rather than hand-copying cmsDriver commands.
See skills/central-production/SKILL.md for the procedure this automates.
"""

import json
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import requests

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "mcm-chain-1.0"

MCM_BASE_URL = "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi"
MAX_CHAIN_LENGTH = 12  # sanity bound against a malformed/looping chain

# Flags that take no value (next token, if any, is itself a flag or absent).
_BOOLEAN_FLAGS = {
    "no_exec", "mc", "data", "fast", "runUnscheduled", "relval",
}

_CMSDRIVER_LINE_RE = re.compile(
    r"^\s*cmsDriver\.py\s+(?P<args>.*?)\s*\|\|\s*exit\s+\$\?\s*;?\s*$",
    re.MULTILINE,
)
_SCRAM_ARCH_RE = re.compile(r"^export SCRAM_ARCH=(\S+)", re.MULTILINE)
_RELEASE_RE = re.compile(r"scram p CMSSW (CMSSW_\S+)")
# Local chain-internal file references follow McM's own naming convention:
# <PREPID>.root or <PREPID>_<N>.root (the trailing "_<N>" is a chain-index
# suffix McM adds, distinct from a prepid's own hyphen-separated sequence
# number, e.g. "...-00001" -- verified against real get_setup output for
# GEN-SIM, DIGI-RECO, MiniAOD, and NanoAOD requests).
_LOCAL_FILEIN_PREPID_RE = re.compile(r"^(?P<prepid>.+?)(?:_\d+)?\.root$")


# ====================================================================== #
# ========================= Helper functions =========================== #
# ====================================================================== #

def _dasgoclient_mcm_prepid(dataset: str, timeout: float = 60.0) -> Optional[str]:
    """
    Resolve a DAS dataset name to the McM request prepid that produced it,
    via `dasgoclient -query="mcm dataset=<dataset>"`.

    Returns None if DAS has no McM tag for the dataset (e.g. it wasn't
    produced through McM at all -- a private/external input). Raises if
    dasgoclient itself is unavailable or fails outright.
    """
    try:
        completed = subprocess.run(
            ["dasgoclient", "-query", f"mcm dataset={dataset}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "dasgoclient not found on PATH -- "
            "source /cvmfs/cms.cern.ch/cmsset_default.sh first"
        ) from e
    if completed.returncode != 0:
        raise RuntimeError(
            f"dasgoclient failed for dataset={dataset!r}: {completed.stderr.strip()}"
        )
    lines = [ln.strip() for ln in completed.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


def _mcm_get_setup(prepid: str, timeout: float = 30.0) -> str:
    """Fetch a request's get_setup shell script text from McM's public REST API."""
    resp = requests.get(f"{MCM_BASE_URL}/requests/get_setup/{prepid}", timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _tokenize_cmsdriver_args(args_str: str) -> Dict[str, Any]:
    """
    Split one cmsDriver.py invocation's argument string into a flag dict,
    plus an optional leading positional fragment path.

    McM's get_setup always writes each cmsDriver call as a single flattened
    line ending in `|| exit $?`, so no multi-line handling is needed here.
    shlex.split handles quoted values (e.g. --pileup_input "dbs:/...")
    transparently, including stripping the quotes.
    """
    tokens = shlex.split(args_str)
    flags: Dict[str, Any] = {}
    fragment_path = None

    i = 0
    if tokens and not tokens[0].startswith("-"):
        fragment_path = tokens[0]
        i = 1

    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            # Stray positional token outside the leading-fragment slot --
            # keep it rather than silently dropping it.
            flags.setdefault("_positional", []).append(tok)
            i += 1
            continue
        key = tok.lstrip("-")
        has_value = (
            i + 1 < len(tokens)
            and not tokens[i + 1].startswith("-")
            and key not in _BOOLEAN_FLAGS
        )
        if has_value:
            flags[key] = tokens[i + 1]
            i += 2
        else:
            flags[key] = True
            i += 1

    return {"fragment_path": fragment_path, "flags": flags}


def _parse_mcm_setup_script(script_text: str, prepid: str) -> Dict[str, Any]:
    """
    Parse one McM get_setup script into {cmssw_release, scram_arch, steps}.

    A single request's setup script sets up exactly one CMSSW release but
    can contain more than one cmsDriver.py invocation -- e.g. a DIGI-RECO
    request expands into a separate DIGI/HLT command and RECO command
    sharing one prepid (SKILL.md Section 2, "4 requests, 5 cmsDrivers").
    Every match is returned, in the order they appear.
    """
    scram_match = _SCRAM_ARCH_RE.search(script_text)
    release_match = _RELEASE_RE.search(script_text)
    scram_arch = scram_match.group(1) if scram_match else None
    cmssw_release = release_match.group(1) if release_match else None

    commands = _CMSDRIVER_LINE_RE.findall(script_text)
    steps: List[Dict[str, Any]] = []

    for args_str in commands:
        try:
            parsed = _tokenize_cmsdriver_args(args_str)
            flags = parsed["flags"]
            steps.append({
                "mcm_prepid": prepid,
                "cmssw_release": cmssw_release,
                "scram_arch": scram_arch,
                "needs_fragment": parsed["fragment_path"] is not None,
                "fragment_path": parsed["fragment_path"],
                "step": flags.get("step"),
                "conditions": flags.get("conditions"),
                "era": flags.get("era"),
                "geometry": flags.get("geometry"),
                "eventcontent": flags.get("eventcontent"),
                "datatier": flags.get("datatier"),
                "filein": flags.get("filein"),
                "fileout": flags.get("fileout"),
                "pileup_input": flags.get("pileup_input"),
                "requires_grid_proxy": "pileup_input" in flags,
                "flags": flags,
                "raw_command": f"cmsDriver.py {args_str}",
                "parse_status": "ok",
            })
        except Exception as e:
            steps.append({
                "mcm_prepid": prepid,
                "cmssw_release": cmssw_release,
                "scram_arch": scram_arch,
                "raw_command": f"cmsDriver.py {args_str}",
                "parse_status": "partial",
                "parse_error": str(e),
            })

    return {
        "cmssw_release": cmssw_release,
        "scram_arch": scram_arch,
        "steps": steps,
        "parse_status": "ok" if commands else "no_cmsdriver_found",
    }


def _resolve_parent_prepid(
    filein: Optional[str],
    dasgoclient_fn=_dasgoclient_mcm_prepid,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Given the first cmsDriver command's --filein value for a request,
    determine the prepid of the request that produced its input, if any.

    Returns (parent_prepid, warning). Exactly one of the two is non-None
    unless filein is absent (root request, both None, no warning needed).

    - No filein at all: this is the chain root (GEN-SIM, built from a
      fragment rather than a parent's output). (None, None)
    - "file:X.root": a chain-internal reference to a step run in the same
      job/directory. McM's own local filenames are "<PREPID>[_<N>].root",
      so the parent prepid is read directly off the filename -- no lookup.
    - "dbs:/.../TIER": an external DBS-registered dataset. Resolved via
      dasgoclient's "mcm dataset=" query (the same query used to resolve
      the tool's own `dataset` input). If DAS has no McM tag for it (e.g.
      a private/non-McM predecessor, or pileup input -- which is filtered
      out by the caller before this is reached), the walk stops there.
    """
    if not filein:
        return None, None

    if filein.startswith("file:"):
        local_name = filein[len("file:"):]
        m = _LOCAL_FILEIN_PREPID_RE.match(local_name)
        if not m:
            return None, (
                f"Could not parse a prepid out of local filein {filein!r} "
                "-- chain walk stopped here."
            )
        return m.group("prepid"), None

    if filein.startswith("dbs:"):
        dataset = filein[len("dbs:"):].strip()
        parent_prepid = dasgoclient_fn(dataset)
        if not parent_prepid:
            return None, (
                f"dasgoclient found no McM request for parent dataset "
                f"{dataset!r} -- chain walk stopped here (it may be a "
                "private or non-McM-produced predecessor)."
            )
        return parent_prepid, None

    return None, f"Unrecognized --filein value {filein!r} -- chain walk stopped here."


# ====================================================================== #
# =============================== Tool =================================== #
# ====================================================================== #

class MCMChainTool(BaseTool):
    """
    Resolve a central CMS dataset (or a known McM request prepid) into the
    ordered, structured chain of production steps used to actually produce
    it -- release, SCRAM_ARCH, and parsed cmsDriver args for every step from
    GEN-SIM through to the requested tier -- pulled from McM's own
    get_setup scripts rather than hand-copied.

    Walks backward from the given dataset/prepid using each step's own
    first cmsDriver command's --filein value: a local "file:X.root"
    reference encodes its producing prepid directly in the filename (McM's
    own naming convention), while a "dbs:/.../TIER" reference is resolved
    via the same DAS "mcm dataset=" lookup used for the tool's own `dataset`
    input. The walk stops once a request has no --filein at all (the chain
    root, generated from a fragment) or when a --filein can't be resolved
    further (e.g. a private predecessor DAS has no McM tag for).

    (McM's own `input_dataset` request field looks like the obvious source
    for this, but is only populated when a step's input was an
    already-existing DBS dataset at request-authoring time -- it is blank
    for steps whose input hadn't been produced yet, e.g. DIGI-RECO
    following GEN-SIM in the same chain -- so it isn't used here.)

    Companion to skills/central-production/SKILL.md: this tool answers
    "what are the steps", not "run them" -- --filein values in the
    returned steps still point at McM's assumed inputs (local filenames
    that only exist in a validation job, or DBS datasets) and must be
    re-pointed at your own local output files before executing anything
    (SKILL.md Section 4), and any fragment the root step needs still has
    to be fetched separately (get_fragment) and, for an unfiltered
    reproduction, edited by hand (SKILL.md Section 3).

    Input:
        dataset: full DAS dataset name for any tier in the chain (e.g. the
            terminal NanoAODSIM dataset). Resolved to a McM prepid via
            `dasgoclient -query="mcm dataset=<dataset>"`.
        request_prepid: a McM request prepid to start from directly,
            skipping the DAS dataset lookup above (use when the dataset
            isn't registered in DBS yet, or the prepid is already known).

    Exactly one of dataset / request_prepid must be given.
    """

    # ======================== Runtime fields ======================== #
    dataset: Optional[str] = RuntimeField(
        default=None,
        description="Full DAS dataset name for any tier in the chain, e.g. "
        "/TTto2L2Nu_HT-500_NJet-7_.../Run3Summer22EENanoAODv12-.../NANOAODSIM"
    )
    request_prepid: Optional[str] = RuntimeField(
        default=None,
        description="McM request prepid to start from directly, skipping "
        "the DAS dataset lookup (e.g. 'TOP-Run3Summer22EENanoAODv12-00001')"
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
        if not self.dataset and not self.request_prepid:
            return json.dumps({
                "status": "error",
                "schema": SCHEMA_VERSION,
                "reason": "Provide either 'dataset' or 'request_prepid'.",
            }, indent=2)

        try:
            start_prepid = self.request_prepid or _dasgoclient_mcm_prepid(self.dataset)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "schema": SCHEMA_VERSION,
                "reason": str(e),
            }, indent=2)

        if not start_prepid:
            return json.dumps({
                "status": "error",
                "schema": SCHEMA_VERSION,
                "reason": f"dasgoclient found no McM request for dataset={self.dataset!r} "
                "-- check the dataset name, or pass request_prepid directly.",
            }, indent=2)

        try:
            chain, warnings = _walk_and_parse_chain(start_prepid)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "schema": SCHEMA_VERSION,
                "reason": str(e),
            }, indent=2)

        steps: List[Dict[str, Any]] = []
        for prepid, parsed in chain:
            steps.extend(parsed["steps"])

        result = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "chain_prepids": [prepid for prepid, _ in chain],
            "steps": steps,
        }
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, indent=2)


def _walk_and_parse_chain(
    start_prepid: str,
    get_setup_fn=_mcm_get_setup,
    resolve_parent_fn=_resolve_parent_prepid,
) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
    """
    Fetch and parse get_setup for start_prepid and every ancestor, walking
    backward via each step's own --filein (see _resolve_parent_prepid).

    Returns (chain, warnings) where chain is [(prepid, parsed_setup), ...]
    ordered root (GEN-SIM) first, terminal (start_prepid) last.
    """
    chain: List[Tuple[str, Dict[str, Any]]] = []
    warnings: List[str] = []
    prepid = start_prepid
    seen = set()

    while True:
        if prepid in seen:
            raise RuntimeError(f"Cycle detected in chain at prepid={prepid}")
        if len(chain) >= MAX_CHAIN_LENGTH:
            raise RuntimeError(
                f"Chain exceeded {MAX_CHAIN_LENGTH} steps without reaching a "
                "root request -- likely a parsing bug, not a real chain."
            )
        seen.add(prepid)

        try:
            setup_text = get_setup_fn(prepid)
            parsed = _parse_mcm_setup_script(setup_text, prepid)
        except Exception as e:
            warnings.append(f"{prepid}: failed to fetch/parse get_setup ({e})")
            chain.append((prepid, {"steps": [{
                "mcm_prepid": prepid,
                "parse_status": "fetch_failed",
                "parse_error": str(e),
            }]}))
            break

        if parsed["parse_status"] != "ok":
            warnings.append(
                f"{prepid}: {parsed['parse_status']} -- no cmsDriver command "
                "found in its get_setup script, check it manually."
            )

        chain.append((prepid, parsed))

        first_filein = parsed["steps"][0].get("filein") if parsed["steps"] else None
        parent_prepid, warning = resolve_parent_fn(first_filein)
        if warning:
            warnings.append(f"{prepid}: {warning}")
        if not parent_prepid:
            break
        prepid = parent_prepid

    chain.reverse()
    return chain, warnings
