"""Loop optimizer — orchestrates the trial/error cycle for thinking loops.

The Phase 10 optimizer is the conductor that ties the other Phase 10
pieces together:

* :class:`meta_agent.MetaAgent` proposes loop variants for a task type.
* :class:`loops.critic.LoopCritic` scores each variant's output.
* :class:`loops.assembler.LoopAssembler` actually executes the loops
  (when an executor is wired in).
* :class:`loops.trial_store.TrialStore` persists every immutable
  trial record and derives the leaderboard.

The optimizer itself is just the orchestrator.  It owns no
long-running state beyond a handle to the four collaborators above.

The optimizer exposes three public methods:

* :meth:`LoopOptimizer.run_optimization_cycle` — propose + execute +
  score + record ``n_trials`` variants of a base loop, return the
  resulting trials.
* :meth:`LoopOptimizer.select_for_task` — read the leaderboard and
  return the best-known loop for a task type.  Falls back to a
  premade loop when the leaderboard has no data.
* :meth:`LoopOptimizer.get_leaderboard` — wrap :class:`TrialStore` so
  callers don't have to import the store directly.

Why an orchestrator class at all?  The four pieces above are useful
on their own (and exposed for testing), but the *cycle* —
propose→execute→score→record→select—is one unit of work that the
Main Agent, the conductor, and the dashboard all want to invoke
without reimplementing the wiring.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from loops.assembler import LoopAssembler, LoopGraph
from loops.base_loop import LoopResult
from loops.critic import LoopCritic
from loops.primitives import LoopPrimitive, PrimitiveType
from loops.trial_store import (
    DEFAULT_MIN_TRIALS,
    LeaderboardEntry,
    Trial,
    TrialStore,
)
from meta_agent import MetaAgent, LoopVariant

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OptimizationConfig:
    """Tunables for a single :meth:`LoopOptimizer.run_optimization_cycle` call.

    All fields are optional and have sensible defaults; the orchestrator
    also exposes its own default instance at :data:`DEFAULT_OPT_CONFIG`.
    """

    n_trials: int = 3
    """How many variants to try in this cycle."""

    base_loop: str = "reflection"
    """Premade loop to mutate (empty string lets the meta-agent pick)."""

    task_type: str = "general"
    """Tag used for the leaderboard filter."""

    task_sample: str = ""
    """One concrete task to hand to the meta-agent (empty = heuristic)."""

    task_type_to_base: dict[str, str] | None = None
    """Override the meta-agent's task-type → base-loop mapping."""

    include_builtins: bool = True
    """If True, also trial the unmodified premade loop alongside variants."""


DEFAULT_OPT_CONFIG: OptimizationConfig = OptimizationConfig()


# ---------------------------------------------------------------------------
# Pydantic result types
# ---------------------------------------------------------------------------


class CycleReport(BaseModel):
    """A summary of one :meth:`LoopOptimizer.run_optimization_cycle` call.

    Attributes:
        cycle_id: Stable UUID4 for the cycle.
        task_type: Task type the cycle targeted.
        base_loop: Premade loop that was mutated.
        trials: Immutable trial records the cycle produced.
        best_loop_id: ``loop_id`` of the highest-composite trial, or
            ``None`` if the cycle produced no trials.
        best_score: The corresponding composite score (or ``0.0``).
    """

    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    task_type: str
    base_loop: str
    trials: list[Trial]
    best_loop_id: str | None = None
    best_score: float = 0.0


# ---------------------------------------------------------------------------
# Executor protocol — the optimizer is executor-agnostic.
# ---------------------------------------------------------------------------


@runtime_checkable
class _LoopExecutor(Protocol):
    """Anything that can take a :class:`LoopGraph` + task and run it.

    The contract is intentionally minimal.  Production code passes the
    :class:`loops.assembler.LoopAssembler` (which exposes
    :meth:`LoopAssembler.execute_graph` with the right shape); tests
    pass any callable that returns a :class:`LoopResult` synchronously
    or asynchronously.
    """

    async def __call__(  # pragma: no cover — protocol
        self, graph: LoopGraph, task: str, *, loop_id: str
    ) -> LoopResult: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_executor(
    assembler: LoopAssembler,
    model_client: Any,
) -> _LoopExecutor:
    """Wrap an assembler + model client in the executor protocol.

    The protocol method takes a :class:`LoopGraph`, a task, and a
    pre-computed ``loop_id`` so the resulting :class:`LoopResult` can
    be stored under the same id the meta-agent chose.  The assembler
    ignores our pre-computed id (it uses ``graph.loop_id`` instead) so
    we set the id on the graph right before execution.
    """

    async def _run(graph: LoopGraph, task: str, *, loop_id: str) -> LoopResult:
        stamped = graph.model_copy(update={"loop_id": loop_id})
        return await assembler.execute_graph(
            stamped,
            task=task,
            preamble={"intent": {"goal": task}, "permissions": {}},
            model_client=model_client,
        )

    return _run


def _stub_executor() -> _LoopExecutor:
    """Offline executor used when the caller did not wire an assembler.

    The stub produces a deterministic :class:`LoopResult` whose quality
    scales with the number of nodes in the graph.  This keeps the
    trial/error cycle honest in tests without standing up a real LLM.
    """

    async def _run(graph: LoopGraph, task: str, *, loop_id: str) -> LoopResult:
        node_count = max(1, len(graph.nodes))
        # Reward generate nodes; critique/revise get a small boost.
        gen_count = sum(1 for n in graph.nodes if n.primitive == PrimitiveType.GENERATE)
        crit_count = sum(
            1
            for n in graph.nodes
            if n.primitive in {PrimitiveType.CRITIQUE, PrimitiveType.REVISE}
        )
        base_conf = min(0.95, 0.5 + 0.05 * gen_count + 0.05 * crit_count)
        tokens = 200 * node_count
        cost = 0.0002 * node_count
        latency = 50 * node_count
        output = (
            f"[stub-loop:{loop_id}] Handled task of {len(task or '')} chars with "
            f"{node_count} nodes ({gen_count} generate, {crit_count} critique/revise)."
        )
        return LoopResult(
            output=output,
            confidence=base_conf,
            tokens_used=tokens,
            cost_usd=cost,
            latency_ms=latency,
            iterations=node_count,
            intermediate_outputs=[
                {
                    "node_id": n.node_id,
                    "primitive": n.primitive.value,
                    "output": f"stub-{n.node_id}",
                    "score": base_conf,
                    "tokens_used": 200,
                    "cost_usd": 0.0002,
                    "latency_ms": 50.0,
                }
                for n in graph.nodes
            ],
        )

    return _run


def _find_node(graph: LoopGraph, primitive: PrimitiveType) -> LoopPrimitive | None:
    """Return the first node of the given primitive type, or ``None``."""
    for n in graph.nodes:
        if n.primitive == primitive:
            return n
    return None


def _variant_to_graph(variant: LoopVariant) -> LoopGraph:
    """Re-stamp a variant's id so the trial store sees stable ids."""
    return variant.graph.model_copy(update={"loop_id": variant.loop_id})


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class LoopOptimizer:
    """Propose, execute, score, record, and select thinking-loop variants.

    Args:
        assembler: The :class:`loops.assembler.LoopAssembler` used when
            the optimizer needs to *execute* a graph.  ``None`` selects
            the offline stub executor, which returns synthetic but
            deterministic results.
        critic: The :class:`loops.critic.LoopCritic` used to score
            each trial.  ``None`` builds one with a heuristic backend
            (no LLM required).
        meta_agent: The :class:`meta_agent.MetaAgent` used to propose
            variants.  ``None`` builds a default one with no LLM.
        trial_store: The :class:`loops.trial_store.TrialStore` to write
            trials to.  ``None`` uses an in-memory store.
        rng: An optional :class:`random.Random` instance for the
            "explore" half of the cycle.  Tests pass a seeded RNG for
            determinism.
    """

    def __init__(
        self,
        *,
        assembler: LoopAssembler | None = None,
        critic: LoopCritic | None = None,
        meta_agent: MetaAgent | None = None,
        trial_store: TrialStore | None = None,
        model_client: Any = None,
        rng: random.Random | None = None,
    ) -> None:
        self._assembler = assembler or LoopAssembler()
        self._critic = critic or LoopCritic(model=None)
        # Wire the meta-agent's assembler so its ``assemble_builtin``
        # path uses the same instance the executor does.
        self._meta_agent = meta_agent or MetaAgent(
            llm=None, assembler=self._assembler
        )
        self._store = trial_store or TrialStore()
        self._rng = rng or random.Random()
        # ``model_client`` is optional; when ``None`` the executor
        # falls back to the offline stub so the trial/error cycle is
        # testable without an LLM.
        self._model_client = model_client
        self._executor: _LoopExecutor = (
            _default_executor(self._assembler, self._model_client)
            if self._model_client is not None
            else _stub_executor()
        )

    # ------------------------------------------------------------------
    # Public configuration knobs
    # ------------------------------------------------------------------

    def set_executor(self, executor: _LoopExecutor | None) -> None:
        """Replace the executor (pass ``None`` to use the offline stub)."""
        self._executor = executor or _stub_executor()

    def set_model_client(self, model_client: Any) -> None:
        """Wire a real :class:`loops.model_router.LLMClient` to the executor.

        After this call, the cycle will execute the proposed loops
        against a real LLM.  Pass ``None`` to revert to the offline
        stub executor.
        """
        self._model_client = model_client
        if model_client is None:
            self._executor = _stub_executor()
        else:
            self._executor = _default_executor(self._assembler, model_client)

    @property
    def trial_store(self) -> TrialStore:
        """The store the optimizer writes trials to."""
        return self._store

    @property
    def critic(self) -> LoopCritic:
        """The critic the optimizer uses to score trials."""
        return self._critic

    @property
    def meta_agent(self) -> MetaAgent:
        """The meta-agent the optimizer uses to propose variants."""
        return self._meta_agent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_optimization_cycle(
        self,
        task_type: str,
        task_sample: str,
        n_trials: int = 3,
        *,
        base_loop: str = "reflection",
        include_builtins: bool = True,
    ) -> CycleReport:
        """Run one trial/error cycle for ``task_type``.

        Args:
            task_type: Tag (e.g. ``"code_review"``).
            task_sample: One concrete task to ground the proposals.
            n_trials: How many variants to try (defaults to 3).
            base_loop: Premade loop to mutate; empty lets the
                meta-agent pick a base from the task-type mapping.
            include_builtins: If True, the first trial is the
                unmodified premade loop; the remaining trials are
                meta-agent variants.

        Returns:
            A :class:`CycleReport` summarising the cycle.
        """
        n = max(1, int(n_trials))
        cycle_id = str(uuid.uuid4())
        base_id = (base_loop or self._meta_agent._pick_base_for_task(task_type)).lower()

        # 1) Build the variant catalogue.
        candidates: list[tuple[str, LoopGraph, str]] = []
        # The unmodified premade loop (cheap baseline).
        if include_builtins:
            base_graph = self._assembler.assemble_builtin(base_id)
            candidates.append((f"{base_id}-baseline", base_graph, "baseline"))
        # Variants from the meta-agent.
        # We request one more than we need so we can drop any that
        # collide with the baseline id.
        variants_needed = max(1, n - (1 if include_builtins else 0))
        seen_ids: set[str] = {c[0] for c in candidates}
        for _ in range(variants_needed + 2):
            try:
                variant = await self._meta_agent.propose_variant(
                    base_loop_id=base_id,
                    task_type=task_type,
                    task_sample=task_sample,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("meta-agent failed to propose variant: %s", exc)
                continue
            v_graph = _variant_to_graph(variant)
            if variant.loop_id in seen_ids:
                continue
            seen_ids.add(variant.loop_id)
            candidates.append((variant.loop_id, v_graph, variant.modification))
            if len(candidates) >= n:
                break

        # 2) Execute + score + record.
        trials: list[Trial] = []
        # Pre-fetch the recent feedback for the meta-agent's next call
        # — we don't actually need it here (each trial is independent),
        # but threading it through makes the cycle chainable.
        recent = await self.aget_recent_feedback(task_type=task_type, limit=5)
        for loop_id, graph, modification in candidates[:n]:
            try:
                result = await self._executor(
                    graph, task_sample or task_type, loop_id=loop_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("executor raised for %s: %s", loop_id, exc)
                # Synthesise a failure result so the trial is still
                # recorded and the optimizer doesn't blow up.
                result = LoopResult(
                    output=f"[executor error] {exc}",
                    confidence=0.0,
                    tokens_used=0,
                    cost_usd=0.001,
                    latency_ms=1.0,
                    iterations=0,
                    intermediate_outputs=[],
                )
            score = await self._critic.score(
                task=task_sample or task_type,
                output=result.output,
                expected=None,
                task_type=task_type,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                loop_id=loop_id,
            )
            trial_id = await self._store.arecord_trial(
                loop_id=loop_id,
                task_type=task_type,
                loop_graph=graph.to_dict(),
                score=score,
                result=result,
                task_preview=task_sample[:200] if task_sample else "",
            )
            # Re-fetch the trial to attach the trial_id for the report.
            stored = await self._store.aget_trials(loop_id=loop_id, limit=1)
            trials.append(stored[0] if stored else _make_trial(trial_id, loop_id, task_type, graph, score, result, task_sample))

        # 3) Compute the report.
        best_id: str | None = None
        best_score = 0.0
        for t in trials:
            s = t.score.composite_score
            if s > best_score:
                best_score = s
                best_id = t.loop_id
        return CycleReport(
            cycle_id=cycle_id,
            task_type=task_type,
            base_loop=base_id,
            trials=trials,
            best_loop_id=best_id,
            best_score=best_score,
        )

    async def select_for_task(
        self,
        task_type: str,
        *,
        min_trials: int = DEFAULT_MIN_TRIALS,
    ) -> LoopGraph:
        """Pick the best-known loop for ``task_type``.

        Selection rules:

        1. If the leaderboard has any entry for ``task_type`` with
           ``trial_count >= min_trials``, return the highest-ranked
           entry's ``best_variant`` deserialised as a :class:`LoopGraph`.
        2. Else, if the global leaderboard has an entry with enough
           trials, return that.
        3. Else, fall back to the meta-agent's recommended premade
           loop and return its graph.

        Args:
            task_type: Tag to look up.
            min_trials: Minimum evidence threshold (defaults to
                :data:`loops.trial_store.DEFAULT_MIN_TRIALS`).

        Returns:
            A :class:`LoopGraph` ready to be executed.
        """
        entries = await self._store.aget_leaderboard(
            task_type=task_type, sort_by="score", min_trials=min_trials
        )
        if not entries:
            entries = await self._store.aget_leaderboard(
                task_type=None, sort_by="score", min_trials=min_trials
            )
        if entries:
            return self._entry_to_graph(entries[0])
        # Fallback: premade loop.
        base_id = self._meta_agent._pick_base_for_task(task_type)
        return self._assembler.assemble_builtin(base_id)

    async def get_leaderboard(
        self,
        task_type: str | None = None,
        sort_by: str = "score",
        min_trials: int = DEFAULT_MIN_TRIALS,
    ) -> list[LeaderboardEntry]:
        """Return the leaderboard (delegates to :class:`TrialStore`)."""
        return await self._store.aget_leaderboard(
            task_type=task_type, sort_by=sort_by, min_trials=min_trials  # type: ignore[arg-type]
        )

    async def arecord_trial(
        self,
        loop_id: str,
        task_type: str | None,
        graph: LoopGraph,
        result: LoopResult,
        score: Any | None = None,
        task_sample: str = "",
    ) -> Trial:
        """Record a single trial (used by callers that already executed).

        Args:
            loop_id: Loop graph id.
            task_type: Optional task-type tag.
            graph: The Pydantic :class:`LoopGraph` that was executed.
            result: The :class:`LoopResult` from the executor.
            score: A pre-computed :class:`loops.critic.CriticScore`; if
                ``None``, the optimizer's critic scores the output.
            task_sample: Optional task text for the leaderboard preview.

        Returns:
            The :class:`Trial` as stored.
        """
        if score is None:
            score = await self._critic.score(
                task=task_sample or task_type or loop_id,
                output=result.output,
                task_type=task_type,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                loop_id=loop_id,
            )
        trial_id = await self._store.arecord_trial(
            loop_id=loop_id,
            task_type=task_type,
            loop_graph=graph.to_dict(),
            score=score,
            result=result,
            task_preview=task_sample[:200] if task_sample else "",
        )
        stored = await self._store.aget_trials(loop_id=loop_id, limit=1)
        if stored:
            return stored[0]
        return _make_trial(
            trial_id, loop_id, task_type, graph, score, result, task_sample
        )

    async def aget_recent_feedback(
        self, task_type: str, limit: int = 5
    ) -> list[Any]:
        """Return the most-recent :class:`CriticScore` rows for ``task_type``.

        Used by the meta-agent to bias the next proposal.  Returns an
        empty list if the store has no trials yet.
        """
        try:
            trials = await self._store.aget_trials(task_type=task_type, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recent feedback lookup failed: %s", exc)
            return []
        return [t.score for t in trials]

    # ------------------------------------------------------------------
    # Sync wrappers — useful for non-async call sites (CLI tools, scripts)
    # ------------------------------------------------------------------

    def run_optimization_cycle_sync(self, *args: Any, **kwargs: Any) -> CycleReport:
        """Sync wrapper around :meth:`run_optimization_cycle`."""
        return asyncio.run(self.run_optimization_cycle(*args, **kwargs))

    def select_for_task_sync(
        self, task_type: str, *, min_trials: int = DEFAULT_MIN_TRIALS
    ) -> LoopGraph:
        """Sync wrapper around :meth:`select_for_task`."""
        return asyncio.run(self.select_for_task(task_type, min_trials=min_trials))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _entry_to_graph(self, entry: LeaderboardEntry) -> LoopGraph:
        """Deserialise a leaderboard entry's ``best_variant`` blob."""
        if entry.best_variant:
            try:
                return LoopGraph.from_dict(entry.best_variant)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "could not deserialise best_variant for %s: %s",
                    entry.loop_id, exc,
                )
        # No serialised graph → assemble a fresh copy of the premade
        # loop the leaderboard id points to.  Best-effort.
        for base in ("reflection", "cot", "tree", "debate", "ensemble", "direct"):
            if base in entry.loop_id:
                return self._assembler.assemble_builtin(base)
        return self._assembler.assemble_builtin("reflection")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trial(
    trial_id: str,
    loop_id: str,
    task_type: str | None,
    graph: LoopGraph,
    score: Any,
    result: LoopResult,
    task_sample: str,
) -> Trial:
    """Build a :class:`Trial` from in-memory pieces (best-effort)."""
    from datetime import datetime, timezone

    return Trial(
        trial_id=trial_id,
        loop_id=loop_id,
        task_type=task_type,
        loop_graph=graph.to_dict(),
        score=score,
        result=result,
        timestamp=datetime.now(timezone.utc),
        task_preview=(task_sample or "")[:200],
        output_preview=(result.output or "")[:200],
    )


__all__ = [
    "CycleReport",
    "DEFAULT_OPT_CONFIG",
    "LoopOptimizer",
    "OptimizationConfig",
]
