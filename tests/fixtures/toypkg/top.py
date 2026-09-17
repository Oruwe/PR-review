"""Calls into middle. Two hops above leaf."""

from toypkg.middle import b


def a(value: int) -> int:
    return b(value)
