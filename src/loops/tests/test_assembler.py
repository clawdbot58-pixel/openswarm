"""Tests for loop assembler."""

import pytest

from loops.assembler import AssemblerError, LoopAssembler, PrebuiltGraphs
from loops.graph import LoopGraph
from loops.model_router import LLMClient


@pytest.fixture
def mock_model_client():
    """Create a mock model client for testing."""
    return LLMClient(models=["gpt-4o-mini"], provider="openai")


@pytest.fixture
def sample_preamble():
    """Create a sample preamble for testing."""
    return {
        "intent": {"goal": "Test task", "phase": "execution"},
        "permissions": {"can_read": ["*"], "can_write": ["*"]},
        "thinking_loop_config": {"mode": "thorough"},
    }


class TestLoopAssembler:
    """Tests for LoopAssembler."""

    @pytest.mark.asyncio
    async def test_execute_direct_graph(self, mock_model_client, sample_preamble):
        """Test executing direct graph matches DirectLoop."""
        graph = LoopGraph.direct_graph()
        assembler = LoopAssembler()

        result = await assembler.execute(graph, "Say hello", sample_preamble, mock_model_client)

        assert result.output
        assert result.tokens_used > 0
        assert result.cost_usd >= 0
        assert result.latency_ms > 0
        assert result.iterations == 1
        assert len(result.intermediate_outputs) == 1

    @pytest.mark.asyncio
    async def test_execute_reflection_graph(self, mock_model_client, sample_preamble):
        """Test executing reflection graph: draft -> critique -> revise."""
        graph = LoopGraph.reflection_graph()
        assembler = LoopAssembler()

        result = await assembler.execute(graph, "Write a poem", sample_preamble, mock_model_client)

        assert result.output
        assert result.iterations == 3
        assert len(result.intermediate_outputs) == 3

        assert result.intermediate_outputs[0]["node_id"] == "draft"
        assert result.intermediate_outputs[1]["node_id"] == "critique"
        assert result.intermediate_outputs[2]["node_id"] == "revise"

    @pytest.mark.asyncio
    async def test_execute_with_invalid_graph_raises(self, mock_model_client, sample_preamble):
        """Test that invalid graph raises GraphValidationError on construction."""
        from loops.graph import GraphValidationError, LoopNode

        with pytest.raises(GraphValidationError, match="not found in nodes"):
            LoopGraph(
                id="bad",
                name="Bad",
                description="Invalid graph",
                nodes=[LoopNode(id="a", primitive="generate")],
                edges=[],
                entry_node="nonexistent",
                terminal_nodes=["a"],
            )

    @pytest.mark.asyncio
    async def test_execute_with_stop_condition(self, mock_model_client, sample_preamble):
        """Test execution stops early when condition met."""
        graph = LoopGraph.reflection_graph()
        graph.stop_conditions = ["iteration > 1"]

        assembler = LoopAssembler()
        result = await assembler.execute(graph, "Test", sample_preamble, mock_model_client)

        assert result.iterations <= 2

    @pytest.mark.asyncio
    async def test_execute_cot_graph(self, mock_model_client, sample_preamble):
        """Test executing chain-of-thought graph."""
        graph = LoopGraph.cot_graph()
        assembler = LoopAssembler()

        result = await assembler.execute(graph, "What is 2+2?", sample_preamble, mock_model_client)

        assert result.output
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_execute_tree_graph(self, mock_model_client, sample_preamble):
        """Test executing tree of thoughts graph."""
        graph = LoopGraph.tree_graph(branch_count=3)
        assembler = LoopAssembler()

        result = await assembler.execute(graph, "Design a house", sample_preamble, mock_model_client)

        assert result.output
        assert len(result.intermediate_outputs) >= 2

    @pytest.mark.asyncio
    async def test_execute_debate_graph(self, mock_model_client, sample_preamble):
        """Test executing debate graph."""
        graph = LoopGraph.debate_graph()
        assembler = LoopAssembler()

        result = await assembler.execute(graph, "Should we use AI?", sample_preamble, mock_model_client)

        assert result.output
        assert len(result.intermediate_outputs) >= 2

    @pytest.mark.asyncio
    async def test_dynamic_assembly_from_vision_example(
        self, mock_model_client, sample_preamble
    ):
        """Dynamic assembly works for the JSON shape in vision/thinking-loops.md.

        Verifies that the ``from``/``to`` shorthand on edges is accepted
        and that the assembled graph runs end-to-end through the assembler.
        """
        graph = LoopGraph.from_dict(
            {
                "id": "draft-check-fix",
                "name": "Dynamic Reflection",
                "description": "Generate → critique → revise",
                "nodes": [
                    {"id": "draft", "primitive": "generate", "model": "gpt-4o-mini"},
                    {"id": "check", "primitive": "critique", "model": "claude-sonnet"},
                    {"id": "fix", "primitive": "revise", "model": "gpt-4o"},
                ],
                "edges": [
                    {"from": "draft", "to": "check"},
                    {"from": "check", "to": "fix"},
                ],
            }
        )
        assembler = LoopAssembler()
        result = await assembler.execute(
            graph, "Write a haiku about recursion", sample_preamble, mock_model_client
        )

        assert result.output
        # All three primitives produced intermediate outputs.
        primitives = [io["primitive"] for io in result.intermediate_outputs]
        assert primitives == ["generate", "critique", "revise"]
        # ``fix`` is the only terminal node and produced the final output.
        assert result.intermediate_outputs[-1]["node_id"] == "fix"

    @pytest.mark.asyncio
    async def test_dynamic_assembly_accepts_canonical_edge_keys(
        self, mock_model_client, sample_preamble
    ):
        """``from_node``/``to_node`` keys still parse (backward compat)."""
        graph = LoopGraph.from_dict(
            {
                "id": "canonical",
                "name": "Canonical edges",
                "description": "use from_node/to_node",
                "nodes": [
                    {"id": "a", "primitive": "generate"},
                    {"id": "b", "primitive": "critique"},
                ],
                "edges": [
                    {"from_node": "a", "to_node": "b", "output_key": "output"},
                ],
            }
        )
        assert graph.get_incoming_edges("b")[0].from_node == "a"
        assembler = LoopAssembler()
        result = await assembler.execute(
            graph, "ping", sample_preamble, mock_model_client
        )
        assert result.output

    def test_evaluate_stop_condition_score(self):
        """Test stop condition evaluation with score."""
        assembler = LoopAssembler()

        from loops.primitives import PrimitiveResult

        node_results = {
            "critique": PrimitiveResult(output="Good critique", score=9.0),
        }

        assert assembler._evaluate_condition("score > 8", node_results, 1) is True
        assert assembler._evaluate_condition("score > 9", node_results, 1) is False

    def test_evaluate_stop_condition_iteration(self):
        """Test stop condition evaluation with iteration."""
        assembler = LoopAssembler()
        node_results = {}

        assert assembler._evaluate_condition("iteration > 5", node_results, 6) is True
        assert assembler._evaluate_condition("iteration > 5", node_results, 5) is False

    def test_evaluate_stop_condition_and_or(self):
        """Test stop condition evaluation with and/or."""
        assembler = LoopAssembler()

        from loops.primitives import PrimitiveResult

        node_results = {
            "node": PrimitiveResult(output="test", score=8.0),
        }

        assert assembler._evaluate_condition("score > 7 and iteration > 1", node_results, 2) is True
        assert assembler._evaluate_condition("score > 7 or iteration > 10", node_results, 2) is True
        assert assembler._evaluate_condition("score > 9 and iteration > 1", node_results, 2) is False

    def test_aggregate_terminal_outputs_single(self):
        """Test aggregating single terminal output."""
        assembler = LoopAssembler()
        graph = LoopGraph.direct_graph()

        from loops.primitives import PrimitiveResult

        node_results = {"generate": PrimitiveResult(output="Output text")}

        result = assembler._aggregate_terminal_outputs(graph, node_results)
        assert result == "Output text"

    def test_aggregate_terminal_outputs_multiple(self):
        """Test aggregating multiple terminal outputs."""
        assembler = LoopAssembler()
        graph = LoopGraph.reflection_graph()

        from loops.primitives import PrimitiveResult

        node_results = {
            "draft": PrimitiveResult(output="Draft"),
            "critique": PrimitiveResult(output="Critique"),
            "revise": PrimitiveResult(output="Final"),
        }

        result = assembler._aggregate_terminal_outputs(graph, node_results)
        assert "Final" in result

    def test_compute_confidence_with_scores(self):
        """Test confidence computation with scores."""
        assembler = LoopAssembler()

        from loops.primitives import PrimitiveResult

        node_results = {
            "a": PrimitiveResult(output="out", score=8.0),
            "b": PrimitiveResult(output="out", score=6.0),
        }

        confidence = assembler._compute_confidence(node_results)
        assert 0.7 <= confidence <= 0.8

    def test_compute_confidence_no_scores(self):
        """Test confidence computation without scores."""
        assembler = LoopAssembler()

        from loops.primitives import PrimitiveResult

        node_results = {
            "a": PrimitiveResult(output="out"),
        }

        confidence = assembler._compute_confidence(node_results)
        assert confidence == 0.7


class TestPrebuiltGraphs:
    """Tests for PrebuiltGraphs convenience class."""

    @pytest.mark.asyncio
    async def test_direct_static(self, mock_model_client, sample_preamble):
        """Test PrebuiltGraphs.direct helper."""
        result = await PrebuiltGraphs.direct("Say hi", sample_preamble, mock_model_client)

        assert result.output
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_reflection_static(self, mock_model_client, sample_preamble):
        """Test PrebuiltGraphs.reflection helper."""
        result = await PrebuiltGraphs.reflection("Write a haiku", sample_preamble, mock_model_client)

        assert result.output
        assert result.iterations == 3

    @pytest.mark.asyncio
    async def test_cot_static(self, mock_model_client, sample_preamble):
        """Test PrebuiltGraphs.cot helper."""
        result = await PrebuiltGraphs.cot("Solve 3x + 5 = 14", sample_preamble, mock_model_client)

        assert result.output
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_tree_static(self, mock_model_client, sample_preamble):
        """Test PrebuiltGraphs.tree helper."""
        result = await PrebuiltGraphs.tree("Pick a color", sample_preamble, mock_model_client, branch_count=3)

        assert result.output


class TestStopConditionError:
    """Tests for StopConditionError."""

    def test_stop_condition_error_message(self):
        """Test StopConditionError has proper message."""
        from loops.assembler import StopConditionError

        error = StopConditionError("score > 8")
        assert "score > 8" in str(error)


class TestAssemblerError:
    """Tests for AssemblerError."""

    def test_assembler_error_message(self):
        """Test AssemblerError has proper message."""
        error = AssemblerError("Graph validation failed")
        assert "Graph validation failed" in str(error)


# ---------------------------------------------------------------------------
# Phase 10: Pydantic LoopGraph + assemble_builtin + execute_graph
# ---------------------------------------------------------------------------


class TestPydanticLoopGraph:
    """The Phase 10 Pydantic :class:`LoopGraph` and its helpers."""

    def _make_graph(self):
        from loops.assembler import LoopGraph
        from loops.primitives import LoopPrimitive, PrimitiveType

        nodes = [
            LoopPrimitive(node_id="a", primitive=PrimitiveType.GENERATE),
            LoopPrimitive(
                node_id="b",
                primitive=PrimitiveType.CRITIQUE,
                temperature=0.3,
            ),
        ]
        from loops.assembler import LoopEdge

        edges = [LoopEdge(from_node="a", to_node="b")]
        return LoopGraph(
            loop_id="g1",
            name="g1",
            description="two nodes",
            nodes=nodes,
            edges=edges,
            terminal_nodes=["b"],
            entry_node="a",
        )

    def test_validate_dag_ok(self):
        g = self._make_graph()
        g.validate_dag()
        assert g.topological_order() == ["a", "b"]

    def test_cycle_raises(self):
        from loops.assembler import LoopEdge, LoopGraph
        from loops.graph import GraphValidationError
        from loops.primitives import LoopPrimitive, PrimitiveType

        nodes = [
            LoopPrimitive(node_id="a", primitive=PrimitiveType.GENERATE),
            LoopPrimitive(node_id="b", primitive=PrimitiveType.GENERATE),
        ]
        edges = [
            LoopEdge(from_node="a", to_node="b"),
            LoopEdge(from_node="b", to_node="a"),
        ]
        with pytest.raises(GraphValidationError):
            LoopGraph(
                loop_id="cycle",
                name="cycle",
                nodes=nodes,
                edges=edges,
                terminal_nodes=["a", "b"],
                entry_node="a",
            )

    def test_duplicate_node_id_raises(self):
        from loops.assembler import LoopGraph
        from loops.graph import GraphValidationError
        from loops.primitives import LoopPrimitive, PrimitiveType

        nodes = [
            LoopPrimitive(node_id="dup", primitive=PrimitiveType.GENERATE),
            LoopPrimitive(node_id="dup", primitive=PrimitiveType.CRITIQUE),
        ]
        with pytest.raises(GraphValidationError):
            LoopGraph(loop_id="x", name="x", nodes=nodes)

    def test_to_from_dict_round_trip(self):
        g = self._make_graph()
        d = g.to_dict()
        assert d["loop_id"] == "g1"
        assert d["nodes"][0]["primitive"] == "generate"
        assert d["edges"][0]["from_node"] == "a"
        from loops.assembler import LoopGraph

        g2 = LoopGraph.from_dict(d)
        assert g2.loop_id == g.loop_id
        assert [n.node_id for n in g2.nodes] == ["a", "b"]

    def test_from_dict_accepts_short_edge_keys(self):
        """Vision spec uses ``from``/``to``; from_dict should tolerate it."""
        d = {
            "loop_id": "short",
            "name": "short",
            "nodes": [
                {"node_id": "a", "primitive": "generate"},
                {"node_id": "b", "primitive": "critique"},
            ],
            "edges": [{"from": "a", "to": "b"}],
            "terminal_nodes": ["b"],
            "entry_node": "a",
        }
        from loops.assembler import LoopGraph

        g = LoopGraph.from_dict(d)
        assert g.edges[0].from_node == "a"
        assert g.edges[0].to_node == "b"

    def test_to_graph_round_trip(self):
        """Pydantic <-> dataclass should round-trip losslessly."""
        g = self._make_graph()
        dc = g.to_graph()
        assert dc.id == g.loop_id
        assert len(dc.nodes) == len(g.nodes)
        from loops.assembler import LoopGraph

        g2 = LoopGraph.from_graph(dc)
        assert [n.node_id for n in g2.nodes] == [n.node_id for n in g.nodes]

    def test_incoming_outgoing_edges(self):
        g = self._make_graph()
        assert [e.to_node for e in g.incoming_edges("b")] == ["b"]
        assert [e.to_node for e in g.incoming_edges("a")] == []
        assert [e.from_node for e in g.outgoing_edges("a")] == ["a"]


class TestAssembleBuiltin:
    """The ``assemble_builtin`` factory for premade loops."""

    @pytest.mark.parametrize(
        "loop_type",
        ["direct", "cot", "reflection", "tree", "debate", "ensemble"],
    )
    def test_assemble_builtin_returns_valid_dag(self, loop_type: str):
        from loops.assembler import LoopAssembler

        g = LoopAssembler().assemble_builtin(loop_type)
        g.validate_dag()
        assert g.loop_id
        assert g.terminal_nodes

    def test_assemble_builtin_unknown_raises(self):
        from loops.assembler import LoopAssembler

        with pytest.raises(ValueError):
            LoopAssembler().assemble_builtin("nope")