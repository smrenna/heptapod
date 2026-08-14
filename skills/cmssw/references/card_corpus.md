# The genproductions card corpus — check it before inferring a convention

Almost every question of the form *"what value does CMS actually use for X?"*
is answered by a working example in
[`cms-gen/genproductions_cards`](https://gitlab.cern.ch/cms-gen/genproductions_cards),
the repository of production cards and GEN fragments for central MC requests.
It is ~20 MB and reading it is far cheaper than rediscovering a convention by
failed gridpack build.

**Use it as ground truth, not as a rulebook.** It is a record of what was
merged, which includes deprecated patterns alongside current ones. The way to
read a convention off it is to *count* — a pattern in 237 fragments and its
alternative in 73 tells you which is current and that the other still works.
Several rules stated in the repo's own `AGENTS.md` are contradicted by the
files sitting next to it, so verify against the tree, not the prose.

```bash
git clone ssh://git@gitlab.cern.ch:7999/cms-gen/genproductions_cards.git
```

Needs a CERN account with an SSH key registered on CERN GitLab. There is no
public mirror; the older, partly-overlapping
[`cms-sw/genproductions`](https://github.com/cms-sw/genproductions) on GitHub
is public and still holds the *scripts* (`gridpack_generation.sh`,
`Utilities/`, `MetaData/`) plus a large set of legacy cards.

## Layout

```
<Generator>/production/<Energy>/<Process>/<Process>_{proc,run,customize,extramodels}_card.dat
genfragments/production/<Energy>/<Process>_fragment/Hadronizer_..._cff.py
```

Generators present: `MadGraph5_aMCatNLO` (by far the largest — 98 process
directories at 13.6 TeV alone), `Powheg`, `Sherpa`, `JHUGen`, `MCFM`,
`Herwig`, `Phantom`, plus the UPC set (`gammaUPC`, `Starlight`, `Superchic`,
`UPCgen`). Energies: `13p6TeV` (Run 3), `13TeV` (Run 2), `14TeV`, and the
heavy-ion/reference points `5TeV`, `5p36TeV`, `PbPb5p36TeV`, `OO5p36TeV`,
`pO9p62TeV`.

Note that cards and fragments live in **separate trees** and are linked only
by naming convention, so finding one does not hand you the other — search
both.

Two caveats on paths: `MadGraph5_aMCatNLO/examples/` holds tutorial/reference
cards, not production ones. And the `bin/` prefix in the old
`cms-sw/genproductions` layout is *not* used here — a path from a
`cms-sw/genproductions` doc will not resolve.

## Searching it

Find whether a process already exists before building a private model:

```bash
# by physics content
ls MadGraph5_aMCatNLO/production/13p6TeV/ | grep -i <keyword>
grep -rl "<particle_label>\|<pdg_code>" MadGraph5_aMCatNLO/ genfragments/

# what UFO models are actually in use, and where they're published
cat $(find . -name "*_extramodels.dat") | sort -u

# what a convention's real distribution is, before writing it down as a rule
grep -rho "Configuration.Generator.MCTunes[A-Za-z0-9._]*" genfragments/ | sort | uniq -c
```

That last form is the important habit. Every campaign-convention claim in
`pitfalls.md` P8/P9 was established this way and is re-checkable in one line.

## What it settled (as of 2026-08)

- **Published UFO models.** `_extramodels.dat` entries are filenames under
  `https://cms-project-generators.web.cern.ch/cms-project-generators/`
  (P1). Collecting them across the corpus gives the list of models you can use
  *without* building or hosting anything, including BSM models well beyond the
  handful that ship with MG5. Check this before concluding a model needs
  FeynRules.
- **BSM resonance declaration** — the `id:all` + `oneChannel`/`addChannel`
  pattern, with field-by-field meaning, is in P4.
- **Campaign boilerplate** — hadronizer class, tune module, PS weights,
  `comEnergy`, `ebeam`: P8.
- **`lhaid` is a substituted placeholder** in Run 3 cards, not a number: P9.

## Why to look first

A worked example from this repo's own history, stated generically. A request
came in for a leptoquark decaying to a specific quark–lepton pair. The
reasoning went: that pair's total charge is inconsistent with the charge of
the leptoquark under discussion, therefore the process is forbidden, therefore
it needs a new model built with FeynRules. The first step was right, the
conclusion was wrong.

The charge argument only rules out the *particular multiplet* being
considered. A different multiplet — in that case a weak triplet with a
component carrying the required charge, rather than a singlet — accommodates
the same final state fine. And a card set for it was already merged in the
corpus, against a UFO already published to the CMS generators area. The
detour cost a day.

Two habits follow:

- A quantum-number argument tells you *which multiplet you need*. It never
  tells you whether someone has already carded it. Those are separate
  questions and the second one is answered by `grep`, not by reasoning.
- "The model I need does not exist" is a claim about a repository, so check
  the repository. Search both `genproductions_cards` and the published-model
  list in the aggregated `_extramodels.dat` entries before building anything.

When you do find an existing card set, read its `customizecards.dat` closely:
that is usually where the physics choice you care about lives — which
generation couples, at what strength, with what mass and width — while the
proc and run cards are comparatively boilerplate.
