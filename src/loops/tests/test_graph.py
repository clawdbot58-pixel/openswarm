"""Tests for loop graph definition and validation."""

import pytest

from loops.graph import (
    GraphValidationError,
    LoopEdge,
    LoopGraph,
    LoopNode,
    VALID_PRIMITIVES,
)


class TestLoopNode:
    """Tests for LoopNode."""

    def test_node_creation(self):
        """Test LoopNode can be created."""
        node = LoopNode(id="test-node", primitive="generate")
        assert node.id == "test-node"
        assert node.primitive == "generate"
        assert node.model is None
        assert node.config == {}
        assert node.prompt_template is None

    def test_node_with_all_fields(self):
        """Test LoopNode with all optional fields."""
        node = LoopNode(
            id="test-node",
            primitive="critique",
            model="gpt-4o",
            config={"temperature": 0.3},
            prompt_template="Critique: {{ task }}",
        )
        assert node.id == "test-node"
        assert node.primitive == "critique"
        assert node.model == "gpt-4o"
        assert node.config == {"temperature": 0.3}
        assert node.prompt_template == "Critique: {{ task }}"

    def test_node_invalid_primitive_raises(self):
        """Test that invalid primitive raises GraphValidationError."""
        with pytest.raises(GraphValidationError):
            LoopNode(id="test", primitive="invalid")

    def test_node_to_dict(self):
        """Test node serialization to dict."""
        node = LoopNode(id="test", primitive="generate", model="gpt-4o-mini")
        d = node.to_dict()

        assert d["id"] == "test"
        assert d["primitive"] == "generate"
        assert d["model"] == "gpt-4o-mini"

    def test_node_from_dict(self):
        """Test node deserialization from dict."""
        data = {
            "id": "test",
            "primitive": "vote",
            "model": "claude-sonnet",
            "config": {"temperature": 0.5},
        }
        node = LoopNode.from_dict(data)

        assert node.id == "test"
        assert node.primitive == "vote"
        assert node.model == "claude-sonnet"
        assert node.config == {"temperature": 0.5}


class TestLoopEdge:
    """Tests for LoopEdge."""

    def test_edge_creation(self):
        """Test LoopEdge can be created."""
        edge = LoopEdge(from_node="node1", to_node="node2")
        assert edge.from_node == "node1"
        assert edge.to_node == "node2"
        assert edge.output_key == "output"

    def test_edge_with_custom_output_key(self):
        """Test LoopEdge with custom output key."""
        edge = LoopEdge(
            from_node="node1",
            to_node="node2",
            output_key="score",
        )
        assert edge.output_key == "score"

    def test_edge_to_dict(self):
        """Test edge serialization to dict."""
        edge = LoopEdge(from_node="a", to_node="b", output_key="candidates")
        d = edge.to_dict()

        assert d["from_node"] == "a"
        assert d["to_node"] == "b"
        assert d["output_key"] == "candidates"

    def test_edge_from_dict(self):
        """Test edge deserialization from dict."""
        data = {"from_node": "x", "to_node": "y", "output_key": "score"}
        edge = LoopEdge.from_dict(data)

        assert edge.from_node == "x"
        assert edge.to_node == "y"
        assert edge.output_key == "score"


class TestLoopGraph:
    """Tests for LoopGraph."""

    def test_valid_direct_graph(self):
        """Test valid direct graph passes validation."""
        graph = LoopGraph.direct_graph("test-direct")
        assert graph.id == "test-direct"
        assert len(graph.nodes) == 1
        assert graph.entry_node == "generate"
        assert graph.terminal_nodes == ["generate"]

    def test_valid_reflection_graph(self):
        """Test valid reflection graph passes validation."""
        graph = LoopGraph.reflection_graph("test-reflection")
        assert graph.id == "test-reflection"
        assert len(graph.nodes) == 3
        assert graph.entry_node == "draft"

    def test_valid_tree_graph(self):
        """Test valid tree graph passes validation."""
        graph = LoopGraph.tree_graph("test-tree", branch_count=5)
        assert graph.id == "test-tree"
        assert len(graph.nodes) == 3
        assert graph.entry_node == "branch"

    def test_valid_debate_graph(self):
        """Test valid debate graph passes validation."""
        graph = LoopGraph.debate_graph("test-debate")
        assert graph.id == "test-debate"
        assert len(graph.nodes) == 3

    def test_valid_ensemble_graph(self):
        """Test valid ensemble graph passes validation."""
        graph = LoopGraph.ensemble_graph("test-ensemble")
        assert graph.id == "test-ensemble"
        assert len(graph.nodes) == 3

    def test_duplicate_node_ids_raises(self):
        """Test that duplicate node IDs raise GraphValidationError."""
        nodes = [
            LoopNode(id="same", primitive="generate"),
            LoopNode(id="same", primitive="critique"),
        ]
        with pytest.raises(GraphValidationError, match="Duplicate node IDs"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=nodes,
                edges=[],
                entry_node="same",
                terminal_nodes=["same"],
            )

    def test_missing_entry_node_raises(self):
        """Test that missing entry node raises GraphValidationError."""
        with pytest.raises(GraphValidationError, match="not found in nodes"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=[LoopNode(id="node", primitive="generate")],
                edges=[],
                entry_node="nonexistent",
                terminal_nodes=["node"],
            )

    def test_missing_terminal_node_raises(self):
        """Test that missing terminal node raises GraphValidationError."""
        with pytest.raises(GraphValidationError, match="No terminal nodes"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=[LoopNode(id="node", primitive="generate")],
                edges=[],
                entry_node="node",
                terminal_nodes=[],
            )

    def test_edge_to_nonexistent_node_raises(self):
        """Test that edge to nonexistent node raises GraphValidationError."""
        with pytest.raises(GraphValidationError, match="non-existent to_node"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=[LoopNode(id="node", primitive="generate")],
                edges=[LoopEdge(from_node="node", to_node="ghost")],
                entry_node="node",
                terminal_nodes=["node"],
            )

    def test_cycle_detection_raises(self):
        """Test that cycle in graph raises GraphValidationError."""
        nodes = [
            LoopNode(id="a", primitive="generate"),
            LoopNode(id="b", primitive="critique"),
            LoopNode(id="c", primitive="revise"),
        ]
        edges = [
            LoopEdge(from_node="a", to_node="b"),
            LoopEdge(from_node="b", to_node="c"),
            LoopEdge(from_node="c", to_node="a"),
        ]
        with pytest.raises(GraphValidationError, match="Cycle detected"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=nodes,
                edges=edges,
                entry_node="a",
                terminal_nodes=["c"],
            )

    def test_unreachable_node_raises(self):
        """Test that unreachable node raises GraphValidationError."""
        nodes = [
            LoopNode(id="a", primitive="generate"),
            LoopNode(id="b", primitive="critique"),
        ]
        edges = []
        with pytest.raises(GraphValidationError, match="not reachable"):
            LoopGraph(
                id="test",
                name="Test",
                description="Test",
                nodes=nodes,
                edges=edges,
                entry_node="a",
                terminal_nodes=["b"],
            )

    def test_topological_sort(self):
        """Test topological sort returns correct order."""
        graph = LoopGraph.reflection_graph("test")
        order = graph.topological_sort()

        assert order.index("draft") < order.index("critique")
        assert order.index("critique") < order.index("revise")

    def test_get_node(self):
        """Test getting a node by ID."""
        graph = LoopGraph.reflection_graph()
        node = graph.get_node("draft")

        assert node is not None
        assert node.id == "draft"
        assert node.primitive == "generate"

    def test_get_node_not_found(self):
        """Test getting nonexistent node returns None."""
        graph = LoopGraph.direct_graph()
        node = graph.get_node("nonexistent")

        assert node is None

    def test_get_incoming_edges(self):
        """Test getting incoming edges for a node."""
        graph = LoopGraph.reflection_graph()
        edges = graph.get_incoming_edges("critique")

        assert len(edges) == 1
        assert edges[0].from_node == "draft"

    def test_get_outgoing_edges(self):
        """Test getting outgoing edges from a node."""
        graph = LoopGraph.reflection_graph()
        edges = graph.get_outgoing_edges("draft")

        assert len(edges) == 1
        assert edges[0].to_node == "critique"

    def test_to_dict(self):
        """Test graph serialization to dict."""
        graph = LoopGraph.direct_graph("test")
        d = graph.to_dict()

        assert d["id"] == "test"
        assert d["name"] == "Direct Loop"
        assert len(d["nodes"]) == 1
        assert len(d["edges"]) == 0

    def test_from_dict(self):
        """Test graph deserialization from dict."""
        data = {
            "id": "custom",
            "name": "Custom Graph",
            "description": "A custom graph",
            "nodes": [{"id": "gen", "primitive": "generate"}],
            "edges": [],
            "stop_conditions": ["score > 8"],
            "entry_node": "gen",
            "terminal_nodes": ["gen"],
        }
        graph = LoopGraph.from_dict(data)

        assert graph.id == "custom"
        assert graph.name == "Custom Graph"
        assert len(graph.nodes) == 1
        assert graph.stop_conditions == ["score > 8"]


class TestValidPrimitives:
    """Tests for VALID_PRIMITIVES constant."""

    def test_valid_primitives_includes_all_expected(self):
        """Test that VALID_PRIMITIVES includes expected values."""
        assert "generate" in VALID_PRIMITIVES
        assert "critique" in VALID_PRIMITIVES
        assert "vote" in VALID_PRIMITIVES
        assert "revise" in VALID_PRIMITIVES
        assert "branch" in VALID_PRIMITIVES
        assert "merge" in VALID_PRIMITIVES

    def test_valid_primitives_count(self):
        """Test that there are exactly 6 primitives."""
        assert len(VALID_PRIMITIVES) == 6