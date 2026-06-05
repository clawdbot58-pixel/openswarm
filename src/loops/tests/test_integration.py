"""Integration tests for the complete loop system flow."""

import pytest

from loops.assembler import LoopAssembler
from loops.graph import LoopGraph
from loops.meta_stub import MetaAgentStub
from loops.model_router import LLMClient
from loops.optimizer import LoopOptimizer, CriticScore
from loops.registry import create_registry
from loops.router import LoopRouter


@pytest.fixture
def full_system():
    """Create a full system with registry, optimizer, and router."""
    registry = create_registry(db_path=None)
    optimizer = LoopOptimizer(registry)
    assembler = LoopAssembler()
    model_client = LLMClient(models=["gpt-4o-mini"], provider="openai")

    return {
        "registry": registry,
        "optimizer": optimizer,
        "assembler": assembler,
        "model_client": model_client,
        "router": LoopRouter(model_client, registry=registry, assembler=assembler),
    }


@pytest.fixture
def sample_preamble():
    """Sample preamble for testing."""
    return {
        "intent": {"goal": "Write a Python function", "phase": "execution"},
        "permissions": {"can_read": ["*"], "can_write": ["*"]},
        "thinking_loop_config": {"mode": "thorough"},
    }


class TestFullFlowIntegration:
    """Integration tests for complete flows."""

    @pytest.mark.asyncio
    async def test_task_propose_execute_score_recommend(self, full_system, sample_preamble):
        """Full flow: task -> propose loop -> execute -> score -> recommend."""
        task = "Write a Python function that calculates fibonacci numbers"
        task_type = "coding"

        meta = MetaAgentStub()
        graph = await meta.propose_loop(task, task_type)

        assert graph is not None
        assert graph.id is not None
        assert len(graph.nodes) > 0

        result = await full_system["assembler"].execute(
            graph, task, sample_preamble, full_system["model_client"]
        )

        assert result.output
        assert result.iterations >= 1
        assert result.tokens_used > 0

    @pytest.mark.asyncio
    async def test_execute_then_record_then_recommend(self, full_system, sample_preamble):
        """Execute a loop, record result, then get recommendations."""
        task = "Debug this code: print('hello'"
        task_type = "coding"

        graph = LoopGraph.reflection_graph()
        result = await full_system["assembler"].execute(
            graph, task, sample_preamble, full_system["model_client"]
        )

        critic_score = CriticScore(
            score=7.5,
            critique="Good reflection loop execution",
            confidence=0.85,
        )

        await full_system["optimizer"].record_result(graph.id, result, critic_score)

        recs = full_system["optimizer"].recommend(task_type, min_score=5.0)

        assert len(recs) >= 1
        assert any(r.template_id == graph.id for r in recs)

    @pytest.mark.asyncio
    async def test_router_with_premade_loop(self, full_system, sample_preamble):
        """Test router runs premade loop correctly."""
        result = await full_system["router"].run(
            "direct", "Say hello", sample_preamble, full_system["model_client"]
        )

        assert result.output
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_router_with_custom_graph(self, full_system, sample_preamble):
        """Test router runs custom graph correctly."""
        graph = LoopGraph.reflection_graph()

        result = await full_system["router"].run_custom_graph(
            graph, "Write a haiku", sample_preamble, full_system["model_client"]
        )

        assert result.output
        assert result.iterations == 3

    def test_meta_stub_proposes_correct_graph_types(self):
        """Test meta agent proposes correct graph types for task types."""
        meta = MetaAgentStub()

        coding_graph = meta.get_graph_for_task("Implement a class", "coding")
        assert "reflection" in coding_graph.id

        design_graph = meta.get_graph_for_task("Design a system", "design")
        assert "tree" in design_graph.id

        research_graph = meta.get_graph_for_task("Find information", "research")
        assert "cot" in research_graph.id

        general_graph = meta.get_graph_for_task("Do something", "general")
        assert "direct" in general_graph.id

    def test_registry_has_premade_templates(self, full_system):
        """Test registry contains all premade templates."""
        expected = ["direct", "cot", "reflection", "tree", "debate", "ensemble"]

        for template_id in expected:
            template = full_system["registry"].get_template(template_id)
            assert template is not None, f"Missing template: {template_id}"

    def test_loop_comparison(self, full_system):
        """Test comparing two loops."""
        full_system["registry"].update_stats(
            "direct", score=7.0, cost=0.001, latency=100, success=True
        )
        full_system["registry"].update_stats(
            "reflection", score=8.0, cost=0.003, latency=300, success=True
        )

        comparison = full_system["optimizer"].compare_templates("direct", "reflection")

        assert comparison["template_a"] == "direct"
        assert comparison["template_b"] == "reflection"
        assert comparison["winner"] in ["direct", "reflection"]


class TestGraphSerializationRoundTrip:
    """Test graph serialization and deserialization."""

    def test_graph_to_dict_and_back(self):
        """Test graph can be serialized to dict and back."""
        original = LoopGraph.reflection_graph("test-graph")

        as_dict = original.to_dict()
        restored = LoopGraph.from_dict(as_dict)

        assert restored.id == original.id
        assert restored.name == original.name
        assert len(restored.nodes) == len(original.nodes)
        assert len(restored.edges) == len(original.edges)
        assert restored.entry_node == original.entry_node
        assert restored.terminal_nodes == original.terminal_nodes

    def test_custom_graph_round_trip(self):
        """Test custom graph survives round trip."""
        from loops.graph import LoopNode, LoopEdge

        original = LoopGraph(
            id="my-custom-loop",
            name="My Custom Loop",
            description="A custom loop for testing",
            nodes=[
                LoopNode(id="start", primitive="generate"),
                LoopNode(id="check", primitive="critique"),
                LoopNode(id="fix", primitive="revise"),
            ],
            edges=[
                LoopEdge(from_node="start", to_node="check"),
                LoopEdge(from_node="check", to_node="fix"),
            ],
            stop_conditions=["score > 8"],
            entry_node="start",
            terminal_nodes=["fix"],
        )

        as_dict = original.to_dict()
        restored = LoopGraph.from_dict(as_dict)

        assert restored.id == "my-custom-loop"
        assert restored.stop_conditions == ["score > 8"]
        assert len(restored.nodes) == 3
        assert len(restored.edges) == 2


class TestPerformanceTracking:
    """Test performance tracking across multiple runs."""

    @pytest.mark.asyncio
    async def test_multiple_runs_update_stats(self, full_system, sample_preamble):
        """Test multiple runs update template statistics."""
        template_id = "direct"

        initial_stats = full_system["registry"].get_stats(template_id)
        initial_count = initial_stats.usage_count if initial_stats else 0

        for i in range(3):
            graph = LoopGraph.direct_graph()
            result = await full_system["assembler"].execute(
                graph, f"Task {i}", sample_preamble, full_system["model_client"]
            )

            await full_system["optimizer"].record_result(
                template_id,
                result,
                CriticScore(score=6.0 + i, critique=f"Run {i}"),
            )

        updated_stats = full_system["registry"].get_stats(template_id)
        assert updated_stats is not None
        assert updated_stats.usage_count == initial_count + 3

    def test_stats_reflect_actual_performance(self, full_system):
        """Test that running averages reflect actual performance."""
        template_id = "cot"

        scores = [5.0, 7.0, 9.0]
        costs = [0.001, 0.002, 0.003]
        latencies = [100, 200, 300]

        for score, cost, latency in zip(scores, costs, latencies):
            full_system["registry"].update_stats(
                template_id, score=score, cost=cost, latency=latency, success=True
            )

        stats = full_system["registry"].get_stats(template_id)
        assert stats is not None
        assert stats.usage_count == 3
        assert 5.0 <= stats.avg_score <= 9.0
        assert 0.001 <= stats.avg_cost_usd <= 0.003


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_task_still_produces_output(self, full_system, sample_preamble):
        """Test that empty task still produces some output."""
        graph = LoopGraph.direct_graph()
        result = await full_system["assembler"].execute(
            graph, "", sample_preamble, full_system["model_client"]
        )

        assert result.output is not None

    @pytest.mark.asyncio
    async def test_very_long_task_handled(self, full_system, sample_preamble):
        """Test that very long task is handled."""
        long_task = "Write a comprehensive report. " * 100

        graph = LoopGraph.direct_graph()
        result = await full_system["assembler"].execute(
            graph, long_task, sample_preamble, full_system["model_client"]
        )

        assert result.output is not None

    def test_meta_stub_infers_task_type(self):
        """Test meta agent infers task type from description."""
        meta = MetaAgentStub()

        assert meta._infer_task_type("write a function") == "coding"
        assert meta._infer_task_type("please review this") == "review"
        assert meta._infer_task_type("design an architecture") == "design"
        assert meta._infer_task_type("find information") == "research"
        assert meta._infer_task_type("make a plan") == "planning"

    def test_premade_graphs_all_valid(self):
        """Test all premade graphs are valid DAGs."""
        graphs = [
            LoopGraph.direct_graph(),
            LoopGraph.cot_graph(),
            LoopGraph.reflection_graph(),
            LoopGraph.tree_graph(),
            LoopGraph.debate_graph(),
            LoopGraph.ensemble_graph(),
        ]

        for graph in graphs:
            graph.validate()
            assert len(graph.nodes) > 0
            assert graph.entry_node
            assert len(graph.terminal_nodes) > 0