"""The run state machine.

Transitions are declared rather than implied by control flow, so an illegal one
is caught where it happens instead of showing up later as a run that reports a
stage it never reached.
"""

from __future__ import annotations

from prflagger.core.models import TERMINAL_STATES, RunState

__all__ = ["ORDER", "can_transition", "is_terminal", "progress_of"]

#: The happy path, in order. Stages may be skipped (a run with no LLM budget
#: goes straight from PROBING to RENDERING) but never revisited.
ORDER: tuple[RunState, ...] = (
    RunState.QUEUED,
    RunState.PREPARING,
    RunState.IMAGE_BUILD,
    RunState.BASE_RUN,
    RunState.HEAD_RUN,
    RunState.PROBING,
    RunState.ADJUDICATING,
    RunState.RENDERING,
    RunState.DONE,
)

_INDEX = {state: position for position, state in enumerate(ORDER)}


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def can_transition(current: RunState, target: RunState) -> bool:
    """Forward along ORDER, or into a terminal state. Never backwards."""
    if is_terminal(current):
        return False
    if target in (RunState.FAILED, RunState.CANCELLED):
        return True
    if current == RunState.QUEUED and target == RunState.PREPARING:
        return True
    here, there = _INDEX.get(current), _INDEX.get(target)
    if here is None or there is None:
        return False
    return there > here


def progress_of(state: RunState) -> float:
    """0.0-1.0, for the progress bar. Terminal states read as complete."""
    if state is RunState.DONE:
        return 1.0
    if state in (RunState.FAILED, RunState.CANCELLED):
        return 1.0
    position = _INDEX.get(state)
    return 0.0 if position is None else position / (len(ORDER) - 1)
