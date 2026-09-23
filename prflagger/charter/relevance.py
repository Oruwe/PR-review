"""How close a finding sits to what the repository is for.

Every observation is placed against the charter of *its own* repository — never
a general notion of good code, never another repository's priorities — and
labelled:

  * **core** — it touches what the repository exists to do: its behaviour, its
    public surface, the commands people run;
  * **supporting** — it touches the repository's own code, but not a promise it
    makes to anyone;
  * **peripheral** — tests, docs, examples, tooling: real, but not the product.

The label weights ranking and is shown beside the finding, with the charter
element it maps to and that element's own source. Mechanical facts are never
dropped for being peripheral — a failing test is still a failing test — they
are ordered below the things the repository says it is for.
"""

from __future__ import annotations

from dataclasses import replace

from prflagger.core.models import Charter, Observation

__all__ = ["WEIGHT", "classify", "grounded"]

WEIGHT = {"core": 1.0, "supporting": 0.75, "peripheral": 0.4}

_PERIPHERAL_ROOTS = (
    "tests/", "test/", "testing/", "docs/", "doc/", "examples/", "example/",
    "benchmarks/", "bench/", ".github/", "scripts/", "tools/", "ci/",
)

#: Kinds that describe what the program *does*. A change here is core by nature,
#: whatever file it was observed through.
_BEHAVIOURAL = ("behavior_change", "timeout", "oom", "collection_error")


def _path_of(observation: Observation) -> str:
    ref = observation.evidence_ref or ""
    if "::" in ref:
        return ref.split("::", 1)[0]
    return ref.split(":", 1)[0] if ":" in ref else ref


def _module_for(charter: Charter, symbol: str, path: str) -> tuple[str, str, str] | None:
    """The documented module a symbol or path falls under, most specific first."""
    dotted = path.removesuffix(".py").replace("/", ".") if path else ""
    best: tuple[str, str, str] | None = None
    for module in charter.modules:
        name = module[0]
        if not any(_within(candidate, name) for candidate in (symbol, dotted)):
            continue
        if best is None or len(name) > len(best[0]):
            best = module
    return best


def _within(candidate: str, module: str) -> bool:
    """Whether a dotted name lies inside `module`, allowing a `src.` style prefix."""
    if not candidate:
        return False
    return f".{module}." in f".{candidate}."


def _entry_point_for(charter: Charter, symbol: str) -> str | None:
    for entry in charter.entry_points:
        name, _, target = entry.partition(" = ")
        module = target.split(":", 1)[0]
        if module and symbol and (symbol == module or symbol.startswith(module + ".")
                                  or symbol.replace(":", ".").startswith(module)):
            return name
    return None


def classify(observation: Observation, charter: Charter, *, repo: str) -> tuple[str, str]:
    """(relevance, note) for one observation, judged against `charter` alone.

    Raises if the charter is not this repository's. Judging a finding against
    another repository's memory is the one mistake this function exists to make
    impossible.
    """
    if charter.repo != repo:
        raise ValueError(
            f"refusing to judge a finding on {repo!r} against the charter of "
            f"{charter.repo!r}: a repository is judged only by its own memory"
        )

    path = _path_of(observation)
    module = _module_for(charter, observation.symbol, path)
    entry = _entry_point_for(charter, observation.symbol)
    where = f" — in {module[0]}: “{module[1]}” ({module[2]})" if module else ""
    purpose = (
        f" — {charter.name} is for: “{charter.summary[:90]}”" if charter.summary else ""
    )

    if observation.kind in _BEHAVIOURAL:
        return "core", f"changes what the program does{where or purpose}"

    if observation.kind == "api_change":
        exposed = observation.symbol in charter.public_api
        if entry:
            return "core", f"reached from the `{entry}` command{where}"
        if exposed or observation.what_changed.endswith("was added"):
            return "core", f"part of the public surface {charter.name} exposes{where}"
        return "supporting", f"not in the public surface this charter records{where}"

    if path.startswith(_PERIPHERAL_ROOTS) or observation.symbol.startswith(_PERIPHERAL_ROOTS):
        return "peripheral", f"in {path or observation.symbol}, outside the product itself"

    if entry:
        return "core", f"reached from the `{entry}` command{where}"
    if module:
        return "supporting", f"inside {charter.name}'s own code{where}"
    return "supporting", "inside the repository's own code"


def grounded(
    observations: list[Observation], charter: Charter | None, *, repo: str
) -> list[Observation]:
    """Label every observation with its relevance to this repository's charter.

    With no charter yet — a repository whose memory has not been built — the
    observations pass through unlabelled, and ranking treats them as core. That
    is stated in the run's coverage statement rather than guessed around.
    """
    if charter is None:
        return observations
    out = []
    for observation in observations:
        relevance, note = classify(observation, charter, repo=repo)
        out.append(replace(observation, relevance=relevance, relevance_note=note))
    return out
