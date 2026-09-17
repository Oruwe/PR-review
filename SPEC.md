# PR Flagger — Implementation Spec

Authoritative. Interfaces here are exact. If a signature seems wrong, **stop and say so** —
do not silently change it.

## What this is

Given a PR (base commit → head commit) on a Python repo, produce a report of **facts**:
behavioral changes the PR did not declare, coverage gaps, lint/type regressions, API
changes. Each fact cites evidence. Each fact that reflects a repo standard cites the PRs
where that standard was historically enforced.

Prior art: Testora (arXiv 2503.18597) does differential behavioral testing and classifies
intent from the PR description alone, reporting F1 ≈ 0.59. Our contribution is the **Repo
Brain**: per-repo norms mined from enforced review history, used as the oracle for what
matters.

## Layout

```
prflagger/
  models.py            # C0 — all shared types
  llm.py               # C0 — cached Bedrock client
  sandbox/
    runner.py          # C1
    calibrate.py       # C1
  analysis/
    diff.py            # C2
    symbols.py         # C2
    callgraph.py       # C2
    blast.py           # C2
  characterize/
    generate.py        # C3
    validate.py        # C3
    differential.py    # C4
  brain/
    harvest.py         # C5
    enforce.py         # C6
    norms.py           # C7
    store.py           # C8
  probes/
    coverage.py        # C9
    lint.py            # C9
    api_diff.py        # C9
  report/
    rank.py            # C10
    render.py          # C10
  cli.py               # C11
tests/
  fixtures/toypkg/     # synthetic package with known call structure
```

## Build order

C0 → C1 → C2 → C3 → C4 → **checkpoint: submittable** → C5 → C6 → C7 → C8 → C9 → C10 → C11

Do not start C5 until C4's acceptance passes. C0–C4 is a complete submission on its own.

---

## C0 — models.py and llm.py

### models.py

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

@dataclass(frozen=True)
class Symbol:
    fqn: str          # "pkg.module.Class.method"
    kind: str         # "function" | "method" | "class"
    file: str         # repo-relative posix path
    line_start: int
    line_end: int

class Outcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INSTALL_FAILED = "install_failed"
    TIMEOUT = "timeout"
    COLLECTION_ERROR = "collection_error"
    OOM = "oom"

@dataclass(frozen=True)
class Job:
    repo_path: str
    commit: str
    image_key: str
    command: tuple[str, ...]
    timeout_s: int = 120
    memory_mb: int = 512

    @property
    def idempotency_key(self) -> str:
        """sha256 of the canonical JSON of all fields. Stable across runs."""

@dataclass(frozen=True)
class TestResult:
    outcome: Outcome
    per_test: dict[str, str]     # pytest nodeid -> "passed"|"failed"|"error"
    duration_s: float
    peak_rss_mb: int | None
    stdout: str
    stderr: str

@dataclass(frozen=True)
class Norm:
    id: str                      # kebab-case slug
    statement: str               # imperative, one line
    scope: str                   # "repo" | "project" | "org"
    support: int
    distinct_reviewers: int
    confidence: float            # 0.0-1.0
    evidence_prs: tuple[int, ...]

@dataclass(frozen=True)
class Finding:
    kind: str                    # "behavior_change"|"coverage_gap"|"lint_regression"|
                                 # "api_change"|"timeout"|"oom"
    symbol: str                  # fqn
    what_changed: str
    how_we_know: str             # nodeid, number, or diff hunk
    norm: Norm | None            # None permitted ONLY when kind == "behavior_change"
    confidence: float
    severity: float              # 0.0-1.0, from probe type

    def __post_init__(self) -> None:
        """Raise ValueError if norm is None and kind != 'behavior_change'."""
```

### llm.py

```python
Provider = Callable[[str, int, float], str]   # prompt, max_tokens, temperature -> text

def complete(prompt: str, *, model: str, max_tokens: int = 4096,
             temperature: float = 0.0, provider: Provider | None = None) -> str:
    """Content-addressed cache at .cache/llm/<sha256>.json. On a cache hit the provider
    is never called. Records token counts to .cache/llm/usage.jsonl.
    provider defaults to providers.bedrock_provider()."""

def embed(texts: list[str]) -> list[list[float]]:
    """sentence-transformers all-MiniLM-L6-v2, local. Cached the same way."""
```

`providers.py` holds `bedrock_provider()`. Keeping the provider injectable means C0 can be
built and tested before AWS credentials exist, and swapping models later is one line.

**Note on the no-mock rule:** it bans mocking Docker, git and the target repo — the things
this system reasons about. Stubbing the *provider boundary* to test caching is intended.

**Acceptance (C0):** `pytest tests/test_models.py` — `Job.idempotency_key` is stable across
two constructions with equal fields and differs when any field differs; `Finding` with
`norm=None` and `kind="coverage_gap"` raises `ValueError`. Calling `complete()` twice with
the same prompt performs exactly one network call (assert via a monkeypatched counter on the
boto3 client, not on `complete` itself).

---

## C1 — sandbox/runner.py, sandbox/calibrate.py

```python
def build_image(repo_path: Path, image_key: str) -> str:
    """Build (or reuse) a Docker image with the repo's dependencies installed.
    image_key = sha256 of the lockfile set. Returns the image tag."""

def run_job(job: Job) -> TestResult:
    """Execute job in a container. Never raises for job failure."""

def calibrate(repo_path: Path, commit: str) -> tuple[int, int]:
    """Run the suite once unconstrained. Returns (memory_mb, timeout_s) =
    (2 x peak RSS, 3 x wall time). Persist to .cache/calibration.json."""
```

`run_job` must invoke exactly:

```
docker run --rm --network=none --memory={mb}m --memory-swap={mb}m
  --pids-limit=256 --cpus=1 --read-only --tmpfs /tmp:rw,size=256m
  --user 1000:1000 --security-opt no-new-privileges --cap-drop=ALL
  -v {worktree}:/src:ro {image} timeout {t} pytest --json-report ...
```

Outcome mapping: exit 0 → `PASSED`; exit 1 with a parseable report → `FAILED`; exit 124 →
`TIMEOUT`; exit 137 → `OOM`; pytest exit 2 → `COLLECTION_ERROR`; image build failure →
`INSTALL_FAILED`.

Worktrees: one bare clone under `.cache/repos/<slug>.git`, `git worktree add` per commit.
Never re-clone. Never share a worktree between concurrent jobs.

**Acceptance (C1):** against the configured target repo — (a) running the suite at HEAD
returns `PASSED` with `len(per_test) > 0`; (b) a job whose command is
`python -c "while True: pass"` returns `TIMEOUT` within `timeout_s + 10`; (c) calling
`run_job` twice with an identical `Job` runs Docker once (second is a cache hit).

---

## C2 — analysis/

```python
def changed_ranges(repo: Path, base: str, head: str) -> dict[str, list[tuple[int, int]]]
    """repo-relative file path -> changed line ranges in the HEAD revision."""

def symbols_in_file(path: Path, module_fqn: str) -> list[Symbol]
    """Parse with ast. Nested functions get dotted fqns. Decorators do not create symbols."""

def build_call_graph(package_root: Path) -> dict[str, set[str]]
    """caller fqn -> set of callee fqns. Name-based resolution: resolve imports where
    possible, fall back to matching on the bare attribute/function name.
    False positives acceptable; false negatives are not."""

def blast_radius(repo: Path, base: str, head: str, *, hops: int = 2) -> list[Symbol]
    """Symbols overlapping changed ranges, plus their transitive callers up to `hops`."""
```

**Acceptance (C2):** `tests/fixtures/toypkg/` is a synthetic package you create with a known
call structure (`a()` calls `b()` calls `c()`, plus an unrelated `d()`). Asserts:
`build_call_graph` returns exactly the expected edge set; `blast_radius` on a commit touching
`c()` returns `{c, b, a}` and excludes `d`. Then a smoke assert on the real target repo:
`blast_radius` on any real commit returns a non-empty list in under 30 seconds.

---

## C3 — characterize/

```python
def generate_tests(symbol: Symbol, source: str, *, n: int = 8) -> list[str]
    """LLM-generate pytest functions recording CURRENT behavior of `symbol`.
    Returns source strings, one test function each."""

def validate_on_base(tests: list[str], repo: Path, base_sha: str,
                     *, max_attempts: int = 3) -> tuple[list[str], float]
    """Run tests against base. Keep only those that PASS. Regenerate failures,
    feeding the failure output back. Returns (surviving_tests, discard_rate)."""
```

Generation rules are in the `characterization` skill — read it before implementing.

Discard rate must be logged per run to `.cache/metrics.jsonl`. It is a headline number for
the writeup.

**Acceptance (C3):** pick one pure function in the target repo with no I/O. `generate_tests`
returns ≥ 5 syntactically valid test functions (assert via `ast.parse`). After
`validate_on_base`, ≥ 1 survives and `discard_rate` is recorded. A deliberately wrong test
(asserting a value the function does not return) is discarded.

---

## C4 — characterize/differential.py

```python
def differential(repo: Path, base: str, head: str) -> list[Finding]
    """Full pipeline: blast radius -> generate -> validate on base -> run survivors on
    head -> emit a Finding per test that passed on base and failed on head."""

def declared_scope(pr_title: str, pr_body: str) -> list[str]
    """LLM extraction of the symbols/behaviors the PR claims to change."""
```

A `Finding` from `differential` has `kind="behavior_change"`, `how_we_know` = the pytest
nodeid plus the assertion diff, and `norm=None` until C10 attaches one. Findings whose symbol
appears in `declared_scope` get `confidence *= 0.5` and are marked declared.

**Acceptance (C4):** on seeded PR #2 (see below), `differential` returns ≥ 1 Finding whose
symbol is the one the seeded bug touches. On seeded PR #1 (honest bugfix), it returns zero
undeclared findings.

### Seeded PRs

Create three branches in your fork of the target repo:

| Branch | Content |
|---|---|
| `seed/honest-fix` | A real bugfix, PR body accurately describes it |
| `seed/silent-edge` | Fixes the stated bug **and** changes an edge case (empty input, None, boundary) without mentioning it |
| `seed/untested-api` | Adds a new public function with no test and no docstring |

---

## C5 — brain/harvest.py

```python
def harvest(repo_slug: str, *, limit: int = 150) -> Path
    """Fetch merged PRs via `gh` CLI: metadata, review comments, commits with timestamps.
    Write to .cache/brain/<slug>/prs.json. Never re-fetch what is cached."""
```

Use `gh api` with pagination. Store raw responses unmodified — filtering happens in C6.

**Acceptance (C5):** after one run, `prs.json` contains ≥ 100 PRs and ≥ 1 with a non-empty
review comment list. A second `harvest` call makes **zero** network requests (assert with a
counter around the subprocess call).

---

## C6 — brain/enforce.py

```python
def enforced_comments(prs_json: Path) -> list[dict]
    """Keep a review comment only if (a) a commit on that PR has a timestamp AFTER the
    comment's, and (b) the PR merged. Returns dicts with:
    pr_number, reviewer_login, body, diff_hunk, created_at."""
```

This filter separates standards that were *enforced* from opinions that were ignored. It is
the single most important step in the Brain — do not relax it.

**Acceptance (C6):** output is a strict subset of all review comments; every kept comment has
at least one later commit on its PR; comments on unmerged PRs are absent. Log the retention
rate.

---

## C7 — brain/norms.py

```python
def cluster_norms(comments: list[dict], *, min_support: int = 3,
                  min_reviewers: int = 2) -> list[Norm]
    """Embed comment bodies, cluster by cosine similarity (threshold 0.75, agglomerative),
    then for each cluster of size >= min_support ask the LLM for one imperative statement.
    Drop clusters below either threshold. evidence_prs = the cluster's PR numbers."""
```

Batch LLM naming: one call per cluster, not per comment. `confidence = min(1.0, support / 10)
* min(1.0, distinct_reviewers / 4)`.

Write `repo_profile.json`: `{repo, generated_at, prs_analyzed, norms: [...], declared: {...}}`
where `declared` holds linter/type-checker config detected from the repo.

**Acceptance (C7):** on the target repo, produces ≥ 3 norms each with `support >= 3`,
`distinct_reviewers >= 2`, and non-empty `evidence_prs`. Print them; a human sanity check that
they are specific rather than generic is a gate on proceeding.

---

## C8 — brain/store.py

```python
class KnowledgeStore(Protocol):
    def put_symbols(self, repo: str, symbols: list[Symbol]) -> None: ...
    def put_call_edges(self, repo: str, edges: dict[str, set[str]]) -> None: ...
    def put_norms(self, repo: str, norms: list[Norm]) -> None: ...
    def callers_of(self, repo: str, fqn: str, hops: int) -> list[Symbol]: ...
    def norms_for(self, repo: str) -> list[Norm]: ...
    def match_norm(self, repo: str, text: str, k: int = 3) -> list[tuple[Norm, float]]: ...

class SqliteGraphStore(KnowledgeStore):
    """NetworkX in memory, SQLite for persistence, numpy array for embeddings."""
```

`Norm.scope` exists and is persisted even though only `"repo"` is used now. Do not remove it
— it is what makes org-level inheritance a query change rather than a migration.

**Acceptance (C8):** write then read back symbols, edges and norms across a process restart.
`callers_of` returns the same set as `blast_radius`'s graph traversal for the toy fixture.
`match_norm("needs a test for this")` ranks a test-related norm first.

---

## C9 — probes/

```python
def coverage_delta(repo: Path, base: str, head: str) -> list[Finding]
    """coverage.py over changed lines. Finding per changed symbol with 0 covered lines."""

def lint_regression(repo: Path, base: str, head: str) -> list[Finding]
    """Run the repo's OWN ruff/mypy config at both commits. Finding per new error."""

def api_diff(repo: Path, base: str, head: str) -> list[Finding]
    """AST diff of public symbols (no leading underscore). Findings for added, removed,
    and signature-changed."""
```

Severity: `behavior_change` 1.0, `api_change` 0.8, `coverage_gap` 0.5, `lint_regression` 0.3.

**Acceptance (C9):** on `seed/untested-api`, `coverage_delta` returns a Finding for the new
function and `api_diff` reports one added public symbol.

---

## C10 — report/

```python
def rank(findings: list[Finding], norms: list[Norm],
         store: KnowledgeStore) -> list[Finding]
    """Attach the best-matching norm to each finding via store.match_norm (drop the match
    below similarity 0.6). Then sort by:
    score = confidence * severity * centrality * scope_weight
    centrality = normalised caller count of finding.symbol; scope_weight: repo 1.0,
    project 0.8, org 0.6. No LLM in this function."""

def render(findings: list[Finding], profile: dict, out: Path) -> None
    """Jinja2 -> single self-contained HTML file. Every finding shows all four contract
    fields. Norm citations render as 'This repo required this in #412, #457, #490'.
    A findings list containing an invalid Finding must raise, not silently skip."""
```

The report must also render a **coverage statement**: which modules were verified, which were
skipped, and why. Never present partial verification as complete.

**Acceptance (C10):** `rank` is deterministic — same input, same order, twice. `render`
produces an HTML file that opens standalone with no network requests, containing all three
seeded PRs' findings.

---

## C11 — cli.py

```
prflagger brain build --repo <slug>      # C5-C8
prflagger check --repo <path> --base <sha> --head <sha> --out report.html
prflagger norms --repo <slug>            # print learned norms with evidence
```

**Acceptance (C11):** `prflagger check` on each seeded branch produces the expected report
end to end, from a clean `.cache/`, in under 10 minutes.

---

## Out of scope — do not build

Cross-repo blast radius. Org/project norm scopes beyond the persisted field. Incremental
brain updates. Norm decay. Languages other than Python. A web server. Authentication. A
queue or worker pool beyond `asyncio.Semaphore`. Neo4j. Any vector database.
