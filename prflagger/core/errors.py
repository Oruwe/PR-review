"""The exception vocabulary.

Every failure this system can have is one of these, and each says what could
not be verified rather than just that something went wrong. A run that hits one
still produces a report — the report names the gap.
"""

from __future__ import annotations

__all__ = [
    "BudgetExceeded",
    "ImageBuildError",
    "PrflaggerError",
    "ProviderUnavailable",
    "RepoUnavailable",
    "ToolchainUndetected",
    "UncitedJudgement",
]


class PrflaggerError(RuntimeError):
    """Base for everything this system raises deliberately."""


class BudgetExceeded(PrflaggerError):
    """A model call would have pushed spend past a configured cap.

    Carries the numbers so the report can say exactly what was refused and why,
    instead of a run silently producing less than it claims.
    """

    def __init__(self, scope: str, spent: float, cap: float, would_add: float) -> None:
        super().__init__(
            f"{scope} budget exhausted: ${spent:.4f} spent of ${cap:.2f} cap; "
            f"this call would add about ${would_add:.4f}"
        )
        self.scope = scope
        self.spent = spent
        self.cap = cap
        self.would_add = would_add


class ProviderUnavailable(PrflaggerError):
    """No model provider could be reached or authenticated."""


class ImageBuildError(PrflaggerError):
    """A sandbox image failed to build. Becomes `Outcome.INSTALL_FAILED`."""

    def __init__(self, tag: str, stdout: str, stderr: str) -> None:
        super().__init__(f"image build failed: {tag}")
        self.tag = tag
        self.stdout = stdout
        self.stderr = stderr


class RepoUnavailable(PrflaggerError):
    """A repository could not be cloned, fetched, or read."""


class ToolchainUndetected(PrflaggerError):
    """No toolchain pack claimed this repo and none was configured."""


class UncitedJudgement(PrflaggerError):
    """An adjudication came back with no citation, or one that does not resolve.

    Raised by the citation gate. The observation still ships — bare, with the
    report saying adjudication was rejected and naming the reason.
    """
