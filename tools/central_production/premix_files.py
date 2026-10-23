"""
# premix_files.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

List the files of a premixed-pileup dataset that are on disk at a given site,
as xrootd URLs, and write them one per line for CMSSWJobSubmitTool(premix_list=).

Why this exists: a DIGI-premix step needs `process.mixData.input.fileNames`.
`cmsDriver.py --pileup_input dbs:/...` resolves that through dasgoclient at
cfg-creation time, which fails (or silently leaves the list empty) when
dasgoclient is not on PATH. And choosing files at random is a trap: premix
datasets are tens of TB, only some blocks are resident at a given site, and a
file that is not on disk there hangs for minutes in the xrootd redirector
("Operation expired") before failing.

The DAS query that works is the BLOCK route:

    dasgoclient -query="block dataset=<D> site=<SITE>"     -> blocks at the site
    dasgoclient -query="file block=<block>"                -> files in a block

The more obvious `file dataset=<D> site=<SITE>` returned nothing in practice
for the datasets tried, which is easy to misread as "no files at that site".
"""

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "premix-files-1.0"

_DEFAULT_REDIRECTOR = "root://cmsxrootd.fnal.gov/"
_DATASET_RE = re.compile(r"^/[^/\s]+/[^/\s]+/[^/\s]+$")
_BLOCK_RE = re.compile(r"^/[^/\s]+/[^/\s]+/[^/\s]+#[0-9a-fA-F-]+$")
_LFN_RE = re.compile(r"^/store/\S+\.root$")


def _das(query: str, timeout: float = 120.0) -> List[str]:
    """Run one dasgoclient query; one non-empty line per result."""
    completed = subprocess.run(
        ["dasgoclient", "-query", query],
        capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"dasgoclient failed for {query!r}: {completed.stderr.strip()}")
    return [ln.strip() for ln in completed.stdout.splitlines() if ln.strip()]


def _select_files(
    blocks: List[str],
    files_in_block: Callable[[str], List[str]],
    max_files: int,
    max_blocks: Optional[int] = None,
) -> List[str]:
    """
    Collect up to `max_files` LFNs, one block at a time, in block order.

    Stops querying as soon as enough files are in hand: a premix dataset can
    have thousands of blocks and each lookup is a network round trip.
    Duplicates are dropped (a file belongs to one block, but be defensive).
    """
    chosen: List[str] = []
    seen = set()
    for n, block in enumerate(blocks):
        if max_blocks is not None and n >= max_blocks:
            break
        for lfn in files_in_block(block):
            if _LFN_RE.match(lfn) and lfn not in seen:
                seen.add(lfn)
                chosen.append(lfn)
                if len(chosen) >= max_files:
                    return chosen
    return chosen


class PremixFileListTool(BaseTool):
    """
    List a premix pileup dataset's files that are on disk at a site, as xrootd
    URLs, and write them to a file for CMSSWJobSubmitTool(premix_list=...).

    Use this when a DIGI-premix cfg has an empty or unresolved
    `process.mixData.input.fileNames` (cmsDriver could not resolve the
    `dbs:` pileup dataset) or before submitting many jobs, so each job reads
    files that are really resident at the site you read from.

    Needs `dasgoclient` on PATH (`source /cvmfs/cms.cern.ch/cmsset_default.sh`
    then `export PATH=$PATH:/cvmfs/cms.cern.ch/common`). Reading the files
    later needs a grid proxy; some sites' data servers also need
    XrdSecGSISRVNAMES='*' (CMSSWJobSubmitTool's chain wrapper sets it).

    Input:
        dataset: premix dataset, e.g. /<gun>/<campaign>-.../PREMIX.
        site: CMS site name that must hold the blocks, e.g. T1_US_FNAL_Disk.
        out_path: file to write, one URL per line (relative to base_directory).
        max_files: stop after this many files (default 200).
        redirector: xrootd prefix to put in front of each /store path. The
            redirector should be one that can serve that site.
        max_blocks: optional cap on blocks examined.

    Output (JSON): status, n_blocks, n_files, out_path, first_files, and a
    reminder of how to use it.
    """

    # ======================== Runtime fields ======================== #
    dataset: str = RuntimeField(
        description="Premix DAS dataset name, /<primary>/<processed>/<tier>")
    site: str = RuntimeField(
        description="Site that must hold the blocks, e.g. T1_US_FNAL_Disk")
    out_path: str = RuntimeField(
        description="File to write (one URL per line), relative to "
        "base_directory")
    max_files: int = RuntimeField(
        default=200, description="Stop after this many files.")
    redirector: str = RuntimeField(
        default=_DEFAULT_REDIRECTOR,
        description="xrootd prefix for each /store LFN, e.g. "
        "root://cmsxrootd.fnal.gov/ . Use an empty string to write bare LFNs "
        "(then pass premix_prefix to CMSSWJobSubmitTool).")
    max_blocks: Optional[int] = RuntimeField(
        default=None, description="Optional cap on blocks examined.")
    # ================================================================ #

    # ========================= State fields ========================= #
    base_directory: str = StateField(
        description="Base sandbox directory for file operations")
    # ================================================================ #

    # Seams for tests: replaced to avoid needing dasgoclient or a network.
    _blocks_fn: Optional[Callable[[str, str], List[str]]] = None
    _files_fn: Optional[Callable[[str], List[str]]] = None

    def _setup(self):
        self.base_directory = os.path.abspath(self.base_directory)

    def _safe_out(self) -> Optional[str]:
        path = self.out_path if os.path.isabs(self.out_path) else os.path.join(
            self.base_directory, self.out_path)
        path = os.path.abspath(path)
        if os.path.commonpath([path, self.base_directory]) != self.base_directory:
            return None
        return path

    def _run(self) -> str:
        try:
            self._setup()
        except Exception as e:
            return self.format_error(error="Setup Error", reason=str(e))

        if not _DATASET_RE.match(self.dataset or ""):
            return self.format_error(
                error="Bad dataset name",
                reason=f"{self.dataset!r} is not /<primary>/<processed>/<tier>.")
        if not self.site or re.search(r"\s", self.site):
            return self.format_error(
                error="Bad site", reason="site must be a CMS site name, "
                "e.g. T1_US_FNAL_Disk.")
        if self.max_files < 1:
            return self.format_error(
                error="Bad max_files", reason="max_files must be >= 1.")
        out = self._safe_out()
        if out is None:
            return self.format_error(
                error="Path outside sandbox",
                reason=f"{self.out_path!r} resolves outside base_directory.")

        blocks_fn = self._blocks_fn or (
            lambda d, s: _das(f"block dataset={d} site={s}"))
        files_fn = self._files_fn or (lambda b: _das(f"file block={b}"))

        try:
            blocks = [b for b in blocks_fn(self.dataset, self.site)
                      if _BLOCK_RE.match(b)]
        except FileNotFoundError:
            return self.format_error(
                error="dasgoclient not found",
                reason="dasgoclient is not on PATH.",
                suggestion="source /cvmfs/cms.cern.ch/cmsset_default.sh; "
                           "export PATH=$PATH:/cvmfs/cms.cern.ch/common")
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            return self.format_error(error="DAS query failed", reason=str(e))

        if not blocks:
            return self.format_error(
                error="No blocks at site",
                reason=f"DAS lists no blocks of {self.dataset} at "
                       f"{self.site}.",
                suggestion="Check the site name (disk endpoints end in "
                           "_Disk) and that the dataset is resident there; "
                           "reading from a site that does not hold it hangs "
                           "in the redirector instead of failing fast.")

        try:
            lfns = _select_files(blocks, files_fn, self.max_files,
                                 self.max_blocks)
        except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
            return self.format_error(error="DAS query failed", reason=str(e))
        if not lfns:
            return self.format_error(
                error="No files found",
                reason=f"{len(blocks)} block(s) found at {self.site} but "
                       "`file block=` returned no .root files.")

        urls = [f"{self.redirector}{l}" if self.redirector else l for l in lfns]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as fh:
            fh.write("\n".join(urls) + "\n")

        return json.dumps({
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "dataset": self.dataset,
            "site": self.site,
            "n_blocks": len(blocks),
            "n_files": len(urls),
            "out_path": os.path.relpath(out, self.base_directory),
            "first_files": urls[:3],
            "next": "Pass out_path as CMSSWJobSubmitTool(premix_list=...); "
                    "ship a grid proxy with the job.",
        }, indent=2)
