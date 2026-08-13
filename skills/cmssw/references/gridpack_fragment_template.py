# Configuration/GenProduction/python/<process_name>_gridpack_cff.py
#
# CMSSW GEN fragment for showering a gridpack (any UFO model). Adapted from
# the CMSDAS generator exercise's Fragments/gridpack_cff.py, generalized per
# skills/cmssw/SKILL.md pitfalls 1-3.

import FWCore.ParameterSet.Config as cms

from Configuration.Generator.Pythia8CommonSettings_cfi import *
from Configuration.Generator.MCTunes2017.PythiaCP5Settings_cfi import *

# --- Point at the gridpack ---------------------------------------------
# Pick ONE of the two scripts below (pitfall 2 in SKILL.md). The names are
# backwards from what you'd guess: `_cvmfs` takes a plain filesystem path,
# while the un-suffixed script can ONLY fetch through the frontier smallfiles
# service and cannot read local disk at all.

# (a) Gridpack on any mounted filesystem — local scratch OR /cvmfs. This is
#     the normal case for a model you just built yourself, and yes, it is the
#     `_cvmfs` script even when the tarball is not on /cvmfs:
externalLHEProducer = cms.EDProducer('ExternalLHEProducer',
  scriptName = cms.FileInPath("GeneratorInterface/LHEInterface/data/run_generic_tarball_cvmfs.sh"),
  outputFile = cms.string("cmsgrid_final.lhe"),
  numberOfParameters = cms.uint32(1),
  args = cms.vstring('/absolute/path/to/<process_name>_gridpack.tar.xz'),
  nEvents = cms.untracked.uint32(10000)
)

# (b) Gridpack published to the CMS frontier smallfiles repository. Note this
#     script takes TWO location arguments (repo, then filename), so
#     numberOfParameters must be 2 — passing a single path here makes
#     ExternalLHEProducer's appended nEvents get read as the filename:
# externalLHEProducer = cms.EDProducer('ExternalLHEProducer',
#   scriptName = cms.FileInPath("GeneratorInterface/LHEInterface/data/run_generic_tarball.sh"),
#   outputFile = cms.string("cmsgrid_final.lhe"),
#   numberOfParameters = cms.uint32(2),
#   args = cms.vstring('<repo>', '<published_tarball.tar.xz>'),
#   nEvents = cms.untracked.uint32(10000)
# )

# --- Shower ---------------------------------------------------------------
generator = cms.EDFilter("Pythia8HadronizerFilter",
    maxEventsToPrint = cms.untracked.int32(1),
    pythiaPylistVerbosity = cms.untracked.int32(1),
    filterEfficiency = cms.untracked.double(1.0),
    pythiaHepMCVerbosity = cms.untracked.bool(False),
    comEnergy = cms.double(13000.),
    # !! If you end up with NOTHING uncommented inside processParameters --
    # !! which is the common case for a gridpack whose BSM states are already
    # !! decayed in the matrix element -- you must DELETE the block entirely
    # !! AND remove 'processParameters' from parameterSets below. An empty
    # !! vstring that is still named in parameterSets SEGFAULTS cmsRun in
    # !! gen::ParameterCollector::const_iterator::next(), via
    # !! Py8InterfaceBase::readSettings() at beginLuminosityBlockProduce.
    # !! There is no exception and no error message -- just
    # !! "A fatal system signal has occurred: segmentation violation" and a
    # !! ~250-byte ROOT stub. Leaving the block out is fine; leaving it empty
    # !! is not.
    PythiaParameters = cms.PSet(
        pythia8CommonSettingsBlock,
        pythia8CP5SettingsBlock,
        processParameters = cms.vstring(
             # 'PartonLevel:MPI = off',            # sanity-check knob, not for production
             #
             # If the UFO model leaves a BSM state stable on purpose (an
             # NLSP-style decay handled outside MG5), paste the .cmnd lines
             # from heptapod's BuildDecayTableTool output here — same Pythia8
             # command syntax, just wrapped as vstring entries, e.g.:
             # '1000022:onMode = off',
             # '1000022:addChannel = 1 1.0 0 22 1000039',
             #
             # If generating with an extra jet at LO (proc card's
             # `add process ... j`), uncomment the MLM matching block instead
             # of the lines above — see references in the tt1j_mlm example
             # and pitfalls.md for what happens if you skip this.
             # 'JetMatching:setMad = off',
             # 'JetMatching:scheme = 1',
             # 'JetMatching:merge = on',
             # 'JetMatching:jetAlgorithm = 2',
             # 'JetMatching:etaJetMax = 5.',
             # 'JetMatching:coneRadius = 1.',
             # 'JetMatching:slowJetPower = 1',
             # 'JetMatching:qCut = 50.',            # tune to your process's pT scale
             # 'JetMatching:nQmatch = 5',
             # 'JetMatching:nJetMax = 1',            # = max extra partons in the proc card
             # 'JetMatching:doShowerKt = off',
        ),
        parameterSets = cms.vstring('pythia8CommonSettings',
                                    'pythia8CP5Settings',
                                    'processParameters',
                                    )
    )
)

# --- Validation (pitfall 3) -------------------------------------------
# Only wire this in if a matching Rivet routine exists for your process.
# Otherwise, drop this block and validate the LHE with heptapod's own
# LHEToJSONL -> EventJSONLToNumpy -> kinematics tool chain instead — it
# doesn't require a routine to exist and is much cheaper to iterate on.
#
# from GeneratorInterface.RivetInterface.rivetAnalyzer_cfi import rivetAnalyzer
# rivetAnalyzer.AnalysisNames = cms.vstring(
#     'MC_XS', 'MC_FSPARTICLES',   # generic, model-agnostic building blocks
# )
# rivetAnalyzer.OutputFile = cms.string("<process_name>_gridpack.yoda")
# ProductionFilterSequence = cms.Sequence(generator*rivetAnalyzer)

ProductionFilterSequence = cms.Sequence(generator)
