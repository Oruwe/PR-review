"""Calls into leaf through a `from ... import` — exercises import resolution."""

from toypkg.leaf import c


def b(value: int) -> int:
    return c(value) + 1
