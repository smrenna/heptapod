# Configuration/GenProduction/python/<process_name>_gridpack_cff.py
#
# CMSSW GEN fragment for showering a gridpack (any UFO model). Adapted from
# the CMSDAS generator exercise's Fragments/gridpack_cff.py, generalized per
# skills/cmssw/SKILL.md pitfalls 1-3, then reconciled against the ~300 real
# fragments in `cms-gen/genproductions_cards` (see references/card_corpus.md).
#
# The boilerplate below (hadronizer class, tune import, PSweights) is
# CAMPAIGN-SPECIFIC and shown for Run 3 / 13.6 TeV, which is the majority
# convention in the corpus. For Run 2 / 13 TeV see pitfall P8.

import FWCore.ParameterSet.Config as cms

from Configuration.Generator.Pythia8CommonSettings_cfi import *
from Configuration.Generator.PSweightsPythia.PythiaPSweightsSettings_cfi import *
# Run 3 @ 13.6 TeV. For Run 2 @ 13 TeV this is MCTunes2017.PythiaCP5Settings_cfi
# instead -- the two are NOT interchangeable (P8).
from Configuration.Generator.MCTunesRun3ECM13p6TeV.PythiaCP5Settings_cfi import *

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
# Pythia8ConcurrentHadronizerFilter is the current default (209 of 224 corpus
# fragments); the non-concurrent Pythia8HadronizerFilter is the legacy class,
# still correct but single-threaded. See P8.
generator = cms.EDFilter("Pythia8ConcurrentHadronizerFilter",
    maxEventsToPrint = cms.untracked.int32(1),
    pythiaPylistVerbosity = cms.untracked.int32(1),
    filterEfficiency = cms.untracked.double(1.0),
    pythiaHepMCVerbosity = cms.untracked.bool(False),
    comEnergy = cms.double(13600.),          # 13000. for Run 2
    # !! If you end up with NOTHING uncommented inside processParameters --
    # !! which happens when the BSM states are already decayed in the matrix
    # !! element and no PDG code collides -- you must DELETE the block entirely
    # !! AND remove 'processParameters' from parameterSets below. An empty
    # !! vstring that is still named in parameterSets SEGFAULTS cmsRun in
    # !! gen::ParameterCollector::const_iterator::next(), via
    # !! Py8InterfaceBase::readSettings() at beginLuminosityBlockProduce.
    # !! There is no exception and no error message -- just
    # !! "A fatal system signal has occurred: segmentation violation" and a
    # !! ~250-byte ROOT stub. Leaving the block out is fine; leaving it empty
    # !! is not. (P7)
    PythiaParameters = cms.PSet(
        pythia8CommonSettingsBlock,
        pythia8CP5SettingsBlock,
        pythia8PSweightsSettingsBlock,
        processParameters = cms.vstring(
             # 'PartonLevel:MPI = off',            # sanity-check knob, not for production
             #
             # -- Declaring a BSM resonance to Pythia8 (P4) --------------
             # If a BSM state reaches Pythia8 undecayed -- either left stable
             # in the LHE on purpose, or a pair-production process where the
             # decay is done downstream -- declare it and its channels here.
             # The convention is a full particle-data line plus explicit
             # channels. Example: a colour-triplet scalar of charge -1/3 and
             # mass 1000 GeV decaying to u e-.
             #
             # '9000001:all = Xs Xs~ 1 -1 3 1000.0 2.0 970.0 1030.0 9.86635e-14',
             # '9000001:oneChannel = 2 1.0 101 2 11',      # -> u  e-
             # '9000001:addChannel = 3 1.0 101 -2 -11',    # -> u~ e+
             # '9000001:mayDecay = on',
             # '9000001:isResonance = on',
             #
             # `id:all` fields, in order:
             #   name antiName spinType chargeType colType m0 mWidth mMin mMax tau0
             #     spinType   2S+1  (1 = scalar, 2 = fermion, 3 = vector)
             #     chargeType charge in units of e/3 (-1 above == -1/3)
             #     colType    0 singlet, 3 triplet, -3 antitriplet, 8 octet
             #     mMin/mMax  Breit-Wigner truncation; convention m0 -+ 15*width
             #     tau0       proper lifetime in mm = hbar*c / width
             #                with hbar*c = 1.97327e-13 mm*GeV
             #     PDG code   9000001 upward, so it cannot collide with an SM
             #                particle (P5)
             # `oneChannel`/`addChannel` fields: onMode bRatio meMode products...
             #   onMode 1 = on, 2 = particle only, 3 = antiparticle only
             #   meMode 101 = phase space, no matrix element
             # Use oneChannel for the FIRST channel (it clears the existing
             # table) and addChannel for each subsequent one.
             #
             # heptapod's BuildDecayTableTool emits the same .cmnd syntax from
             # a parent PDG id, mass, and product list -- paste its output here.
             #
             # -- MLM matching (P3) --------------------------------------
             # If generating with an extra jet at LO (proc card's
             # `add process ... j`), uncomment this block instead. qCut must
             # track the run card's xqcut; the corpus baseline is qCut ~ 1.5*xqcut.
             # 'JetMatching:setMad = off',
             # 'JetMatching:scheme = 1',
             # 'JetMatching:merge = on',
             # 'JetMatching:jetAlgorithm = 2',
             # 'JetMatching:etaJetMax = 5.',
             # 'JetMatching:coneRadius = 1.',
             # 'JetMatching:slowJetPower = 1',
             # 'JetMatching:qCut = 50.',            # ~1.5 * run_card xqcut
             # 'JetMatching:nQmatch = 5',           # = run_card maxjetflavor
             # 'JetMatching:nJetMax = 1',           # = max extra partons in the proc card
             # 'JetMatching:doShowerKt = off',
        ),
        parameterSets = cms.vstring('pythia8CommonSettings',
                                    'pythia8CP5Settings',
                                    'pythia8PSweightsSettings',
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

# If the sample only makes sense with a given state present, the corpus pattern
# is a PythiaFilter appended to the sequence rather than a cut in the run card:
#
# bsmFilter = cms.EDFilter("PythiaFilter", ParticleID = cms.untracked.int32(9000001))
# ProductionFilterSequence = cms.Sequence(generator * bsmFilter)
