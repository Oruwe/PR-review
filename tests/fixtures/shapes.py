"""Shapes for symbols_in_file: nesting, methods, decorators, async.

Deliberately outside toypkg so it cannot perturb the call graph's expected edge set.
"""

import functools


def plain(value):
    return value


@functools.cache
def decorated(value):
    return value


class Holder:
    def method(self, value):
        def nested(inner):
            return inner

        return nested(value)

    @property
    def prop(self):
        return 1

    class Inner:
        def deep(self):
            return 2


async def coroutine(value):
    return value
