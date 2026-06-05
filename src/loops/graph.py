"""Loop graph definition - JSON-serializable graph format for custom loops."""

from dataclasses import dataclass, field
from typing import Any


VALID_PRIMITIVES = {"generate", "critique", "vote", "revise", "branch", "merge"}


class GraphValidationError(Exception):
    """Raised when a loop graph fails validation."""
    pass


@dataclass
class LoopNode:
    """A node in a loop graph.

    Attributes:
        id: Unique identifier within the graph.
        primitive: The primitive type (generate, critique, vote, revise, branch, merge).
        model: Optional model override for this node.
        config: Node-specific configuration (temperature, n branches, etc.).
        prompt_template: Optional Jinja2 template for input rendering.
    """
    id: str
    primitive: str
    model: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    prompt_template: str | None = None

    def __post_init__(self) -> None:
        """Validate node after initialization.

        Raises:
            GraphValidationError: If primitive is invalid.
        """
        if self.primitive not in VALID_PRIMITIVES:
            raise GraphValidationError(
                f"Invalid primitive '{self.primitive}' in node '{self.id}'. "
                f"Must be one of: {VALID_PRIMITIVES}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize node to dictionary.

        Returns:
            Dictionary representation of the node.
        """
        result: dict[str, Any] = {
            "id": self.id,
            "primitive": self.primitive,
        }
        if self.model:
            result["model"] = self.model
        if self.config:
            result["config"] = self.config
        if self.prompt_template:
            result["prompt_template"] = self.prompt_template
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopNode":
        """Deserialize node from dictionary.

        Args:
            data: Dictionary containing node data.

        Returns:
            LoopNode instance.
        """
        return cls(
            id=data["id"],
            primitive=data["primitive"],
            model=data.get("model"),
            config=data.get("config", {}),
            prompt_template=data.get("prompt_template"),
        )


@dataclass
class LoopEdge:
    """An edge connecting two nodes in a loop graph.

    Attributes:
        from_node: Source node ID.
        to_node: Target node ID.
        output_key: Which field of PrimitiveResult to pass (default "output").
    """
    from_node: str
    to_node: str
    output_key: str = "output"

    def to_dict(self) -> dict[str, Any]:
        """Serialize edge to dictionary.

        Returns:
            Dictionary representation of the edge.
        """
        result: dict[str, Any] = {
            "from_node": self.from_node,
            "to_node": self.to_node,
        }
        if self.output_key != "output":
            result["output_key"] = self.output_key
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopEdge":
        """Deserialize edge from dictionary.

        Accepts both the canonical ``from_node`` / ``to_node`` keys and
        the shorthand ``from`` / ``to`` keys used in the dynamic-assembly
        examples in ``vision/thinking-loops.md``.

        Args:
            data: Dictionary containing edge data.

        Returns:
            LoopEdge instance.
        """
        from_node = data.get("from_node", data.get("from"))
        to_node = data.get("to_node", data.get("to"))
        if from_node is None or to_node is None:
            raise KeyError(
                "edge dict must contain either 'from_node'/'to_node' "
                "or 'from'/'to' keys"
            )
        return cls(
            from_node=from_node,
            to_node=to_node,
            output_key=data.get("output_key", "output"),
        )


@dataclass
class LoopGraph:
    """A complete loop graph definition.

    Attributes:
        id: Template ID (unique identifier).
        name: Human-readable name.
        description: Description of what this loop does.
        nodes: List of nodes in the graph.
        edges: List of edges connecting nodes.
        stop_conditions: List of stop condition expressions (e.g. "score > 8").
        entry_node: Starting node ID.
        terminal_nodes: Node IDs that produce loop output.
    """
    id: str
    name: str
    description: str
    nodes: list[LoopNode] = field(default_factory=list)
    edges: list[LoopEdge] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    entry_node: str = ""
    terminal_nodes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate graph after initialization."""
        self.validate()

    def validate(self) -> None:
        """Validate the graph structure.

        Validates:
        - Graph is a DAG (no cycles)
        - All node IDs are unique
        - All edge references point to existing nodes
        - Entry node exists
        - At least one terminal node exists
        - All nodes are reachable from entry node

        Raises:
            GraphValidationError: If any validation fails.
        """
        self._validate_unique_nodes()
        self._validate_edge_references()
        self._validate_entry_node()
        self._validate_terminal_nodes()
        self._validate_dag()
        self._validate_reachability()

    def _validate_unique_nodes(self) -> None:
        """Check that all node IDs are unique.

        Raises:
            GraphValidationError: If duplicate node IDs exist.
        """
        node_ids = [n.id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            duplicates = [id for id in node_ids if node_ids.count(id) > 1]
            raise GraphValidationError(f"Duplicate node IDs found: {set(duplicates)}")

    def _validate_edge_references(self) -> None:
        """Check that all edge references point to existing nodes.

        Raises:
            GraphValidationError: If edge references non-existent node.
        """
        node_ids = {n.id for n in self.nodes}
        for edge in self.edges:
            if edge.from_node not in node_ids:
                raise GraphValidationError(
                    f"Edge references non-existent from_node '{edge.from_node}'"
                )
            if edge.to_node not in node_ids:
                raise GraphValidationError(
                    f"Edge references non-existent to_node '{edge.to_node}'"
                )

    def _validate_entry_node(self) -> None:
        """Check that entry node exists.

        Raises:
            GraphValidationError: If entry node doesn't exist or not set.
        """
        if not self.entry_node:
            raise GraphValidationError("Entry node not set")
        node_ids = {n.id for n in self.nodes}
        if self.entry_node not in node_ids:
            raise GraphValidationError(f"Entry node '{self.entry_node}' not found in nodes")

    def _validate_terminal_nodes(self) -> None:
        """Check that at least one terminal node exists.

        Raises:
            GraphValidationError: If no terminal nodes or any don't exist.
        """
        if not self.terminal_nodes:
            raise GraphValidationError("No terminal nodes defined")
        node_ids = {n.id for n in self.nodes}
        for tn in self.terminal_nodes:
            if tn not in node_ids:
                raise GraphValidationError(f"Terminal node '{tn}' not found in nodes")

    def _validate_dag(self) -> None:
        """Check that graph is a DAG (no cycles).

        Uses topological sort to detect cycles.

        Raises:
            GraphValidationError: If cycle detected.
        """
        in_degree: dict[str, int] = {n.id: 0 for n in self.nodes}
        adjacency: dict[str, list[str]] = {n.id: [] for n in self.nodes}

        for edge in self.edges:
            in_degree[edge.to_node] += 1
            adjacency[edge.from_node].append(edge.to_node)

        queue: list[str] = [nid for nid, deg in in_degree.items() if deg == 0]
        visited_count = 0

        while queue:
            node_id = queue.pop(0)
            visited_count += 1
            for neighbor in adjacency[node_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(self.nodes):
            raise GraphValidationError("Cycle detected in graph - not a valid DAG")

    def _validate_reachability(self) -> None:
        """Check that all nodes are reachable from entry node.

        Uses BFS to check reachability.

        Raises:
            GraphValidationError: If any node is unreachable from entry.
        """
        adjacency: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for edge in self.edges:
            adjacency[edge.from_node].append(edge.to_node)

        visited: set[str] = {self.entry_node}
        queue = [self.entry_node]

        while queue:
            node_id = queue.pop(0)
            for neighbor in adjacency[node_id]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        unreachable = [n.id for n in self.nodes if n.id not in visited]
        if unreachable:
            raise GraphValidationError(
                f"Nodes not reachable from entry '{self.entry_node}': {unreachable}"
            )

    def topological_sort(self) -> list[str]:
        """Get nodes in topological order.

        Returns:
            List of node IDs in topological order.

        Raises:
            GraphValidationError: If graph has cycles.
        """
        in_degree: dict[str, int] = {n.id: 0 for n in self.nodes}
        adjacency: dict[str, list[str]] = {n.id: [] for n in self.nodes}

        for edge in self.edges:
            in_degree[edge.to_node] += 1
            adjacency[edge.from_node].append(edge.to_node)

        queue: list[str] = [nid for nid, deg in in_degree.items() if deg == 0]
        result: list[str] = []

        while queue:
            node_id = queue.pop(0)
            result.append(node_id)
            for neighbor in adjacency[node_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self.nodes):
            raise GraphValidationError("Cannot compute topological sort - graph has cycles")

        return result

    def get_node(self, node_id: str) -> LoopNode | None:
        """Get a node by ID.

        Args:
            node_id: The node ID to find.

        Returns:
            The node or None if not found.
        """
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_incoming_edges(self, node_id: str) -> list[LoopEdge]:
        """Get edges that point to a node.

        Args:
            node_id: Target node ID.

        Returns:
            List of incoming edges.
        """
        return [e for e in self.edges if e.to_node == node_id]

    def get_outgoing_edges(self, node_id: str) -> list[LoopEdge]:
        """Get edges that originate from a node.

        Args:
            node_id: Source node ID.

        Returns:
            List of outgoing edges.
        """
        return [e for e in self.edges if e.from_node == node_id]

    def to_dict(self) -> dict[str, Any]:
        """Serialize graph to dictionary.

        Returns:
            Dictionary representation of the graph.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "stop_conditions": self.stop_conditions,
            "entry_node": self.entry_node,
            "terminal_nodes": self.terminal_nodes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopGraph":
        """Deserialize graph from dictionary.

        When the input omits ``entry_node`` and/or ``terminal_nodes``
        (the typical case for dynamic assembly, see
        ``vision/thinking-loops.md``), the missing values are derived:

        * ``entry_node`` defaults to the first node by id-order.
        * ``terminal_nodes`` defaults to nodes with no outgoing edges.

        Args:
            data: Dictionary containing graph data.

        Returns:
            LoopGraph instance.
        """
        nodes = [LoopNode.from_dict(n) for n in data.get("nodes", [])]
        edges = [LoopEdge.from_dict(e) for e in data.get("edges", [])]

        entry_node = data.get("entry_node") or (nodes[0].id if nodes else "")
        has_outgoing = {e.from_node for e in edges}
        if "terminal_nodes" in data and data["terminal_nodes"]:
            terminal_nodes = list(data["terminal_nodes"])
        else:
            terminal_nodes = [n.id for n in nodes if n.id not in has_outgoing]

        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            nodes=nodes,
            edges=edges,
            stop_conditions=data.get("stop_conditions", []),
            entry_node=entry_node,
            terminal_nodes=terminal_nodes,
        )

    @classmethod
    def direct_graph(cls, graph_id: str = "direct") -> "LoopGraph":
        """Create a simple direct loop graph.

        Args:
            graph_id: The graph ID.

        Returns:
            LoopGraph with single generate node.
        """
        node = LoopNode(id="generate", primitive="generate")
        return cls(
            id=graph_id,
            name="Direct Loop",
            description="Single generate call",
            nodes=[node],
            edges=[],
            entry_node="generate",
            terminal_nodes=["generate"],
        )

    @classmethod
    def cot_graph(cls, graph_id: str = "cot") -> "LoopGraph":
        """Create a chain-of-thought graph.

        Args:
            graph_id: The graph ID.

        Returns:
            LoopGraph with generate node.
        """
        node = LoopNode(
            id="generate",
            primitive="generate",
            prompt_template="Think step by step. {{ task }}",
        )
        return cls(
            id=graph_id,
            name="Chain-of-Thought",
            description="Single call with CoT reasoning",
            nodes=[node],
            edges=[],
            entry_node="generate",
            terminal_nodes=["generate"],
        )

    @classmethod
    def reflection_graph(cls, graph_id: str = "reflection") -> "LoopGraph":
        """Create a reflection loop graph.

        generate -> critique -> revise

        Args:
            graph_id: The graph ID.

        Returns:
            LoopGraph with draft, critique, revise nodes.
        """
        nodes = [
            LoopNode(id="draft", primitive="generate"),
            LoopNode(id="critique", primitive="critique"),
            LoopNode(id="revise", primitive="revise"),
        ]
        edges = [
            LoopEdge(from_node="draft", to_node="critique"),
            LoopEdge(from_node="critique", to_node="revise"),
        ]
        return cls(
            id=graph_id,
            name="Reflection Loop",
            description="Draft -> Critique -> Revise",
            nodes=nodes,
            edges=edges,
            entry_node="draft",
            terminal_nodes=["revise"],
        )

    @classmethod
    def tree_graph(cls, graph_id: str = "tree", branch_count: int = 3) -> "LoopGraph":
        """Create a tree of thoughts graph.

        branch(N) -> vote -> merge

        Args:
            graph_id: The graph ID.
            branch_count: Number of branches to generate.

        Returns:
            LoopGraph with branch, vote, merge nodes.
        """
        nodes = [
            LoopNode(id="branch", primitive="branch", config={"n": branch_count}),
            LoopNode(id="vote", primitive="vote"),
            LoopNode(id="merge", primitive="merge"),
        ]
        edges = [
            LoopEdge(from_node="branch", to_node="vote"),
            LoopEdge(from_node="vote", to_node="merge"),
        ]
        return cls(
            id=graph_id,
            name="Tree of Thoughts",
            description=f"Branch({branch_count}) -> Vote -> Merge",
            nodes=nodes,
            edges=edges,
            entry_node="branch",
            terminal_nodes=["merge"],
        )

    @classmethod
    def debate_graph(cls, graph_id: str = "debate") -> "LoopGraph":
        """Create a debate loop graph.

        entry -> branch -> (argue_for + argue_against) -> vote

        The branch node triggers parallel generation of FOR and AGAINST arguments.

        Args:
            graph_id: The graph ID.

        Returns:
            LoopGraph with entry, branch, argue nodes and vote.
        """
        nodes = [
            LoopNode(id="entry", primitive="generate"),
            LoopNode(
                id="branch",
                primitive="branch",
                config={"n": 2},
                prompt_template="{{ task }}",
            ),
            LoopNode(id="vote", primitive="vote"),
        ]
        edges = [
            LoopEdge(from_node="entry", to_node="branch"),
            LoopEdge(from_node="branch", to_node="vote", output_key="candidates"),
        ]
        return cls(
            id=graph_id,
            name="Debate Loop",
            description="Entry -> Branch -> Vote (parallel FOR/AGAINST)",
            nodes=nodes,
            edges=edges,
            entry_node="entry",
            terminal_nodes=["vote"],
        )

    @classmethod
    def ensemble_graph(
        cls,
        graph_id: str = "ensemble",
        models: list[str] | None = None,
    ) -> "LoopGraph":
        """Create an ensemble loop graph.

        entry -> branch (N models) -> vote

        The branch node generates outputs from multiple models in parallel.

        Args:
            graph_id: The graph ID.
            models: List of model names to use.

        Returns:
            LoopGraph with entry, branch and vote nodes.
        """
        if models is None:
            models = ["gpt-4o", "claude-sonnet"]

        branch_count = len(models)

        nodes = [
            LoopNode(id="entry", primitive="generate"),
            LoopNode(
                id="branch",
                primitive="branch",
                config={"n": branch_count},
            ),
            LoopNode(id="vote", primitive="vote"),
        ]

        edges = [
            LoopEdge(from_node="entry", to_node="branch"),
            LoopEdge(from_node="branch", to_node="vote", output_key="candidates"),
        ]

        return cls(
            id=graph_id,
            name="Ensemble Loop",
            description=f"Entry -> Branch({branch_count}) -> Vote",
            nodes=nodes,
            edges=edges,
            entry_node="entry",
            terminal_nodes=["vote"],
        )