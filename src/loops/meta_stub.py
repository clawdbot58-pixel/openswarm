"""Meta-agent stub - interface for dynamic loop assembly.

Phase 4 provides the stub interface.
Phase 10 will implement the full meta-agent with LLM-based graph generation.
"""

from typing import Any

from .graph import LoopEdge, LoopGraph, LoopNode


class MetaAgentStub:
    """Placeholder for Phase 10 Meta-Agent.

    Phase 4: Returns simple heuristic-based graphs.
    Phase 10: Replace with LLM-based graph generation.

    The meta-agent receives task descriptions and outputs loop graph JSON.
    It does not execute loops - only designs them.
    """

    TASK_TYPE_GRAPHS: dict[str, str] = {
        "coding": "reflection",
        "review": "reflection",
        "debugging": "reflection",
        "architecture": "tree",
        "design": "tree",
        "planning": "tree",
        "research": "cot",
        "analysis": "debate",
        "writing": "reflection",
        "general": "direct",
    }

    async def propose_loop(
        self,
        task_description: str,
        task_type: str,
    ) -> LoopGraph:
        """Propose a loop graph for a task.

        Phase 4: Uses simple heuristics based on task type.
        Phase 10: Uses LLM to generate custom graph based on task analysis.

        Args:
            task_description: Natural language description of the task.
            task_type: Categorized task type (coding, review, research, etc.).

        Returns:
            LoopGraph designed for the task.
        """
        graph_id = self.TASK_TYPE_GRAPHS.get(task_type, "direct")

        return self._get_graph_for_type(graph_id, task_type)

    def _get_graph_for_type(self, graph_type: str, task_type: str) -> LoopGraph:
        """Get a predefined graph for a type.

        Args:
            graph_type: Graph type identifier.
            task_type: Task category.

        Returns:
            LoopGraph instance.
        """
        import uuid

        graph_id = f"custom-{graph_type}-{uuid.uuid4().hex[:8]}"

        if graph_type == "reflection":
            graph = LoopGraph.reflection_graph(graph_id)
            graph.description = f"Reflection loop for {task_type} tasks"
        elif graph_type == "tree":
            graph = LoopGraph.tree_graph(graph_id, branch_count=3)
            graph.description = f"Tree of thoughts for {task_type} tasks"
        elif graph_type == "cot":
            graph = LoopGraph.cot_graph(graph_id)
            graph.description = f"Chain-of-thought for {task_type} tasks"
        elif graph_type == "debate":
            graph = LoopGraph.debate_graph(graph_id)
            graph.description = f"Debate loop for {task_type} tasks"
        elif graph_type == "ensemble":
            graph = LoopGraph.ensemble_graph(graph_id)
            graph.description = f"Ensemble loop for {task_type} tasks"
        else:
            graph = LoopGraph.direct_graph(graph_id)
            graph.description = f"Direct loop for {task_type} tasks"

        return graph

    async def propose_custom_graph(
        self,
        task_description: str,
        task_type: str,
        constraints: dict[str, Any] | None = None,
    ) -> LoopGraph:
        """Propose a custom graph with constraints.

        Phase 4: Limited to predefined graph combinations.
        Phase 10: Full LLM-based custom graph generation.

        Args:
            task_description: Task description.
            task_type: Task category.
            constraints: Optional constraints (max_cost, max_latency, etc.).

        Returns:
            Custom LoopGraph.
        """
        constraints = constraints or {}

        max_cost = constraints.get("max_cost_usd", 1.0)
        max_latency = constraints.get("max_latency_ms", 10000)
        requires_review = constraints.get("requires_review", False)

        base_graph = await self.propose_loop(task_description, task_type)

        if requires_review and base_graph.id != "reflection":
            return self._add_review_to_graph(base_graph)

        return base_graph

    def _add_review_to_graph(self, graph: LoopGraph) -> LoopGraph:
        """Add a review stage to an existing graph.

        Args:
            graph: Original graph.

        Returns:
            Modified graph with review.
        """
        import uuid

        new_id = f"reviewed-{graph.id}-{uuid.uuid4().hex[:8]}"

        review_node = LoopNode(
            id="review",
            primitive="critique",
            prompt_template="Review this output for quality:\n\n{{ draft }}",
        )

        nodes = list(graph.nodes) + [review_node]

        edges = list(graph.edges)
        for terminal in graph.terminal_nodes:
            edges.append(LoopEdge(from_node=terminal, to_node="review"))

        terminal_nodes = graph.terminal_nodes + ["review"]

        return LoopGraph(
            id=new_id,
            name=f"Reviewed {graph.name}",
            description=f"Added review stage to {graph.description}",
            nodes=nodes,
            edges=edges,
            entry_node=graph.entry_node,
            terminal_nodes=terminal_nodes,
        )

    def suggest_optimization(
        self,
        current_graph: LoopGraph,
        performance_data: dict[str, Any],
    ) -> list[str]:
        """Suggest optimizations for a graph based on performance.

        Phase 4: Simple heuristic suggestions.
        Phase 10: LLM-based optimization recommendations.

        Args:
            current_graph: Graph to optimize.
            performance_data: Performance metrics.

        Returns:
            List of suggested improvements.
        """
        suggestions: list[str] = []

        avg_latency = performance_data.get("avg_latency_ms", 0)
        avg_cost = performance_data.get("avg_cost_usd", 0)
        avg_score = performance_data.get("avg_score", 5.0)
        success_rate = performance_data.get("success_rate", 0.0)

        if avg_latency > 10000 and len(current_graph.nodes) > 2:
            suggestions.append(
                "Graph is slow. Consider reducing the number of stages "
                "or using faster models for intermediate steps."
            )

        if avg_cost > 0.5 and len(current_graph.nodes) > 3:
            suggestions.append(
                "Graph is expensive. Consider using a cheaper model for "
                "less critical stages like initial generation."
            )

        if avg_score < 7.0 and current_graph.id == "direct":
            suggestions.append(
                "Direct loop scores low. Consider adding a critique/review "
                "stage for better quality."
            )

        if success_rate < 0.8 and "vote" in [n.primitive for n in current_graph.nodes]:
            suggestions.append(
                "Vote-based selection has low success rate. Consider "
                "using a merge step instead of voting."
            )

        if avg_score >= 8.0 and avg_latency < 5000:
            suggestions.append(
                "Graph performs well! No major optimizations needed."
            )

        return suggestions

    async def learn_from_result(
        self,
        task_type: str,
        graph_id: str,
        score: float,
    ) -> None:
        """Learn from execution result to improve future recommendations.

        Phase 4: Stub implementation.
        Phase 10: Update embedding model or fine-tune recommendation.

        Args:
            task_type: Type of task.
            graph_id: Graph that was used.
            score: Execution score.
        """
        pass

    def get_available_task_types(self) -> list[str]:
        """Get list of task types the meta-agent can handle.

        Returns:
            List of task type strings.
        """
        return list(self.TASK_TYPE_GRAPHS.keys())

    def get_graph_for_task(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> LoopGraph:
        """Synchronous version of propose_loop for non-async contexts.

        Args:
            task_description: Task description.
            task_type: Optional task type override.

        Returns:
            LoopGraph instance.
        """
        if task_type is None:
            task_type = self._infer_task_type(task_description)

        return self._get_graph_for_type(
            self.TASK_TYPE_GRAPHS.get(task_type, "direct"),
            task_type,
        )

    def _infer_task_type(self, task_description: str) -> str:
        """Infer task type from description.

        Args:
            task_description: Task description.

        Returns:
            Inferred task type.
        """
        desc_lower = task_description.lower()

        coding_keywords = ["code", "function", "class", "implement", "write", "program"]
        review_keywords = ["review", "critique", "check", "fix", "bug", "improve"]
        design_keywords = ["design", "architecture", "structure", "pattern", "schema"]
        research_keywords = ["research", "find", "search", "investigate", "analyze"]
        planning_keywords = ["plan", "strategy", "roadmap", "approach"]

        if any(kw in desc_lower for kw in coding_keywords):
            return "coding"
        elif any(kw in desc_lower for kw in review_keywords):
            return "review"
        elif any(kw in desc_lower for kw in design_keywords):
            return "design"
        elif any(kw in desc_lower for kw in planning_keywords):
            return "planning"
        elif any(kw in desc_lower for kw in research_keywords):
            return "research"

        return "general"