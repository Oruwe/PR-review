# PR Flagger

A PR verification agent. It does not review code or give opinions: it runs mechanical
probes and reports facts, each citing evidence.

`SPEC.md` is the authoritative design. `CLAUDE.md` holds the non-negotiable rules.

## Running it

```bash
pytest tests/ -q                 # the full suite, against the real target repo
ruff check . && mypy prflagger/  # lint and types

python -m scripts.seed_target    # create the three seeded branches
python -m prflagger.cli check --repo <checkout> --base <sha> --head <sha> --out report.html
python -m prflagger.cli brain build --repo pallets/click
python -m prflagger.cli norms --repo pallets/click
```

The target repository is configured in `config.toml`.

## The interface

`check` writes two artifacts. `report.html` is the static report — the thing you attach
to a review. `index.html` is the interface: one self-contained HTML file, no backend, no
CDN, no build step, which renders from the findings document at `.cache/ui/findings.json`.

```bash
python -m prflagger.cli check --repo <checkout> --base <sha> --head <sha> \
    --json .cache/ui/findings.json --ui index.html
python -m prflagger.cli ui --data .cache/ui/findings.json --out index.html  # re-render only

python -m scripts.collect_showcase   # build the document from real runs on the target
```

It has three zones and one panel:

- **the pipeline**, drawn as a node graph carrying each component's observed status, the
  count it emitted and the time it took — including the components that could not run;
- **the flags**, ranked, each expanding into the four-field contract: what changed, how
  we know, which repo standard makes it matter, and the confidence;
- **the analysed pull requests**, each opening onto the structural account of the change
  (from the blast radius and the diff, not a summary), its sandbox runs and its flags;
- **the validation loop**, which plays back what characterization really did: candidates
  generated, all of them run against base, the ones that failed on base struck through
  and discarded, regeneration with the real failure output, survivors run against head,
  and the one that goes red. The discard rate on it is measured, not illustrative.

Every sandbox run in the interface can be replayed from its recorded per-line arrival
times (`prflagger/sandbox/record.py` performs the identical C1 invocation and timestamps
each line as it arrives), or read in full, searchable, with failures anchored so a
finding links straight to the line that produced it.

`index.html` in this repository is generated output, committed so it can be opened
without running the pipeline first. The interface never issues a verdict: no approve, no
reject, no score. Where something could not be measured, it says so on the page.

## What it needs

| Capability | Used for | How it is configured |
|---|---|---|
| Docker | every sandboxed run (C1 onward) | a running daemon; `.claude/hooks/session-start.sh` starts one |
| A Bedrock model | test generation, scope extraction, norm naming | boto3's normal credential chain, or `AWS_BEARER_TOKEN_BEDROCK` |
| `all-MiniLM-L6-v2` | norm clustering and `match_norm` | downloaded by `sentence-transformers` on first use |
| `gh` CLI | harvesting merged pull requests | an authenticated `gh` on PATH |

**No credential is ever read from, or written to, this repository.** Configure them the
way your machine normally does — environment variables or `~/.aws/`.

Every one of these degrades explicitly rather than silently: a run that cannot reach a
model still produces a report, and that report names what it could not verify.
