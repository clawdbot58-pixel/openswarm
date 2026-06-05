"""Thinking loop router dispatch table."""

from typing import Any, Type

from .assembler import LoopAssembler
from .base_loop import BaseLoop, LoopResult
from .cot import CoTLoop
from .debate import DebateLoop
from .direct import DirectLoop
from .ensemble import EnsembleLoop
from .graph import LoopGraph
from .model_router import LLMClient
from .reflection import ReflectionLoop
from .registry import LoopRegistry
from .tree import TreeOfThoughtsLoop


LOOPS: dict[str, Type[BaseLoop]] = {
    "direct": DirectLoop,
    "cot": CoTLoop,
    "reflection": ReflectionLoop,
    "tree": TreeOfThoughtsLoop,
    "debate": DebateLoop,
    "ensemble": EnsembleLoop,
}


class LoopRouter:
    """Routes tasks to thinking loops based on configuration.

    Supports both premade loops (Phase 3) and custom graphs (Phase 4).
    """

    def __init__(
        self,
        model_client: LLMClient,
        registry: LoopRegistry | None = None,
        assembler: LoopAssembler | None = None,
    ):
        """Initialize the loop router.

        Args:
            model_client: The LLM client for inference.
            registry: Optional loop template registry for custom graphs.
            assembler: Optional loop assembler for executing custom graphs.
        """
        self.model_client = model_client
        self.registry = registry
        self.assembler = assembler or LoopAssembler()

    def get_loop(self, loop_name: str, **kwargs: Any) -> BaseLoop:
        """Get a thinking loop by name.

        Args:
            loop_name: Name of the loop (direct, cot, reflection, tree, debate, ensemble).
            **kwargs: Additional arguments to pass to the loop constructor.

        Returns:
            The thinking loop instance.

        Raises:
            ValueError: If loop_name is not found.
        """
        if loop_name not in LOOPS:
            raise ValueError(
                f"Unknown loop: {loop_name}. Available: {list(LOOPS.keys())}"
            )

        loop_class = LOOPS[loop_name]

        if loop_name == "tree":
            branch_count = kwargs.get("branch_count", 3)
            return loop_class(self.model_client, branch_count=branch_count)
        elif loop_name == "ensemble":
            models = kwargs.get("models", ["gpt-4o", "claude-sonnet"])
            provider = kwargs.get("provider", "openai")
            return loop_class(models, provider)

        return loop_class(self.model_client)

    def list_available_loops(self) -> list[str]:
        """List all available premade thinking loops.

        Returns:
            List of loop names.
        """
        return list(LOOPS.keys())

    def list_custom_templates(self) -> list[str]:
        """List available custom graph templates from registry.

        Returns:
            List of template IDs.
        """
        if self.registry is None:
            return []

        templates = self.registry.list_templates()
        return [t["id"] for t in templates if not t.get("is_premade", False)]

    async def run(
        self,
        mode: str,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
        **kwargs: Any,
    ) -> LoopResult:
        """Run a thinking loop by mode.

        Args:
            mode: Loop mode (premade name or custom template ID).
            task: The task content.
            preamble: The preamble context.
            model_client: The LLM client.
            **kwargs: Additional configuration.

        Returns:
            LoopResult from loop execution.
        """
        if mode in LOOPS:
            loop = self.get_loop(mode, **kwargs)
            return await loop.run(task, preamble, model_client)

        if self.registry is not None:
            template = self.registry.get_template(mode)
            if template is not None:
                return await self.assembler.execute(
                    template, task, preamble, model_client
                )

        raise ValueError(
            f"Unknown loop mode: {mode}. "
            f"Premade: {list(LOOPS.keys())}. "
            f"Custom templates: {self.list_custom_templates()}"
        )

    async def run_custom_graph(
        self,
        graph: LoopGraph,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute a custom loop graph.

        Args:
            graph: The LoopGraph to execute.
            task: The task content.
            preamble: The preamble context.
            model_client: The LLM client.

        Returns:
            LoopResult from graph execution.
        """
        return await self.assembler.execute(graph, task, preamble, model_client)

    def set_registry(self, registry: LoopRegistry) -> None:
        """Set the loop template registry.

        Args:
            registry: LoopRegistry instance.
        """
        self.registry = registry

    def set_assembler(self, assembler: LoopAssembler) -> None:
        """Set the loop assembler.

        Args:
            assembler: LoopAssembler instance.
        """
        self.assembler = assembler


async def run_loop(
    loop_name: str,
    task: str,
    preamble: dict[str, Any],
    model_client: LLMClient,
    **kwargs: Any,
) -> LoopResult:
    """Convenience function to run a thinking loop.

    Args:
        loop_name: Name of the loop to run.
        task: The task content.
        preamble: The preamble context.
        model_client: The LLM client.
        **kwargs: Additional loop configuration.

    Returns:
        LoopResult from the loop execution.
    """
    router = LoopRouter(model_client)
    return await router.run(loop_name, task, preamble, model_client, **kwargs)


async def run_custom_loop(
    graph: LoopGraph,
    task: str,
    preamble: dict[str, Any],
    model_client: LLMClient,
    registry: LoopRegistry | None = None,
) -> LoopResult:
    """Convenience function to run a custom loop graph.

    Args:
        graph: The LoopGraph to execute.
        task: The task content.
        preamble: The preamble context.
        model_client: The LLM client.
        registry: Optional registry for template storage.

    Returns:
        LoopResult from graph execution.
    """
    assembler = LoopAssembler()
    router = LoopRouter(model_client, registry=registry, assembler=assembler)
    return await router.run_custom_graph(graph, task, preamble, model_client)