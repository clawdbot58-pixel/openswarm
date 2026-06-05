"""Loop assembler - executes LoopGraph definitions."""

import re
import time
from typing import Any

from jinja2 import Template

from .base_loop import LoopResult
from .graph import LoopEdge, LoopGraph, LoopNode
from .model_router import LLMClient
from .primitives import PrimitiveContext, get_primitive


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
        graph: LoopGraph,
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
        node: LoopNode,
        graph: LoopGraph,
        task: str,
        system_prompt: str,
        model_client: LLMClient,
        node_results: dict[str, Any],
    ) -> PrimitiveContext:
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

        return PrimitiveContext(
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
        graph = LoopGraph.direct_graph()
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
        graph = LoopGraph.reflection_graph()
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
        graph = LoopGraph.tree_graph(branch_count=branch_count)
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
        graph = LoopGraph.cot_graph()
        assembler = LoopAssembler()
        return assembler.execute(graph, task, preamble, model_client)