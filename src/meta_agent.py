"""The meta-agent — proposes thinking-loop variants for a task type.

The meta-agent is the *outer* layer above the loops package.  Given a
task type (``code_review``, ``summarisation``, ``planning``, …) and a
sample task, it produces a :class:`loops.assembler.LoopGraph` that the
:mod:`src.loop_optimizer` can then trial.  The proposal is a
*modification* of an existing premade loop — never a from-scratch
graph — so the search space stays manageable and every variant is
guaranteed to be a valid DAG.

The meta-agent never executes loops; it only designs them.  The
optimiser is the one that runs trials, scores them, and feeds the
results back as :class:`loops.critic.CriticScore` objects.

Why an LLM here at all?  Heuristic templates (``"code → reflection"``,
``"design → tree"``) get us 80% of the way, but the last 20% is
tying loop structure to the *specific* task sample.  The meta-agent
asks an LLM to nudge a base graph by adding a ``critique`` step,
swapping to ``cot`` for math-heavy tasks, raising the branch count
on a ``tree`` for ambiguous tasks, etc.

When the LLM is unavailable (no key, no provider, or
``MetaAgent(llm=None)``) the meta-agent falls back to deterministic
mutations: it picks from a small library of canned variants
(``reflection+critique``, ``tree+vote``, …) by hashing the task type.
This keeps the trial/error cycle honest in tests and offline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from loops.assembler import LoopAssembler, LoopEdge, LoopGraph
from loops.primitives import LoopPrimitive, PrimitiveType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic result type
# ---------------------------------------------------------------------------


class LoopVariant(BaseModel):
    """A proposed loop configuration produced by the meta-agent.

    Attributes:
        loop_id: Stable identifier for the variant.  The meta-agent
            derives a deterministic-ish name from the base loop and the
            task type so the same proposal gets the same id across
            cycles.
        base_loop_id: The premade loop this was mutated from.
        task_type: Task-type tag (echoed back from
            :meth:`MetaAgent.propose_variant`).
        graph: The proposed :class:`LoopGraph`.
        modification: One-line description of the change (filled in
            by the LLM when available, or by the heuristic otherwise).
        rationale: Longer natural-language explanation.
    """

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    base_loop_id: str
    task_type: str
    graph: LoopGraph
    modification: str = ""
    rationale: str = ""


# ---------------------------------------------------------------------------
# LLM-client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _JsonGenClient(Protocol):
    """Minimum LLM-client surface the meta-agent needs.

    Compatible with ``loops.model_router.LLMClient`` and
    ``agents.llm_client.LLMClient``.  The meta-agent only ever calls
    :meth:`generate` and only ever asks for JSON output, so the
    protocol is intentionally narrow.
    """

    async def generate(  # pragma: no cover — protocol
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> Any:
        ...


# ---------------------------------------------------------------------------
# Deterministic heuristic library
#
# Each entry is ``(modification_label, mutator_fn)`` where the mutator
# is given the base graph (a Pydantic :class:`LoopGraph`) and returns
# a new graph.  Used both by the offline fallback and by the LLM
# path (the LLM picks one of these by name).
# ---------------------------------------------------------------------------


#: Default premade loop used as the starting point for every task
#: type the LLM has not been trained on.
DEFAULT_BASE_LOOP: str = "reflection"


#: Mapping from task type → which premade loop is the natural base.
#: Mirrors the Phase 4 ``MetaAgentStub`` heuristics; expanded slightly
#: to cover the new task types Phase 10 cares about.
TASK_TYPE_TO_BASE: dict[str, str] = {
    "code": "reflection",
    "code_review": "reflection",
    "review": "reflection",
    "bug_fix": "reflection",
    "debugging": "reflection",
    "refactor": "reflection",
    "writing": "reflection",
    "edit": "reflection",
    "summarisation": "reflection",
    "summary": "reflection",
    "math": "cot",
    "logic": "cot",
    "research": "cot",
    "analysis": "cot",
    "design": "tree",
    "architecture": "tree",
    "planning": "tree",
    "brainstorm": "tree",
    "decision": "debate",
    "controversial": "debate",
    "tradeoff": "debate",
    "general": "reflection",
}


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class MetaAgent:
    """Proposes loop variants for trial.

    Args:
        llm: Any LLM client implementing the ``.generate`` shape above.
            ``None`` disables the LLM path and routes every proposal
            through the deterministic heuristic library.
        assembler: A :class:`loops.assembler.LoopAssembler` used to
            build the base graphs.  Defaults to a fresh assembler.
        model_id: Optional model identifier to embed in the
            ``model_override`` of generated nodes.  The LLM is told
            which model produced a variant so the trial records can
            include provenance.
    """

    #: Catalog of canned mutations the heuristic path (and the LLM
    #: path, by name) can apply.  Each entry is ``(label, fn)``.
    MUTATIONS: list[tuple[str, Any]] = []

    def __init__(
        self,
        llm: _JsonGenClient | None = None,
        *,
        assembler: LoopAssembler | None = None,
        model_id: str | None = None,
    ) -> None:
        self._llm = llm
        self._assembler = assembler or LoopAssembler()
        self._model_id = model_id
        # Populate the mutation catalog the first time the class is
        # instantiated; doing it in __init__ rather than at class
        # definition keeps the functions bound to the instance (and
        # means subclasses that override the catalog get it right).
        if not self.MUTATIONS:
            self.MUTATIONS.extend(_default_mutations())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def propose_variant(
        self,
        base_loop_id: str,
        task_type: str,
        task_sample: str,
        recent_feedback: Iterable[Any] | None = None,
    ) -> LoopVariant:
        """Generate a modified loop config for ``task_type``.

        Args:
            base_loop_id: Premade loop to mutate (``direct``, ``cot``,
                ``reflection``, ``tree``, ``debate``, ``ensemble``).
            task_type: Tag (e.g. ``"code_review"``).  Used both to pick
                the natural base loop (when ``base_loop_id`` is empty)
                and to make the proposed ``loop_id`` stable.
            task_sample: One representative task — gives the LLM
                something concrete to reason about.
            recent_feedback: Optional list of recent
                :class:`loops.critic.CriticScore` objects.  The LLM
                uses these to bias its proposal toward fixing
                recurring failures.

        Returns:
            A validated :class:`LoopVariant`.  Two consecutive calls
            with the same arguments and a working LLM are not
            guaranteed to return the same variant — that's by design,
            the optimizer needs diversity to explore — but the LLM
            is steered toward *meaningful* variation rather than
            random noise.
        """
        base_id = (base_loop_id or self._pick_base_for_task(task_type)).lower()
        if base_id not in {"direct", "cot", "reflection", "tree", "debate", "ensemble"}:
            base_id = DEFAULT_BASE_LOOP
        base_graph = self._assembler.assemble_builtin(base_id)

        feedback_list = list(recent_feedback or [])

        if self._llm is None or not task_sample.strip():
            return self._heuristic_variant(
                base_id=base_id,
                base_graph=base_graph,
                task_type=task_type,
                task_sample=task_sample,
                feedback=feedback_list,
            )

        try:
            return await self._llm_variant(
                base_id=base_id,
                base_graph=base_graph,
                task_type=task_type,
                task_sample=task_sample,
                feedback=feedback_list,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta-agent LLM proposal failed (%s); falling back", exc)
            return self._heuristic_variant(
                base_id=base_id,
                base_graph=base_graph,
                task_type=task_type,
                task_sample=task_sample,
                feedback=feedback_list,
            )

    async def reflect_on_trial(
        self,
        trial: Any,
        score: Any,
    ) -> str:
        """Generate a natural-language explanation of a trial result.

        Args:
            trial: A :class:`loops.trial_store.Trial` (or anything
                with a ``loop_id`` and ``output_preview``).
            score: A :class:`loops.critic.CriticScore`.

        Returns:
            A short string ("the loop scored 8.2 because ...").  When
            the LLM is unavailable, the agent returns a deterministic
            explanation derived from the score's quality + criteria.
        """
        loop_id = getattr(trial, "loop_id", "<unknown>")
        quality = float(getattr(score, "quality_score", 0.0))
        reasoning = getattr(score, "reasoning", "") or ""
        if self._llm is None or not reasoning:
            return (
                f"Loop {loop_id} scored {quality:.1f}/10. "
                f"Composite={getattr(score, 'composite_score', 0.0):.3f}."
            )
        try:
            response = await self._llm.generate(
                system=(
                    "You are a short, plain-spoken analyst.  Summarise "
                    "why a trial got its score.  Two sentences max."
                ),
                user=(
                    f"Trial loop_id={loop_id}\n"
                    f"Quality score: {quality:.1f}/10\n"
                    f"Critic reasoning: {reasoning}\n\n"
                    "Summarise in 1-2 sentences."
                ),
                json_mode=False,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta-agent reflect_on_trial LLM call failed: %s", exc)
            return (
                f"Loop {loop_id} scored {quality:.1f}/10. "
                f"Critic said: {reasoning[:120]}"
            )
        content = getattr(response, "content", None) or getattr(response, "text", "")
        return str(content).strip() or (
            f"Loop {loop_id} scored {quality:.1f}/10."
        )

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    async def _llm_variant(
        self,
        *,
        base_id: str,
        base_graph: LoopGraph,
        task_type: str,
        task_sample: str,
        feedback: list[Any],
    ) -> LoopVariant:
        """Ask the LLM to pick a mutation and apply it."""
        prompt = self._build_proposal_prompt(
            base_id=base_id,
            base_graph=base_graph,
            task_type=task_type,
            task_sample=task_sample,
            feedback=feedback,
        )
        response = await self._llm.generate(
            system=(
                "You design thinking-loop graphs for AI agents.  Respond "
                "with a JSON object only.  Apply at most one mutation "
                "from the catalog; never invent new node types."
            ),
            user=prompt,
            json_mode=True,
            temperature=0.7,
        )
        content = getattr(response, "content", None) or getattr(response, "text", "")
        data = self._parse_proposal(content)
        mutation_name = data.get("mutation") or "noop"
        rationale = data.get("rationale") or ""

        mutated, modification = self._apply_mutation(base_graph, mutation_name)
        loop_id = self._variant_id(base_id, task_type, mutation_name)
        return LoopVariant(
            loop_id=loop_id,
            base_loop_id=base_id,
            task_type=task_type,
            graph=mutated,
            modification=modification,
            rationale=rationale,
        )

    def _build_proposal_prompt(
        self,
        *,
        base_id: str,
        base_graph: LoopGraph,
        task_type: str,
        task_sample: str,
        feedback: list[Any],
    ) -> str:
        catalog_lines = "\n".join(f"- {name}" for name, _ in self.MUTATIONS)
        base_dump = json.dumps(base_graph.to_dict(), indent=2)
        feedback_dump = (
            json.dumps(
                [
                    {
                        "loop_id": getattr(f, "loop_id", "?"),
                        "quality": float(getattr(f, "quality_score", 0.0)),
                    }
                    for f in feedback[:5]
                ],
                indent=2,
            )
            if feedback
            else "(no recent feedback)"
        )
        sample = (task_sample or "")[:600]
        return (
            f"# BASE LOOP\n{base_id}\n\n"
            f"# BASE GRAPH (JSON)\n{base_dump}\n\n"
            f"# TASK TYPE\n{task_type}\n\n"
            f"# SAMPLE TASK\n{sample or '(empty)'}\n\n"
            f"# RECENT FEEDBACK (last {len(feedback[:5])} scores)\n{feedback_dump}\n\n"
            f"# MUTATION CATALOG\n{catalog_lines}\n\n"
            "Pick the best mutation for this task type.  Reply with JSON:\n"
            "{\"mutation\": \"<name>\", \"rationale\": \"<one sentence>\"}\n"
            "Use one of the catalog names.  If no change is needed, use \"noop\"."
        )

    def _parse_proposal(self, content: str) -> dict[str, Any]:
        """Pull ``mutation`` and ``rationale`` out of the LLM response."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Regex fallback for the mutation name.
        if "mutation" not in data:
            match = re.search(r"\"?mutation\"?\s*[:=]\s*\"([^\"]+)\"", text, re.IGNORECASE)
            if match:
                data["mutation"] = match.group(1).strip()
        if "rationale" not in data:
            match = re.search(
                r"\"?rationale\"?\s*[:=]\s*\"([^\"]+)\"", text, re.IGNORECASE
            )
            if match:
                data["rationale"] = match.group(1).strip()
        return data

    # ------------------------------------------------------------------
    # Heuristic path
    # ------------------------------------------------------------------

    def _heuristic_variant(
        self,
        *,
        base_id: str,
        base_graph: LoopGraph,
        task_type: str,
        task_sample: str,
        feedback: list[Any],
    ) -> LoopVariant:
        """Pick a mutation deterministically from the catalog.

        Selection rules (in order):

        1. If any recent feedback has ``quality < 5`` and the base is
           ``direct``, switch to ``reflection`` (most common failure
           mode for "too cheap" loops).
        2. If the task type is math / logic, always apply
           ``strengthen_cot`` (raises temperature low and adds a
           second generate step).
        3. If the task type is in the design/planning family, prefer
           ``raise_branch_count``.
        4. Otherwise, hash the (base, task_type) pair to pick a stable
           mutation from the catalog.  This guarantees two consecutive
           calls with the same args return the same variant, which is
           what the tests need.
        """
        quality_low = any(
            float(getattr(f, "quality_score", 5.0)) < 5.0 for f in feedback
        )
        if base_id == "direct" and quality_low:
            mutation_name = "upgrade_to_reflection"
        elif task_type in {"math", "logic", "analysis", "research"}:
            mutation_name = "strengthen_cot"
        elif task_type in {"design", "architecture", "planning", "brainstorm"}:
            mutation_name = "raise_branch_count"
        else:
            mutation_name = self._stable_pick(base_id, task_type)

        mutated, modification = self._apply_mutation(base_graph, mutation_name)
        loop_id = self._variant_id(base_id, task_type, mutation_name)
        return LoopVariant(
            loop_id=loop_id,
            base_loop_id=base_id,
            task_type=task_type,
            graph=mutated,
            modification=modification,
            rationale=(
                "Heuristic pick: "
                f"mutation={mutation_name}, "
                f"task_type={task_type}, base={base_id}."
            ),
        )

    def _stable_pick(self, base_id: str, task_type: str) -> str:
        """Hash the inputs to a catalog name for deterministic variety."""
        digest = hashlib.sha256(f"{base_id}|{task_type}".encode("utf-8")).digest()
        idx = digest[0] % max(1, len(self.MUTATIONS))
        return self.MUTATIONS[idx][0]

    def _pick_base_for_task(self, task_type: str) -> str:
        """Map a task type to its natural base loop."""
        return TASK_TYPE_TO_BASE.get(task_type, DEFAULT_BASE_LOOP)

    # ------------------------------------------------------------------
    # Mutation catalog
    # ------------------------------------------------------------------

    def _apply_mutation(
        self, base_graph: LoopGraph, mutation_name: str
    ) -> tuple[LoopGraph, str]:
        """Run the named mutation.  Returns ``(new_graph, label)``.

        Unknown mutations (including the explicit ``"noop"``) return
        the base graph untouched, so the LLM can abstain without
        breaking the pipeline.
        """
        for name, fn in self.MUTATIONS:
            if name == mutation_name:
                return fn(self, base_graph)
        return base_graph, f"noop (unknown mutation {mutation_name!r})"

    # Mutation implementations.  Each takes the meta-agent instance
    # (so they can reach ``_variant_id`` etc.) and the base graph, and
    # returns ``(new_graph, label)``.

    def _mutate_noop(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        return base_graph, "noop"

    def _mutate_upgrade_to_reflection(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Wrap a generate-only loop in a draft→critique→revise chain."""
        if any(n.primitive == PrimitiveType.REVISE for n in base_graph.nodes):
            return base_graph, "noop (already reflective)"
        # Add a critique+revise tail off the existing terminal node(s).
        critique_id = self._fresh_id("critique")
        revise_id = self._fresh_id("revise")
        critique = LoopPrimitive(
            node_id=critique_id,
            primitive=PrimitiveType.CRITIQUE,
            temperature=0.3,
            parameters={"rubric": "Evaluate quality, correctness, completeness."},
        )
        revise = LoopPrimitive(
            node_id=revise_id,
            primitive=PrimitiveType.REVISE,
            temperature=0.5,
        )
        terminals = list(base_graph.terminal_nodes) or [
            n.node_id for n in base_graph.nodes
        ]
        new_edges = list(base_graph.edges)
        for term in terminals:
            new_edges.append(LoopEdge(from_node=term, to_node=critique_id))
        new_edges.append(LoopEdge(from_node=critique_id, to_node=revise_id))
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (with critique+revise)",
            description=base_graph.description,
            nodes=base_graph.nodes + [critique, revise],
            edges=new_edges,
            terminal_nodes=[revise_id],
            entry_node=base_graph.entry_node,
        ), "upgrade_to_reflection"

    def _mutate_strengthen_cot(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Lower the temperature and add a CoT-style prompt prefix."""
        nodes: list[LoopPrimitive] = []
        for n in base_graph.nodes:
            new_params = dict(n.parameters)
            new_params["prompt_template"] = (
                new_params.get("prompt_template")
                or "Think step by step. {{ task }}"
            )
            nodes.append(
                n.model_copy(
                    update={
                        "temperature": min(0.3, n.temperature),
                        "parameters": new_params,
                    }
                )
            )
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (CoT-strengthened)",
            description=base_graph.description,
            nodes=nodes,
            edges=list(base_graph.edges),
            terminal_nodes=list(base_graph.terminal_nodes),
            entry_node=base_graph.entry_node,
        ), "strengthen_cot"

    def _mutate_raise_branch_count(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Bump ``branch`` nodes' ``n`` parameter from 3 → 5."""
        nodes: list[LoopPrimitive] = []
        for n in base_graph.nodes:
            if n.primitive == PrimitiveType.BRANCH:
                new_params = dict(n.parameters)
                new_params["n"] = int(new_params.get("n", 3)) + 2
                nodes.append(n.model_copy(update={"parameters": new_params}))
            else:
                nodes.append(n)
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (more branches)",
            description=base_graph.description,
            nodes=nodes,
            edges=list(base_graph.edges),
            terminal_nodes=list(base_graph.terminal_nodes),
            entry_node=base_graph.entry_node,
        ), "raise_branch_count"

    def _mutate_add_critique(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Add a single critique node after the existing terminal."""
        if any(n.primitive == PrimitiveType.CRITIQUE for n in base_graph.nodes):
            return base_graph, "noop (already has critique)"
        critique_id = self._fresh_id("critique")
        critique = LoopPrimitive(
            node_id=critique_id,
            primitive=PrimitiveType.CRITIQUE,
            temperature=0.3,
        )
        terminals = list(base_graph.terminal_nodes) or [
            n.node_id for n in base_graph.nodes
        ]
        new_edges = list(base_graph.edges)
        for term in terminals:
            new_edges.append(LoopEdge(from_node=term, to_node=critique_id))
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (with critique)",
            description=base_graph.description,
            nodes=base_graph.nodes + [critique],
            edges=new_edges,
            terminal_nodes=[critique_id],
            entry_node=base_graph.entry_node,
        ), "add_critique"

    def _mutate_swap_to_cot(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Re-emit a single generate node with CoT prompt prefix."""
        first = base_graph.nodes[0] if base_graph.nodes else None
        if first is None:
            return base_graph, "noop (empty graph)"
        new_params = dict(first.parameters)
        new_params["prompt_template"] = (
            new_params.get("prompt_template")
            or "Think step by step. {{ task }}"
        )
        new_node = first.model_copy(update={"parameters": new_params})
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (CoT)",
            description=base_graph.description,
            nodes=[new_node],
            edges=[],
            terminal_nodes=[new_node.node_id],
            entry_node=new_node.node_id,
        ), "swap_to_cot"

    def _mutate_lower_temperature(
        self, base_graph: LoopGraph
    ) -> tuple[LoopGraph, str]:
        """Drop every node's temperature to be more deterministic."""
        nodes = [
            n.model_copy(update={"temperature": min(0.2, n.temperature)})
            for n in base_graph.nodes
        ]
        return LoopGraph(
            loop_id=base_graph.loop_id,
            name=f"{base_graph.name} (cooler)",
            description=base_graph.description,
            nodes=nodes,
            edges=list(base_graph.edges),
            terminal_nodes=list(base_graph.terminal_nodes),
            entry_node=base_graph.entry_node,
        ), "lower_temperature"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fresh_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def _variant_id(
        self, base_id: str, task_type: str, mutation_name: str
    ) -> str:
        """Make a deterministic-ish id for the variant.

        Two calls with the same inputs return the same id, which is
        what the trial store needs to attribute repeated trials to the
        same loop_id.
        """
        digest = hashlib.sha256(
            f"{base_id}|{task_type}|{mutation_name}".encode("utf-8")
        ).hexdigest()[:10]
        safe_task = re.sub(r"[^a-z0-9]+", "-", task_type.lower()).strip("-") or "general"
        return f"{base_id}-{mutation_name}-{safe_task}-{digest}"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _default_mutations() -> list[tuple[str, Any]]:
    """Return the canned mutation catalog.

    Defined at module scope so :meth:`MetaAgent.__init__` can attach
    the bound methods to the instance after construction.
    """

    def _noop(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_noop(g)

    def _upgrade(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_upgrade_to_reflection(g)

    def _strengthen(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_strengthen_cot(g)

    def _raise(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_raise_branch_count(g)

    def _crit(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_add_critique(g)

    def _swap(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_swap_to_cot(g)

    def _cooler(agent: MetaAgent, g: LoopGraph) -> tuple[LoopGraph, str]:
        return agent._mutate_lower_temperature(g)

    return [
        ("noop", _noop),
        ("upgrade_to_reflection", _upgrade),
        ("strengthen_cot", _strengthen),
        ("raise_branch_count", _raise),
        ("add_critique", _crit),
        ("swap_to_cot", _swap),
        ("lower_temperature", _cooler),
    ]


__all__ = [
    "DEFAULT_BASE_LOOP",
    "LoopVariant",
    "MetaAgent",
    "TASK_TYPE_TO_BASE",
]
