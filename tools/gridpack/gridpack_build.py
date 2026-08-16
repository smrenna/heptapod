"""
# gridpack_build.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Drive `gridpack_generation.sh` for a private/BSM UFO model.

The script itself is short; what this tool encodes is the three path
constraints around it, each of which fails in a way that does not name its
cause (see skills/cmssw/references/pitfalls.md and skills/cmssw/SKILL.md):

  * `carddir` must be RELATIVE to the script's own directory, because
    gridpack_generation.sh does `CARDSDIR=${PRODHOME}/${carddir}` with no
    leading-slash check -- an absolute path is concatenated onto PRODHOME and
    reported as "<PRODHOME>//abs/path does not exist!";
  * the EL8 container binds only the CURRENT WORKING DIRECTORY, so it must be
    launched from a common ancestor of genproductions AND of any absolute path
    the proc card imports a model from, or the model is simply invisible;
  * SCRAM_ARCH must match the release the gridpack will later be showered in,
    since LHE generation is compiled Fortran/C++ (pitfall P6).
"""

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "gridpack-build-1.0"

# SCRAM_ARCH prefix -> the cmssw-elN wrapper that provides a matching OS on a
# host running something else. Same axis as the condor bundle's ApptainerImage
# map, different mechanism: a wrapper command here, a ClassAd there.
_ARCH_CONTAINER: Dict[str, str] = {
    "slc7": "cmssw-el7",
    "el8": "cmssw-el8",
    "el9": "cmssw-el9",
}

# `import model /abs/path/to/UFO` in a proc card. Bare model names (stock, or
# fetched via _extramodels.dat) have no path and need no bind-mount handling.
_IMPORT_MODEL_RE = re.compile(
    r"^\s*import\s+model\s+(?P<model>\S+)", re.MULTILINE)

# genproductions reports the integrated cross-section per run block.
_XSEC_RE = re.compile(r"Cross-section\s*:\s*([0-9.eE+-]+)\s*\+-\s*([0-9.eE+-]+)\s*pb")


# ====================================================================== #
# ========================= Helper functions =========================== #
# ====================================================================== #

def _container_for_arch(scram_arch: str) -> Optional[str]:
    """el8_amd64_gcc10 -> cmssw-el8. None for an unrecognized arch."""
    if not scram_arch:
        return None
    return _ARCH_CONTAINER.get(scram_arch.split("_", 1)[0])


def _proc_card_model_path(proc_card_text: str) -> Optional[str]:
    """
    Absolute model path from a proc card's `import model`, if it has one.

    Returns None for a bare model name, which resolves inside the sandboxed
    MG5's own models/ directory and imposes no bind-mount constraint.
    """
    match = _IMPORT_MODEL_RE.search(proc_card_text or "")
    if not match:
        return None
    model = match.group("model")
    # MG5 allows `model-restriction`; the path is the part before the dash only
    # when the dash is not inside the path itself, so split on the last one.
    return model if os.path.isabs(model) else None


def _common_launch_dir(paths: List[str]) -> str:
    """
    Deepest directory containing every path the build must be able to read.

    The container binds only the cwd, so launching from anywhere below this
    makes one of the inputs invisible -- reported as a plain "No such file or
    directory" with no hint that a bind mount is involved.
    """
    real = [os.path.realpath(p) for p in paths if p]
    if not real:
        return os.getcwd()
    if len(real) == 1:
        return real[0] if os.path.isdir(real[0]) else os.path.dirname(real[0])
    return os.path.commonpath(real)


def _relative_carddir(cards_dir: str, prodhome: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Express cards_dir relative to PRODHOME, or explain why it cannot be.

    Returns (relative_path, error). gridpack_generation.sh concatenates
    PRODHOME and carddir unconditionally, so anything outside the tree has to
    be copied in first -- which the tool does rather than failing.
    """
    cards_real = os.path.realpath(cards_dir)
    prod_real = os.path.realpath(prodhome)
    if not os.path.isdir(cards_real):
        return None, f"Cards directory {cards_dir!r} does not exist."
    try:
        rel = os.path.relpath(cards_real, prod_real)
    except ValueError as e:                      # different drives, Windows only
        return None, str(e)
    if rel.startswith(os.pardir):
        return None, "outside"
    return rel, None


def _parse_gridpack_log(text: str) -> Dict[str, Any]:
    """
    Pull the numbers worth checking out of a gridpack_generation log.

    The LAST non-zero cross-section is the one to trust: the log also contains
    a grid-packing pass that legitimately reports `0 +- 0 pb`, which reads as a
    catastrophic failure and is not.
    """
    found = [(float(v), float(e)) for v, e in _XSEC_RE.findall(text or "")]
    nonzero = [v for v in found if v[0] > 0]
    result: Dict[str, Any] = {
        "cross_sections_pb": [v for v, _ in found],
        "n_cross_section_reports": len(found),
    }
    if nonzero:
        result["cross_section_pb"] = nonzero[-1][0]
        result["cross_section_err_pb"] = nonzero[-1][1]
        result["cross_section_fb"] = nonzero[-1][0] * 1000.0
    return result


# gridpack_generation.sh greps the raw customizecards for this string and
# `exit 1`s on a match -- WITHOUT stripping comments, so merely mentioning it
# in a comment aborts the build. Built from pieces so this module's own source
# is not a false positive when someone greps a checkout.
_FORBIDDEN_CARD_LITERAL = "compute" + "_widths"


def _check_customizecards(cards_dir: str, process_name: str) -> Optional[str]:
    """
    Catch the literal that makes gridpack_generation.sh abort.

    The guard in the script is

        if grep -F "<literal>" $CARDSDIR/${name}_customizecards.dat ; then
            echo ...
            exit 1;

    i.e. a raw substring match over the whole file. A card that only *mentions*
    the command in an explanatory comment is killed the same as one that uses
    it, and the message reads as advice rather than as a fatal error.
    """
    path = os.path.join(cards_dir, f"{process_name}_customizecards.dat")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if _FORBIDDEN_CARD_LITERAL not in text:
        return None
    lines = [str(i + 1) for i, ln in enumerate(text.splitlines())
             if _FORBIDDEN_CARD_LITERAL in ln]
    return (
        f"{os.path.basename(path)} contains the literal "
        f"'{_FORBIDDEN_CARD_LITERAL}' on line(s) {', '.join(lines)}. "
        "gridpack_generation.sh greps the raw file for it and exits 1 -- "
        "comments are NOT stripped, so even mentioning it in a comment aborts "
        "the build after printing what looks like mere advice. Use explicit "
        "widths, or `set param_card decay <pdg> Auto`, and do not name the "
        "command anywhere in the card."
    )


def _verify_build(result: Dict[str, Any]) -> List[str]:
    """
    Decide whether a finished build actually produced anything usable.

    gridpack_generation.sh prints "Gridpack created successfully" and writes a
    tarball even when the run failed partway -- observed when compute_widths
    could not compile its temporary decay process, which left a tarball with no
    process/ directory and no integration at all. So a present tarball is not
    evidence of success; a reported cross-section is.
    """
    problems: List[str] = []
    if result.get("tarball") is None:
        problems.append(result.get("tarball_error", "No tarball was produced."))
        return problems
    if "cross_section_pb" not in result:
        problems.append(
            "BUILD LOOKS FAILED: a tarball exists but the log reports no "
            "non-zero cross-section. gridpack_generation.sh prints 'Gridpack "
            "created successfully' even when integration never ran (e.g. a "
            "compute_widths compilation failure), leaving a tarball with no "
            "process/ directory. Do NOT shower this. Search the log for "
            "'MadGraph5Error', 'Error 1' and 'Fail 5 times'."
        )
    size = result.get("tarball_bytes") or 0
    if size and size < 1_000_000:
        problems.append(
            f"Tarball is only {size} bytes, far below the few-MB floor for a "
            "real gridpack -- likely missing its process directory."
        )
    return problems


def _build_gridpack_script(
    process_name: str,
    rel_carddir: str,
    genprod_mg5_dir: str,
    scram_arch: str,
    queue: str,
    launch_dir: str,
    container: Optional[str],
) -> str:
    """Emit the build script, with the reason for each constraint inline."""
    inner = [
        "#!/bin/bash",
        "# Generated by heptapod GridpackBuildTool -- see skills/cmssw/SKILL.md",
        "set -euo pipefail",
        "",
        "# Scrub any outer Python venv / LCG software view from the environment.",
        "# PYTHONPATH confuses the sandboxed MG5's interpreter, and -- worse --",
        "# an LCG view puts an EL9 toolchain on PATH. Inside the EL8 container",
        "# that gfortran cannot run at all:",
        "#   gfortran: /lib64/libc.so.6: version `GLIBC_2.32' not found",
        "# which MG5 reports only as \"A compilation Error occurs\" on",
        "# StringCast.o and ranmar.o, naming neither the compiler nor the venv.",
        "unset PYTHONPATH PYTHONHOME",
        'if [ -n "${VIRTUAL_ENV:-}" ]; then',
        """  PATH=$(printf %s "$PATH" | tr ':' '\\n' | grep -v "^${VIRTUAL_ENV}" | paste -sd: -)""",
        "  unset VIRTUAL_ENV",
        "fi",
        """PATH=$(printf %s "$PATH" | tr ':' '\\n' | grep -v '^/cvmfs/sft.cern.ch/lcg/' | paste -sd: -)""",
        "export PATH",
        "",
        "source /cvmfs/cms.cern.ch/cmsset_default.sh",
        f"export SCRAM_ARCH={scram_arch}",
        "",
        "# carddir is RELATIVE: gridpack_generation.sh does",
        "#   CARDSDIR=${PRODHOME}/${carddir}",
        "# with no leading-slash check, so an absolute path is concatenated.",
        f"cd {genprod_mg5_dir}",
        f"./gridpack_generation.sh {process_name} {rel_carddir} "
        f"{queue} ALL {scram_arch}",
        "",
    ]
    return "\n".join(inner)


def _build_launch_command(
    script_path: str,
    launch_dir: str,
    container: Optional[str],
) -> str:
    """
    How to invoke the build script.

    The container binds only the cwd, so the `cd` is load-bearing: run from
    anywhere deeper and the model (or genproductions) disappears from inside
    the container with no diagnostic beyond ENOENT.
    """
    if container:
        return f"cd {launch_dir} && {container} --command-to-run bash {script_path}"
    return f"cd {launch_dir} && bash {script_path}"


# ====================================================================== #
# =============================== Tool ================================= #
# ====================================================================== #

class GridpackBuildTool(BaseTool):
    """
    Build a MadGraph gridpack from a cards directory via CMS genproductions,
    handling the path constraints that make the script fail obscurely.

    What it does for you:

      * copies the cards into the genproductions tree if they are not already
        there and passes a RELATIVE carddir, since the script concatenates it
        onto its own directory;
      * computes the directory the container must be launched from -- the
        common ancestor of genproductions and of any absolute model path in the
        proc card -- because only the cwd is bind-mounted;
      * picks the `cmssw-elN` wrapper matching SCRAM_ARCH, and refuses an arch
        it does not recognize rather than guessing;
      * after a build, verifies the tarball exists and reports its size and the
        integrated cross-section, ignoring the `0 +- 0 pb` grid-packing pass
        that looks like a failure and is not.

    Nothing runs unless run_build=True: a gridpack build is long, so the default
    writes the script and returns the command.

    Input:
        process_name: gridpack name, matching the card filename prefix.
        cards_dir: directory holding <process_name>_{proc,run,customize}_card.dat.
        genproductions_dir: clone root, or its bin/MadGraph5_aMCatNLO subdir.
        scram_arch: must match the release the gridpack will be showered in
            (pitfall P6) -- LHE generation is compiled, not portable.
    """

    # ======================== Runtime fields ======================== #
    process_name: Optional[str] = RuntimeField(
        default=None,
        description="Gridpack/process name, e.g. MyModel_pp_xx. Must match the "
        "prefix of the card filenames.",
    )
    cards_dir: Optional[str] = RuntimeField(
        default=None,
        description="Directory containing <process_name>_proc_card.dat and "
        "friends. Copied into the genproductions tree if not already there.",
    )
    genproductions_dir: Optional[str] = RuntimeField(
        default=None,
        description="genproductions clone root, or its "
        "bin/MadGraph5_aMCatNLO subdirectory.",
    )
    scram_arch: str = RuntimeField(
        default="el8_amd64_gcc10",
        description="SCRAM_ARCH to build under; selects the cmssw-elN "
        "container. Must match the showering release.",
    )
    queue: str = RuntimeField(
        default="local",
        description="gridpack_generation.sh queue argument: 'local' runs the "
        "integration here, 'condor' farms it out.",
    )
    build_dir: Optional[str] = RuntimeField(
        default=None,
        description="Where to write the build script. Defaults to "
        "base_directory.",
    )
    run_build: bool = RuntimeField(
        default=False,
        description="Actually run the build (long: tens of minutes to hours). "
        "Default writes the script and returns the command. Named run_build, "
        "not execute, because BaseTool.execute() is how a tool is invoked.",
    )
    verify_log: Optional[str] = RuntimeField(
        default=None,
        description="Path to an existing gridpack_generation log to parse "
        "instead of building -- reports cross-section and tarball only.",
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
        # Verification-only mode: no build, just read a log.
        if self.verify_log:
            return self._verify_only()

        missing = [n for n in ("process_name", "cards_dir", "genproductions_dir")
                   if not getattr(self, n)]
        if missing:
            return self.format_error(
                error="Missing required input",
                reason=f"{', '.join(missing)} must be provided.",
            )

        container = _container_for_arch(self.scram_arch)
        if container is None:
            return self.format_error(
                error="Unrecognized SCRAM_ARCH",
                reason=f"No container known for {self.scram_arch!r}.",
                context=f"Known prefixes: {', '.join(sorted(_ARCH_CONTAINER))}.",
                suggestion="A mismatched arch fails at showering time, not "
                           "build time, so it is not guessed here.",
            )

        genprod = os.path.realpath(self.genproductions_dir)
        genprod_root = genprod
        mg5_dir = os.path.join(genprod, "bin", "MadGraph5_aMCatNLO")
        if not os.path.isdir(mg5_dir):
            # Caller may have passed the MG5 subdir directly; recover the root,
            # which is what has to be visible inside the container.
            if os.path.isfile(os.path.join(genprod, "gridpack_generation.sh")):
                mg5_dir = genprod
                parent = os.path.dirname(os.path.dirname(genprod))
                if os.path.isdir(os.path.join(parent, "Utilities")):
                    genprod_root = parent
            else:
                return self.format_error(
                    error="genproductions not found",
                    reason=f"No gridpack_generation.sh under {genprod!r}.",
                    suggestion="Pass the clone root or its "
                               "bin/MadGraph5_aMCatNLO subdirectory.",
                )

        warnings: List[str] = []

        # --- cards: must end up inside the tree, addressed relatively -----
        cards_src = self.cards_dir
        if not os.path.isabs(cards_src):
            cards_src = os.path.join(self.base_directory, cards_src)
        rel, err = _relative_carddir(cards_src, mg5_dir)
        if err and err != "outside":
            return self.format_error(error="Bad cards directory", reason=err)
        if err == "outside":
            dest = os.path.join(mg5_dir, "cards", self.process_name)
            os.makedirs(dest, exist_ok=True)
            for fname in sorted(os.listdir(cards_src)):
                src_file = os.path.join(cards_src, fname)
                if os.path.isfile(src_file):
                    shutil.copy2(src_file, os.path.join(dest, fname))
            rel = os.path.relpath(dest, mg5_dir)
            warnings.append(
                f"Cards copied into the genproductions tree at {dest} and "
                f"passed as the relative path {rel!r}. The originals in "
                f"{cards_src} remain authoritative -- edit those and re-run, "
                "or the two copies drift."
            )

        # --- customizecards: a literal in here can abort the build --------
        card_error = _check_customizecards(os.path.join(mg5_dir, rel),
                                           self.process_name)
        if card_error:
            return self.format_error(
                error="customizecards would abort the build",
                reason=card_error,
            )

        # --- proc card: does it import a model by absolute path? ----------
        proc_card = os.path.join(mg5_dir, rel,
                                 f"{self.process_name}_proc_card.dat")
        model_path = None
        if os.path.isfile(proc_card):
            with open(proc_card) as fh:
                model_path = _proc_card_model_path(fh.read())
        else:
            warnings.append(
                f"No {os.path.basename(proc_card)} in the cards directory -- "
                "the build will fail unless the name matches process_name."
            )

        # --- where the container must be launched from --------------------
        # The genproductions ROOT, not the MG5 subdir: gridpack_generation.sh
        # resolves its helpers as
        #     script_dir=${PRODHOME%genproductions*}/genproductions/Utilities/scripts
        # and reads MetaData/pdflist_*_run3.dat from the same level. Bind only
        # bin/MadGraph5_aMCatNLO and those vanish, and the $DEFAULT_PDF_SETS
        # substitution quietly falls through to "not using the recommended PDF
        # sets" instead of failing.
        bind_paths = [genprod_root]
        if model_path:
            if not os.path.exists(model_path):
                warnings.append(
                    f"Proc card imports a model at {model_path}, which does "
                    "not exist. MG5 resolves this at code-generation time and "
                    "will fail there."
                )
            bind_paths.append(model_path)
        build_dir = self.build_dir or self.base_directory
        if not os.path.isabs(build_dir):
            build_dir = os.path.join(self.base_directory, build_dir)
        os.makedirs(build_dir, exist_ok=True)
        # The script itself must be readable from inside the container, so
        # where we WRITE it constrains where we can launch from just as much
        # as what it reads.
        bind_paths.append(build_dir)

        launch_dir = _common_launch_dir(bind_paths)
        if model_path:
            warnings.append(
                f"Proc card imports a local model, so the container is "
                f"launched from {launch_dir} -- the common ancestor of "
                "genproductions and the model. Only the cwd is bind-mounted; "
                "launching from either directory alone hides the other."
            )

        script_path = os.path.join(build_dir,
                                   f"build_{self.process_name}_gridpack.sh")
        with open(script_path, "w") as fh:
            fh.write(_build_gridpack_script(
                process_name=self.process_name, rel_carddir=rel,
                genprod_mg5_dir=mg5_dir, scram_arch=self.scram_arch,
                queue=self.queue, launch_dir=launch_dir, container=container))
        os.chmod(script_path, 0o755)

        command = _build_launch_command(script_path, launch_dir, container)
        tarball = os.path.join(
            mg5_dir, f"{self.process_name}_{self.scram_arch}_*_tarball.tar.xz")

        result: Dict[str, Any] = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "script": script_path,
            "launch_dir": launch_dir,
            "container": container,
            "relative_carddir": rel,
            "model_path": model_path,
            "expected_tarball_glob": tarball,
            "command": command,
            "executed": False,
        }

        if self.run_build:
            log_path = os.path.join(build_dir,
                                    f"{self.process_name}_gridpack.log")
            try:
                with open(log_path, "w") as log_fh:
                    completed = subprocess.run(
                        command, shell=True, cwd=launch_dir,
                        stdout=log_fh, stderr=subprocess.STDOUT,
                        timeout=60 * 60 * 12,
                    )
            except subprocess.TimeoutExpired:
                result["executed"] = False
                warnings.append("Build exceeded the 12 h timeout.")
            else:
                result["executed"] = True
                result["returncode"] = completed.returncode
                result["log"] = log_path
                with open(log_path, errors="replace") as fh:
                    result.update(_parse_gridpack_log(fh.read()))
                result.update(self._find_tarball(mg5_dir))
                warnings.extend(_verify_build(result))

        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, indent=2)

    def _find_tarball(self, mg5_dir: str) -> Dict[str, Any]:
        """Locate the produced tarball and report its size."""
        prefix = f"{self.process_name}_{self.scram_arch}_"
        for fname in sorted(os.listdir(mg5_dir)):
            if fname.startswith(prefix) and fname.endswith("_tarball.tar.xz"):
                path = os.path.join(mg5_dir, fname)
                return {"tarball": path,
                        "tarball_bytes": os.path.getsize(path)}
        return {"tarball": None,
                "tarball_error": "No tarball matching "
                                 f"{prefix}*_tarball.tar.xz in {mg5_dir}."}

    def _verify_only(self) -> str:
        path = self.verify_log
        if not os.path.isabs(path):
            path = os.path.join(self.base_directory, path)
        if not os.path.isfile(path):
            return self.format_error(
                error="Log not found", reason=f"{path!r} does not exist.")
        with open(path, errors="replace") as fh:
            parsed = _parse_gridpack_log(fh.read())
        result = {"status": "ok", "schema": SCHEMA_VERSION,
                  "verified_log": path}
        result.update(parsed)
        if "cross_section_pb" not in result:
            result["warning"] = (
                "No non-zero cross-section found. A log reporting only "
                "'0 +- 0 pb' has not completed its pilot run.")
        return json.dumps(result, indent=2)
