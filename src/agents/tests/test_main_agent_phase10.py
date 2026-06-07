"""Phase 10 Main Agent integration tests.

The Main Agent gains a ``loop_optimizer`` kwarg and a
``select_thinking_loop`` method.  These tests cover the wiring without
needing a live LLM.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make ``src`` importable without spinning up the full kernel.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agents.main_agent import MainAgent  # noqa: E402
from loop_optimizer import LoopOptimizer  # noqa: E402
from loops.assembler import LoopGraph  # noqa: E402
from loops.trial_store import TrialStore  # noqa: E402


def _make_agent(*, loop_optimizer=None) -> MainAgent:
    """Build a MainAgent without going through the full manifest init."""
    agent = MainAgent.__new__(MainAgent)
    agent._llm = None
    agent._loop_optimizer = loop_optimizer
    return agent


class TestSelectThinkingLoop:
    """``MainAgent.select_thinking_loop`` returns a LoopGraph."""

    @pytest.mark.asyncio
    async def test_no_optimizer_falls_back_to_premade(self):
        agent = _make_agent()
        graph = await agent.select_thinking_loop("code_review")
        assert isinstance(graph, LoopGraph)
        # Without an optimizer we assemble a premade loop; the default
        # base for "code_review" is reflection.
        assert graph.loop_id == "reflection"

    @pytest.mark.asyncio
    async def test_no_optimizer_math_picks_cot(self):
        agent = _make_agent()
        graph = await agent.select_thinking_loop("math")
        assert graph.loop_id == "cot"

    @pytest.mark.asyncio
    async def test_optimizer_wired(self):
        opt = LoopOptimizer(trial_store=TrialStore())
        agent = _make_agent(loop_optimizer=opt)
        graph = await agent.select_thinking_loop("code_review", min_trials=1)
        # With an optimizer and no trials, falls back to premade loop.
        assert isinstance(graph, LoopGraph)

    @pytest.mark.asyncio
    async def test_set_loop_optimizer_wires_later(self):
        agent = _make_agent()
        opt = LoopOptimizer(trial_store=TrialStore())
        agent.set_loop_optimizer(opt)
        assert agent.loop_optimizer is opt
        graph = await agent.select_thinking_loop("general")
        assert isinstance(graph, LoopGraph)

    @pytest.mark.asyncio
    async def test_optimizer_exception_falls_back_safely(self):
        class FailingOptimizer:
            async def select_for_task(self, *a, **k):
                raise RuntimeError("nope")

        agent = _make_agent(loop_optimizer=FailingOptimizer())
        graph = await agent.select_thinking_loop("code_review")
        # Must return a premade loop even when the optimizer raises.
        assert isinstance(graph, LoopGraph)


class TestLoopOptimizerProperty:
    """The ``loop_optimizer`` property reflects the constructor kwarg."""

    def test_default_is_none(self):
        agent = _make_agent()
        assert agent.loop_optimizer is None

    def test_set_then_get(self):
        agent = _make_agent()
        opt = LoopOptimizer(trial_store=TrialStore())
        agent.set_loop_optimizer(opt)
        assert agent.loop_optimizer is opt
