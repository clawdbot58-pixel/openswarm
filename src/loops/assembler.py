"""Loop assembler - executes LoopGraph definitions.

Phase 4 ships a dataclass-based :class:`loops.graph.LoopGraph` that the
:class:`LoopAssembler` consumes directly.  Phase 10 adds a Pydantic
counterpart — :class:`LoopGraph` in *this* module — that the
:mod:`src.meta_agent` and :mod:`src.loop_optimizer` modules use to build
JSON-serialisable graph descriptions without going through the
dataclass.  The two coexist: the Phase 4 :class:`loops.graph.LoopGraph`
is still the canonical representation in the loop registry, and the
Pydantic one converts to/from it via :meth:`LoopGraph.to_graph` /
:meth:`LoopGraph.from_graph`.

The :class:`LoopAssembler` class also gets two new methods that the
Phase 10 spec asks for:

* :meth:`LoopAssembler.execute_graph` — alias for :meth:`execute` that
  accepts a Pydantic :class:`LoopGraph` and returns a
  :class:`loops.base_loop.LoopResult`.
* :meth:`LoopAssembler.assemble_builtin` — build a premade
  Pydantic :class:`LoopGraph` from a name (``direct`` / ``cot`` /
  ``reflection`` / ``tree`` / ``debate``).
"""

from __future__ import annotations

import re
import time
from typing import Any

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field

from .base_loop import LoopResult
from .graph import LoopEdge as _DataclassLoopEdge
from .graph import LoopGraph as _DataclassLoopGraph
from .graph import LoopNode as _DataclassLoopNode
from .graph import GraphValidationError
from .model_router import LLMClient
from .primitives import (
    LoopPrimitive,
    PrimitiveExecutor,
    PrimitiveOutput,
    PrimitiveType,
    PrimitiveContext as _Phase4Context,
)
from .primitives import get_primitive


class StopConditionError(Exception):
    """Raised when a stop condition is met."""
    pass


class AssemblerError(Exception):
    """Raised when assembly or execution fails."""
    pass


class LoopAssembler:
    """Executes a LoopGraph using primitives."""

    def __init__(self, primitives: dict[str, Any] | None = None):
        """Initialize the loop assembler.

        Args:
            primitives: Optional dict mapping primitive names to classes.
                        If not provided, uses default primitives.
        """
        self._primitive_registry = primitives or {}

    def get_primitive_instance(self, name: str) -> Any:
        """Get a primitive instance by name.

        Args:
            name: Primitive name.

        Returns:
            Primitive instance.

        Raises:
            AssemblerError: If primitive not found.
        """
        if name in self._primitive_registry:
            primitive_or_class = self._primitive_registry[name]
            if callable(primitive_or_class):
                return primitive_or_class()
            return primitive_or_class

        try:
            return get_primitive(name)
        except ValueError as e:
            raise AssemblerError(str(e))

    async def execute(
        self,
        graph: Any,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute a loop graph.

        Args:
            graph: The LoopGraph to execute.
            task: The original user task.
            preamble: The preamble context.
            model_client: The LLM client.

        Returns:
            LoopResult with output and metadata.

        Raises:
            AssemblerError: If execution fails.
        """
        try:
            graph.validate()
        except Exception as e:
            raise AssemblerError(f"Graph validation failed: {e}")

        system_prompt = self._build_system_prompt(preamble)

        node_results: dict[str, Any] = {}
        iteration = 0
        total_tokens = 0
        total_cost = 0.0
        total_latency = 0.0
        intermediate_outputs: list[dict[str, Any]] = []

        sorted_nodes = graph.topological_sort()

        for node_id in sorted_nodes:
            iteration += 1

            node = graph.get_node(node_id)
            if node is None:
                raise AssemblerError(f"Node '{node_id}' not found")

            context = self._build_context(
                node=node,
                graph=graph,
                task=task,
                system_prompt=system_prompt,
                model_client=model_client,
                node_results=node_results,
            )

            primitive = self.get_primitive_instance(node.primitive)

            try:
                result = await primitive.execute(context)
            except Exception as e:
                raise AssemblerError(
                    f"Primitive '{node.primitive}' failed on node '{node_id}': {e}"
                )

            node_results[node_id] = result
            total_tokens += result.tokens_used
            total_cost += result.cost_usd
            total_latency += result.latency_ms

            intermediate_outputs.append({
                "node_id": node_id,
                "primitive": node.primitive,
                "output": result.output,
                "score": result.score,
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
            })

            if self._check_stop_conditions(graph.stop_conditions, node_results, iteration):
                break

        final_output = self._aggregate_terminal_outputs(graph, node_results)

        return LoopResult(
            output=final_output,
            confidence=self._compute_confidence(node_results),
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=total_latency,
            iterations=iteration,
            intermediate_outputs=intermediate_outputs,
        )

    def _build_system_prompt(self, preamble: dict[str, Any]) -> str:
        """Build system prompt from preamble.

        Args:
            preamble: The preamble context.

        Returns:
            Formatted system prompt.
        """
        from .preamble_assembler import assemble

        intent = preamble.get("intent", {})
        permissions = preamble.get("permissions", {})

        minimal_manifest = {
            "role": "executor",
            "intent": intent.get("goal", "") if isinstance(intent, dict) else str(intent),
            "agent_id": "assembler-worker",
        }

        return assemble(preamble, minimal_manifest)

    def _build_context(
        self,
        node: Any,
        graph: Any,
        task: str,
        system_prompt: str,
        model_client: LLMClient,
        node_results: dict[str, Any],
    ) -> _Phase4Context:
        """Build execution context for a node.

        Args:
            node: The node to execute.
            graph: The full graph.
            task: Original task.
            system_prompt: Assembled system prompt.
            model_client: LLM client.
            node_results: Results from previous nodes.

        Returns:
            PrimitiveContext for node execution.
        """
        incoming_edges = graph.get_incoming_edges(node.id)

        inputs: dict[str, Any] = {}

        for edge in incoming_edges:
            source_result = node_results.get(edge.from_node)
            if source_result is not None:
                if edge.output_key == "output":
                    inputs[edge.from_node] = source_result.output
                elif edge.output_key == "score":
                    inputs[f"{edge.from_node}_score"] = source_result.score
                elif edge.output_key == "candidates":
                    candidates_str = source_result.output
                    if isinstance(candidates_str, str):
                        try:
                            inputs.setdefault("candidates", []).extend(
                                eval(candidates_str) if "[" in candidates_str else [candidates_str]
                            )
                        except Exception:
                            inputs.setdefault("candidates", []).append(candidates_str)
                    elif isinstance(candidates_str, list):
                        inputs.setdefault("candidates", []).extend(candidates_str)
                elif edge.output_key == "original":
                    inputs["original"] = source_result.output
                elif edge.output_key == "target":
                    inputs["target"] = source_result.output
                elif edge.output_key == "critique":
                    inputs["critique"] = source_result.output
                elif edge.output_key == "outputs":
                    inputs.setdefault("outputs", []).append(source_result.output)
                else:
                    inputs[edge.output_key] = source_result.output

        if node.prompt_template:
            try:
                template = Template(node.prompt_template)
                rendered = template.render(task=task, **inputs)
                inputs["prompt"] = rendered
            except Exception:
                inputs["prompt"] = node.prompt_template
        elif node.primitive == "generate" and "prompt" not in inputs:
            if task:
                inputs["prompt"] = task

        config = dict(node.config)
        if node.model:
            config["model"] = node.model

        return _Phase4Context(
            task=task,
            model_client=model_client,
            system_prompt=system_prompt,
            inputs=inputs,
            config=config,
            metadata={
                "node_id": node.id,
                "graph_id": graph.id,
            },
        )

    def _check_stop_conditions(
        self,
        stop_conditions: list[str],
        node_results: dict[str, Any],
        iteration: int,
    ) -> bool:
        """Check if any stop condition is met.

        Args:
            stop_conditions: List of stop condition expressions.
            node_results: Results from executed nodes.
            iteration: Current iteration count.

        Returns:
            True if any stop condition is met.
        """
        if not stop_conditions:
            return False

        for condition in stop_conditions:
            if self._evaluate_condition(condition, node_results, iteration):
                return True

        return False

    def _evaluate_condition(
        self,
        condition: str,
        node_results: dict[str, Any],
        iteration: int,
    ) -> bool:
        """Evaluate a stop condition.

        Supports: >, <, >=, <=, ==, and, or

        Args:
            condition: Condition expression (e.g., "score > 8").
            node_results: Results from executed nodes.
            iteration: Current iteration count.

        Returns:
            True if condition is met.
        """
        condition = condition.strip()

        condition = condition.replace("iteration", str(iteration))

        last_result = None
        for result in node_results.values():
            if hasattr(result, "score") and result.score is not None:
                last_result = result

        if last_result is not None and "score" in condition:
            condition = condition.replace("score", str(last_result.score))

        if last_result is not None and "confidence" in condition:
            confidence = last_result.score / 10.0 if last_result.score else 0.0
            condition = condition.replace("confidence", str(confidence))

        try:
            allowed_chars = set("0123456789.><=and or() ")
            filtered = "".join(c for c in condition if c in allowed_chars or c.isalnum())
            return bool(eval(filtered))
        except Exception:
            return False

    def _aggregate_terminal_outputs(
        self,
        graph: LoopGraph,
        node_results: dict[str, Any],
    ) -> str:
        """Aggregate outputs from terminal nodes.

        Args:
            graph: The loop graph.
            node_results: Results from all nodes.

        Returns:
            Aggregated output string.
        """
        terminal_outputs: list[str] = []

        for terminal_id in graph.terminal_nodes:
            result = node_results.get(terminal_id)
            if result is not None:
                terminal_outputs.append(result.output)

        if len(terminal_outputs) == 1:
            return terminal_outputs[0]

        return "\n\n".join(terminal_outputs)

    def _compute_confidence(self, node_results: dict[str, Any]) -> float:
        """Compute confidence from node results.

        Args:
            node_results: Results from executed nodes.

        Returns:
            Confidence score 0.0-1.0.
        """
        scores: list[float] = []

        for result in node_results.values():
            if hasattr(result, "score") and result.score is not None:
                scores.append(result.score)

        if not scores:
            return 0.7

        avg_score = sum(scores) / len(scores)
        return min(1.0, avg_score / 10.0)


class PrebuiltGraphs:
    """Factory for prebuilt loop graphs.

    Provides convenience methods to create common graph configurations.
    """

    @staticmethod
    def direct(task: str, preamble: dict[str, Any], model_client: LLMClient) -> LoopResult:
        """Execute a direct loop.

        Args:
            task: The task content.
            preamble: The preamble context.
            model_client: LLM client.

        Returns:
            LoopResult from direct execution.
        """
        graph = _DataclassLoopGraph.direct_graph()
        assembler = LoopAssembler()
        return assembler.execute(graph, task, preamble, model_client)

    @staticmethod
    def reflection(task: str, preamble: dict[str, Any], model_client: LLMClient) -> LoopResult:
        """Execute a reflection loop.

        Args:
            task: The task content.
            preamble: The preamble context.
            model_client: LLM client.

        Returns:
            LoopResult from reflection loop.
        """
        graph = _DataclassLoopGraph.reflection_graph()
        assembler = LoopAssembler()
        return assembler.execute(graph, task, preamble, model_client)

    @staticmethod
    def tree(
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
        branch_count: int = 3,
    ) -> LoopResult:
        """Execute a tree of thoughts loop.

        Args:
            task: The task content.
            preamble: The preamble context.
            model_client: LLM client.
            branch_count: Number of branches.

        Returns:
            LoopResult from tree loop.
        """
        graph = _DataclassLoopGraph.tree_graph(branch_count=branch_count)
        assembler = LoopAssembler()
        return assembler.execute(graph, task, preamble, model_client)

    @staticmethod
    def cot(task: str, preamble: dict[str, Any], model_client: LLMClient) -> LoopResult:
        """Execute a chain-of-thought loop.

        Args:
            task: The task content.
            preamble: The preamble context.
            model_client: LLM client.

        Returns:
            LoopResult from CoT loop.
        """
        graph = _DataclassLoopGraph.cot_graph()
        assembler = LoopAssembler()
        return assembler.execute(graph, task, preamble, model_client)


# ---------------------------------------------------------------------------
# Phase 10 — Pydantic LoopGraph + execute_graph / assemble_builtin
#
# The Phase 4 dataclass :class:`loops.graph.LoopGraph` is still the
# canonical graph representation in the registry.  The Pydantic model
# below is what the meta-agent and the loop-optimizer pass around: it
# is JSON-safe, validates the DAG on construction, and converts
# to/from the dataclass via :meth:`to_graph` / :meth:`from_graph`.
# ---------------------------------------------------------------------------


# Valid primitive names — kept in sync with ``loops.graph``.
_VALID_PRIMITIVE_NAMES: set[str] = {p.value for p in PrimitiveType}


class LoopGraph(BaseModel):
    """A directed acyclic graph of reasoning primitives.

    Mirrors the dataclass ``loops.graph.LoopGraph`` but is Pydantic-typed
    and JSON-serialisable.  Both representations convert into each other
    losslessly, and :class:`LoopAssembler` knows how to execute either.

    Attributes:
        loop_id: Stable identifier (used as the trial/leaderboard key).
        name: Human-readable name.
        description: Optional summary of what the loop does.
        nodes: Reasoning nodes (one per LLM call).
        edges: Directed edges between nodes (``from``/``to`` shorthand
            is also accepted by :meth:`from_dict`).
        terminal_nodes: Node ids whose outputs form the final result.
        entry_node: First node to run.  Defaults to the first node by id.
    """

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    name: str
    description: str = ""
    nodes: list[LoopPrimitive] = Field(default_factory=list)
    edges: list["LoopEdge"] = Field(default_factory=list)
    terminal_nodes: list[str] = Field(default_factory=list)
    entry_node: str | None = None

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        """Validate the graph on construction (Pydantic v2 hook)."""
        # Stash raw for late validation in case pydantic re-instantiates.
        object.__setattr__(self, "_validated", False)
        self.validate_dag()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_dag(self) -> None:
        """Validate the graph and raise :class:`GraphValidationError`.

        The rules enforced here are the same ones
        :class:`loops.graph.LoopGraph` enforces in Phase 4:

        * node ids are unique;
        * every edge references existing nodes;
        * every primitive name is one of the six canonical types;
        * the graph is a DAG (no cycles);
        * all nodes are reachable from the entry node.

        Raises:
            GraphValidationError: If any rule is broken.
        """
        node_ids = [n.node_id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            duplicates = sorted({nid for nid in node_ids if node_ids.count(nid) > 1})
            raise GraphValidationError(
                f"duplicate node ids in graph: {duplicates}"
            )

        known = set(node_ids)
        for n in self.nodes:
            prim_value = (
                n.primitive.value
                if isinstance(n.primitive, PrimitiveType)
                else str(n.primitive)
            )
            if prim_value not in _VALID_PRIMITIVE_NAMES:
                raise GraphValidationError(
                    f"node {n.node_id!r} has unknown primitive {prim_value!r}; "
                    f"valid: {sorted(_VALID_PRIMITIVE_NAMES)}"
                )

        for edge in self.edges:
            if edge.from_node not in known:
                raise GraphValidationError(
                    f"edge references non-existent from_node {edge.from_node!r}"
                )
            if edge.to_node not in known:
                raise GraphValidationError(
                    f"edge references non-existent to_node {edge.to_node!r}"
                )

        # Cycle detection via Kahn's algorithm.
        in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
        adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in self.edges:
            in_degree[edge.to_node] += 1
            adjacency[edge.from_node].append(edge.to_node)

        queue: list[str] = [nid for nid, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            cur = queue.pop(0)
            visited += 1
            for neighbour in adjacency[cur]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)
        if visited != len(node_ids):
            raise GraphValidationError(
                "cycle detected in graph; not a valid DAG"
            )

        # Reachability from entry.
        entry = self.entry_node or (node_ids[0] if node_ids else None)
        if entry is None:
            raise GraphValidationError("graph has no nodes")
        if entry not in known:
            raise GraphValidationError(
                f"entry_node {entry!r} not found in nodes"
            )
        reachable: set[str] = {entry}
        bfs: list[str] = [entry]
        while bfs:
            cur = bfs.pop()
            for neighbour in adjacency[cur]:
                if neighbour not in reachable:
                    reachable.add(neighbour)
                    bfs.append(neighbour)
        unreachable = [nid for nid in node_ids if nid not in reachable]
        if unreachable:
            raise GraphValidationError(
                f"nodes not reachable from entry {entry!r}: {unreachable}"
            )

        # Terminals must exist; if not provided, default to nodes with no outgoing edges.
        if not self.terminal_nodes:
            has_outgoing = {edge.from_node for edge in self.edges}
            derived = [nid for nid in node_ids if nid not in has_outgoing]
            object.__setattr__(self, "terminal_nodes", derived)
        for tn in self.terminal_nodes:
            if tn not in known:
                raise GraphValidationError(
                    f"terminal node {tn!r} not found in nodes"
                )

    # ------------------------------------------------------------------
    # Topology helpers
    # ------------------------------------------------------------------

    def topological_order(self) -> list[str]:
        """Return node ids in topological execution order."""
        in_degree: dict[str, int] = {n.node_id: 0 for n in self.nodes}
        adjacency: dict[str, list[str]] = {n.node_id: [] for n in self.nodes}
        for edge in self.edges:
            in_degree[edge.to_node] += 1
            adjacency[edge.from_node].append(edge.to_node)
        queue: list[str] = [nid for nid, d in in_degree.items() if d == 0]
        order: list[str] = []
        while queue:
            cur = queue.pop(0)
            order.append(cur)
            for neighbour in adjacency[cur]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)
        if len(order) != len(self.nodes):
            raise GraphValidationError("graph has cycles; no valid topological order")
        return order

    def incoming_edges(self, node_id: str) -> list["LoopEdge"]:
        """Return all edges pointing at ``node_id``."""
        return [e for e in self.edges if e.to_node == node_id]

    def outgoing_edges(self, node_id: str) -> list["LoopEdge"]:
        """Return all edges originating from ``node_id``."""
        return [e for e in self.edges if e.from_node == node_id]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the graph to a JSON-safe dict.

        Returns:
            A dict whose values are JSON-serialisable.  Suitable for
            storage in the trial database's opaque ``graph_json`` blob.
        """
        return {
            "loop_id": self.loop_id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "terminal_nodes": list(self.terminal_nodes),
            "entry_node": self.entry_node,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopGraph":
        """Build a graph from a dict.

        Accepts edges specified with either ``from_node``/``to_node``
        (canonical) or ``from``/``to`` (the shorthand used in
        ``vision/thinking-loops.md``).  Extra keys are ignored.
        """
        nodes = []
        for raw in data.get("nodes", []):
            if isinstance(raw, LoopPrimitive):
                nodes.append(raw)
                continue
            primitive_raw = raw.get("primitive", "generate")
            if isinstance(primitive_raw, PrimitiveType):
                primitive = primitive_raw
            else:
                primitive = PrimitiveType(str(primitive_raw))
            nodes.append(
                LoopPrimitive(
                    node_id=raw["node_id"] if "node_id" in raw else raw["id"],
                    primitive=primitive,
                    model_override=raw.get("model_override") or raw.get("model"),
                    temperature=float(raw.get("temperature", 0.7)),
                    parameters=dict(raw.get("parameters") or raw.get("config") or {}),
                )
            )
        edges = []
        for raw in data.get("edges", []):
            from_node = raw.get("from_node", raw.get("from"))
            to_node = raw.get("to_node", raw.get("to"))
            if from_node is None or to_node is None:
                raise GraphValidationError(
                    f"edge missing from_node/to_node: {raw!r}"
                )
            edges.append(
                LoopEdge(
                    from_node=from_node,
                    to_node=to_node,
                    output_key=raw.get("output_key", "output"),
                )
            )
        return cls(
            loop_id=data["loop_id"] if "loop_id" in data else data.get("id", "graph"),
            name=data.get("name", data.get("loop_id", "graph")),
            description=data.get("description", ""),
            nodes=nodes,
            edges=edges,
            terminal_nodes=list(data.get("terminal_nodes", [])),
            entry_node=data.get("entry_node"),
        )

    # ------------------------------------------------------------------
    # Conversion to/from the Phase 4 dataclass representation
    # ------------------------------------------------------------------

    def to_graph(self) -> _DataclassLoopGraph:
        """Convert to the Phase 4 :class:`loops.graph.LoopGraph` dataclass.

        The Phase 4 :class:`LoopAssembler` only knows the dataclass
        shape, so this bridge keeps the rest of the system unchanged.
        """
        nodes = []
        for n in self.nodes:
            primitive_name = (
                n.primitive.value
                if isinstance(n.primitive, PrimitiveType)
                else str(n.primitive)
            )
            # The dataclass stores node-specific knobs in ``config``.
            # Pull the well-known keys out for clarity and keep the rest
            # under the same key.
            config = dict(n.parameters)
            node = _DataclassLoopNode(
                id=n.node_id,
                primitive=primitive_name,
                model=n.model_override,
                config=config,
                prompt_template=(
                    config.pop("prompt_template", None)
                    if isinstance(config, dict)
                    else None
                ),
            )
            nodes.append(node)
        edges = [
            _DataclassLoopEdge(
                from_node=e.from_node, to_node=e.to_node, output_key=e.output_key
            )
            for e in self.edges
        ]
        entry = self.entry_node or (self.nodes[0].node_id if self.nodes else "")
        # The dataclass enforces at least one terminal node; copy or
        # derive ours before constructing.
        terminals = list(self.terminal_nodes)
        if not terminals and self.nodes:
            has_outgoing = {e.from_node for e in edges}
            terminals = [n.node_id for n in self.nodes if n.node_id not in has_outgoing]
        return _DataclassLoopGraph(
            id=self.loop_id,
            name=self.name,
            description=self.description or self.name,
            nodes=nodes,
            edges=edges,
            stop_conditions=[],
            entry_node=entry,
            terminal_nodes=terminals,
        )

    @classmethod
    def from_graph(cls, graph: _DataclassLoopGraph) -> "LoopGraph":
        """Build a Pydantic :class:`LoopGraph` from the Phase 4 dataclass."""
        nodes = []
        for n in graph.nodes:
            try:
                primitive = PrimitiveType(n.primitive)
            except ValueError:
                continue
            nodes.append(
                LoopPrimitive(
                    node_id=n.id,
                    primitive=primitive,
                    model_override=n.model,
                    temperature=float((n.config or {}).get("temperature", 0.7)),
                    parameters={k: v for k, v in (n.config or {}).items() if k != "temperature"},
                )
            )
        edges = [
            LoopEdge(
                from_node=e.from_node, to_node=e.to_node, output_key=e.output_key
            )
            for e in graph.edges
        ]
        return cls(
            loop_id=graph.id,
            name=graph.name,
            description=graph.description,
            nodes=nodes,
            edges=edges,
            terminal_nodes=list(graph.terminal_nodes),
            entry_node=graph.entry_node,
        )


class LoopEdge(BaseModel):
    """A directed edge between two :class:`LoopPrimitive` nodes.

    Attributes:
        from_node: Source node id.
        to_node: Target node id.
        output_key: Which field of the source's :class:`PrimitiveOutput`
            to forward (``output``, ``score``, ``candidates``, ...).
            Defaults to ``output``.
    """

    model_config = ConfigDict(extra="forbid")

    from_node: str
    to_node: str
    output_key: str = "output"


# Resolve forward references now that LoopEdge exists.
LoopGraph.model_rebuild()


# ---------------------------------------------------------------------------
# Wire the new API onto the existing LoopAssembler
# ---------------------------------------------------------------------------


def _to_primitive(node: _DataclassLoopNode) -> LoopPrimitive:
    """Convert a Phase 4 dataclass node to a :class:`LoopPrimitive`."""
    primitive = PrimitiveType(node.primitive)
    return LoopPrimitive(
        node_id=node.id,
        primitive=primitive,
        model_override=node.model,
        temperature=float((node.config or {}).get("temperature", 0.7)),
        parameters={k: v for k, v in (node.config or {}).items() if k != "temperature"},
    )


def _collect_terminals(graph: _DataclassLoopGraph) -> list[str]:
    """Re-derive terminal nodes if the graph has none explicit."""
    if graph.terminal_nodes:
        return list(graph.terminal_nodes)
    has_outgoing = {e.from_node for e in graph.edges}
    return [n.id for n in graph.nodes if n.id not in has_outgoing]


# Attach Phase 10 methods to the existing LoopAssembler without
# monkey-patching via setattr-by-class (so mypy can see them).
def _assembler_execute_graph(
    self: "LoopAssembler",
    graph: "LoopGraph",
    task: str,
    preamble: dict[str, Any],
    model_client: LLMClient,
) -> LoopResult:
    """Execute a Pydantic :class:`LoopGraph` and return a :class:`LoopResult`.

    This is the Phase 10 entry point.  It accepts the Pydantic graph
    and internally delegates to the same Phase 4 execution engine that
    :meth:`LoopAssembler.execute` uses, by first converting to the
    Phase 4 dataclass form.
    """
    return self.execute(graph.to_graph(), task, preamble, model_client)


def _assembler_assemble_builtin(
    self: "LoopAssembler",
    loop_type: str,
) -> "LoopGraph":
    """Build a premade Pydantic :class:`LoopGraph`.

    Args:
        loop_type: One of ``direct`` / ``cot`` / ``reflection`` /
            ``tree`` / ``debate`` / ``ensemble``.

    Returns:
        A validated :class:`LoopGraph`.

    Raises:
        ValueError: If ``loop_type`` is not a known premade loop.
    """
    loop_type = loop_type.lower()
    if loop_type == "direct":
        base = _DataclassLoopGraph.direct_graph("direct")
    elif loop_type == "cot":
        base = _DataclassLoopGraph.cot_graph("cot")
    elif loop_type == "reflection":
        base = _DataclassLoopGraph.reflection_graph("reflection")
    elif loop_type == "tree":
        base = _DataclassLoopGraph.tree_graph("tree", branch_count=3)
    elif loop_type == "debate":
        base = _DataclassLoopGraph.debate_graph("debate")
    elif loop_type == "ensemble":
        base = _DataclassLoopGraph.ensemble_graph("ensemble")
    else:
        raise ValueError(
            f"unknown premade loop type: {loop_type!r}; "
            f"valid: direct, cot, reflection, tree, debate, ensemble"
        )
    return LoopGraph.from_graph(base)


# Bind the new methods onto the existing class.  Pylint/mypy see them
# as bound methods; runtime behaviour is identical to defining them
# directly in the class body.
LoopAssembler.execute_graph = _assembler_execute_graph  # type: ignore[attr-defined]
LoopAssembler.assemble_builtin = _assembler_assemble_builtin  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Public re-exports — keep ``loops.assembler`` discoverable as the
# "place to build and execute graphs" without breaking the Phase 4
# import paths.
# ---------------------------------------------------------------------------


__all__ = [
    "AssemblerError",
    "LoopAssembler",
    "LoopEdge",
    "LoopGraph",
    "PrebuiltGraphs",
    "PrimitiveExecutor",
    "PrimitiveOutput",
    "StopConditionError",
]


# Re-export the Phase 4 dataclass LoopGraph for the few call sites that
# need it; the Pydantic ``LoopGraph`` defined above is the canonical
# public name now.  The dataclass remains available as
# ``assembler._DataclassLoopGraph`` for any code that needs the
# underlying Phase 4 representation.
__all__.append("_DataclassLoopGraph")