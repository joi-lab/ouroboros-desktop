"""Null-agent integrity probe for the GAIA harness-comparison rig.

A zero-capability agent: it takes NO actions and returns an empty answer. Per the
Berkeley-RDI "trustworthy benchmarks" guidance, a null agent MUST score ~0. If it
scores above ~0, the rig/scorer is leaking credit (e.g. answers reachable, scorer
bug) and the comparison is invalid. Run this as a control alongside every real run.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

try:
    from inspect_ai.solver import Generate, TaskState, solver
except Exception:  # pragma: no cover
    Generate = Any  # type: ignore
    TaskState = Any  # type: ignore

    def solver(fn):  # type: ignore
        return fn


@solver
def null_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if getattr(state, "output", None) is None:
            state.output = SimpleNamespace(completion="")
        state.output.completion = ""  # no action, no answer
        return state

    return solve
