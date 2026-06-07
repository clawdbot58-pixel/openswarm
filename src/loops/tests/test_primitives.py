"""Tests for loop primitives."""

import pytest

from loops.model_router import LLMClient
from loops.primitives import (
    BranchPrimitive,
    CritiquePrimitive,
    GeneratePrimitive,
    MergePrimitive,
    PrimitiveContext,
    PrimitiveResult,
    RevisePrimitive,
    VotePrimitive,
    get_primitive,
)


@pytest.fixture
def mock_model_client():
    """Create a mock model client for testing."""
    return LLMClient(models=["gpt-4o-mini"], provider="openai")


@pytest.fixture
def basic_context(mock_model_client):
    """Create a basic primitive context."""
    return PrimitiveContext(
        task="Test task",
        model_client=mock_model_client,
        system_prompt="# ROLE\nTest agent",
        inputs={},
        config={},
        metadata={},
    )


class TestGeneratePrimitive:
    """Tests for GeneratePrimitive."""

    @pytest.mark.asyncio
    async def test_generate_with_prompt_in_inputs(self, basic_context):
        """Test generate with prompt in inputs."""
        basic_context.inputs["prompt"] = "Write hello world"

        primitive = GeneratePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.tokens_used > 0
        assert result.cost_usd >= 0
        assert result.latency_ms >= 0
        assert "model_used" in result.metadata

    @pytest.mark.asyncio
    async def test_generate_with_prompt_in_config(self, mock_model_client):
        """Test generate with prompt in config."""
        context = PrimitiveContext(
            task="Test task",
            model_client=mock_model_client,
            system_prompt="# ROLE\nTest agent",
            inputs={},
            config={"prompt": "Say goodbye"},
            metadata={},
        )

        primitive = GeneratePrimitive()
        result = await primitive.execute(context)

        assert isinstance(result, PrimitiveResult)
        assert result.output

    @pytest.mark.asyncio
    async def test_generate_with_model_override(self, basic_context):
        """Test generate with model override."""
        basic_context.inputs["prompt"] = "Test prompt"
        basic_context.config["model"] = "gpt-4o"

        primitive = GeneratePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.metadata.get("model_used") == "gpt-4o"


class TestCritiquePrimitive:
    """Tests for CritiquePrimitive."""

    @pytest.mark.asyncio
    async def test_critique_with_target_in_inputs(self, basic_context):
        """Test critique with target output in inputs."""
        basic_context.inputs["target"] = "This is my output to critique"
        basic_context.inputs["rubric"] = "Evaluate clarity and correctness"

        primitive = CritiquePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.score is not None
        assert 1.0 <= result.score <= 10.0

    @pytest.mark.asyncio
    async def test_critique_with_target_in_config(self, mock_model_client):
        """Test critique with target output in config."""
        context = PrimitiveContext(
            task="Test task",
            model_client=mock_model_client,
            system_prompt="# ROLE\nTest agent",
            inputs={},
            config={
                "target": "Output to critique",
                "rubric": "Quality assessment",
            },
            metadata={},
        )

        primitive = CritiquePrimitive()
        result = await primitive.execute(context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.score is not None

    @pytest.mark.asyncio
    async def test_critique_score_parsing(self):
        """Test that critique score is parsed correctly."""
        primitive = CritiquePrimitive()

        content_with_score = "SCORE: 8.5\nThis is a great output"
        score = primitive._parse_score(content_with_score)
        assert score == 8.5

        content_no_score = "This output looks fine"
        score = primitive._parse_score(content_no_score)
        assert score == 5.0


class TestVotePrimitive:
    """Tests for VotePrimitive."""

    @pytest.mark.asyncio
    async def test_vote_with_candidates_in_inputs(self, basic_context):
        """Test vote with candidates in inputs."""
        basic_context.inputs["candidates"] = ["Option A", "Option B", "Option C"]
        basic_context.inputs["criteria"] = "Select the best option"

        primitive = VotePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.metadata.get("winner_index") is not None
        assert result.metadata.get("num_candidates") == 3

    @pytest.mark.asyncio
    async def test_vote_single_candidate(self, basic_context):
        """Test vote with single candidate auto-selects."""
        basic_context.inputs["candidates"] = ["Only option"]

        primitive = VotePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.metadata.get("winner_index") == 0

    @pytest.mark.asyncio
    async def test_vote_no_candidates(self, basic_context):
        """Test vote with no candidates."""
        basic_context.inputs["candidates"] = []

        primitive = VotePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.metadata.get("error") == "no_candidates"


class TestRevisePrimitive:
    """Tests for RevisePrimitive."""

    @pytest.mark.asyncio
    async def test_revise_with_original_and_critique(self, basic_context):
        """Test revise with original and critique."""
        basic_context.inputs["original"] = "Original output text"
        basic_context.inputs["critique"] = "Please make it clearer"

        primitive = RevisePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.metadata.get("original_length") == len("Original output text")
        assert result.metadata.get("critique_length") == len("Please make it clearer")


class TestBranchPrimitive:
    """Tests for BranchPrimitive."""

    @pytest.mark.asyncio
    async def test_branch_generates_multiple_candidates(self, basic_context):
        """Test branch generates multiple candidates."""
        basic_context.inputs["prompt"] = "Generate a story opening"
        basic_context.config["n"] = 3

        primitive = BranchPrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.metadata.get("num_branches") == 3
        assert len(result.metadata.get("candidates", [])) == 3


class TestMergePrimitive:
    """Tests for MergePrimitive."""

    @pytest.mark.asyncio
    async def test_merge_multiple_outputs(self, basic_context):
        """Test merge combines multiple outputs."""
        basic_context.inputs["outputs"] = ["Output 1", "Output 2", "Output 3"]
        basic_context.config["strategy"] = "combine"

        primitive = MergePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output
        assert result.metadata.get("num_inputs") == 3
        assert result.metadata.get("strategy") == "combine"

    @pytest.mark.asyncio
    async def test_merge_single_output(self, basic_context):
        """Test merge with single output returns it directly."""
        basic_context.inputs["outputs"] = ["Only one output"]

        primitive = MergePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.output == "Only one output"
        assert result.metadata.get("singleton") is True

    @pytest.mark.asyncio
    async def test_merge_no_outputs(self, basic_context):
        """Test merge with no outputs returns error."""
        basic_context.inputs["outputs"] = []

        primitive = MergePrimitive()
        result = await primitive.execute(basic_context)

        assert isinstance(result, PrimitiveResult)
        assert result.metadata.get("error") == "no_outputs"


class TestGetPrimitive:
    """Tests for get_primitive factory function."""

    def test_get_known_primitive(self):
        """Test getting a known primitive."""
        primitive = get_primitive("generate")
        assert isinstance(primitive, GeneratePrimitive)

        primitive = get_primitive("critique")
        assert isinstance(primitive, CritiquePrimitive)

        primitive = get_primitive("vote")
        assert isinstance(primitive, VotePrimitive)

        primitive = get_primitive("revise")
        assert isinstance(primitive, RevisePrimitive)

        primitive = get_primitive("branch")
        assert isinstance(primitive, BranchPrimitive)

        primitive = get_primitive("merge")
        assert isinstance(primitive, MergePrimitive)

    def test_get_unknown_primitive_raises(self):
        """Test that getting an unknown primitive raises ValueError."""
        with pytest.raises(ValueError):
            get_primitive("unknown_primitive")


class TestPrimitiveContext:
    """Tests for PrimitiveContext."""

    def test_primitive_context_creation(self):
        """Test PrimitiveContext can be created."""
        from loops.model_router import LLMClient

        client = LLMClient(models=["gpt-4o-mini"])
        context = PrimitiveContext(
            task="Test task",
            model_client=client,
            system_prompt="System prompt",
            inputs={"key": "value"},
            config={"setting": True},
            metadata={"node_id": "test-node"},
        )

        assert context.task == "Test task"
        assert context.model_client is client
        assert context.inputs == {"key": "value"}
        assert context.config == {"setting": True}
        assert context.metadata == {"node_id": "test-node"}


class TestPrimitiveResult:
    """Tests for PrimitiveResult."""

    def test_primitive_result_with_score(self):
        """Test PrimitiveResult with score."""
        result = PrimitiveResult(
            output="Test output",
            score=8.5,
            tokens_used=100,
            cost_usd=0.001,
            latency_ms=500,
            metadata={"key": "value"},
        )

        assert result.output == "Test output"
        assert result.score == 8.5
        assert result.tokens_used == 100
        assert result.cost_usd == 0.001
        assert result.latency_ms == 500
        assert result.metadata == {"key": "value"}

    def test_primitive_result_without_score(self):
        """Test PrimitiveResult without score."""
        result = PrimitiveResult(
            output="Test output",
            tokens_used=100,
            cost_usd=0.001,
            latency_ms=500,
        )

        assert result.output == "Test output"
        assert result.score is None


# ---------------------------------------------------------------------------
# Phase 10: Pydantic LoopPrimitive / PrimitiveExecutor / PrimitiveType
# ---------------------------------------------------------------------------


class TestPrimitiveType:
    """Tests for the Phase 10 :class:`PrimitiveType` enum."""

    def test_six_canonical_types(self):
        from loops.primitives import PrimitiveType

        names = {t.value for t in PrimitiveType}
        assert names == {
            "generate",
            "critique",
            "revise",
            "branch",
            "vote",
            "merge",
        }

    def test_enum_members_are_unique(self):
        from loops.primitives import PrimitiveType

        values = [t.value for t in PrimitiveType]
        assert len(values) == len(set(values))


class TestLoopPrimitive:
    """Tests for the Phase 10 :class:`LoopPrimitive` Pydantic model."""

    def test_default_construction(self):
        from loops.primitives import LoopPrimitive, PrimitiveType

        p = LoopPrimitive(
            node_id="n1",
            primitive=PrimitiveType.GENERATE,
        )
        assert p.node_id == "n1"
        assert p.primitive == PrimitiveType.GENERATE
        assert p.temperature == 0.7
        assert p.model_override is None
        assert p.parameters == {}

    def test_extra_forbidden(self):
        from loops.primitives import LoopPrimitive, PrimitiveType

        with pytest.raises(Exception):
            LoopPrimitive(
                node_id="n1", primitive=PrimitiveType.GENERATE, bogus=1
            )

    def test_serialization_round_trip(self):
        from loops.primitives import LoopPrimitive, PrimitiveType

        p = LoopPrimitive(
            node_id="n1",
            primitive=PrimitiveType.CRITIQUE,
            temperature=0.3,
            parameters={"rubric": "accuracy"},
        )
        d = p.model_dump(mode="json")
        assert d["node_id"] == "n1"
        assert d["primitive"] == "critique"
        assert d["parameters"] == {"rubric": "accuracy"}

    def test_six_primitive_types_constructable(self):
        from loops.primitives import LoopPrimitive, PrimitiveType

        for pt in PrimitiveType:
            p = LoopPrimitive(node_id=f"n-{pt.value}", primitive=pt)
            assert p.primitive == pt


class TestPrimitiveOutput:
    """Tests for :class:`PrimitiveOutput`."""

    def test_default_construction(self):
        from loops.primitives import PrimitiveOutput

        out = PrimitiveOutput(output="hello")
        assert out.output == "hello"
        assert out.score is None
        assert out.tokens_used == 0
        assert out.metadata == {}

    def test_serialization_round_trip(self):
        from loops.primitives import PrimitiveOutput

        out = PrimitiveOutput(
            output="x", score=7.5, metadata={"k": "v"}
        )
        d = out.model_dump()
        assert d["output"] == "x"
        assert d["score"] == 7.5
        assert d["metadata"] == {"k": "v"}


class TestPrimitiveExecutor:
    """Tests for :class:`PrimitiveExecutor`."""

    def test_executor_estimate_cost_branch(self):
        from loops.primitives import (
            LoopPrimitive,
            PrimitiveExecutor,
            PrimitiveType,
        )

        executor = PrimitiveExecutor()
        # Branch primitive's cost is the branch count ``n``.
        three = LoopPrimitive(
            node_id="b3",
            primitive=PrimitiveType.BRANCH,
            temperature=0.5,
            parameters={"n": 3},
        )
        five = LoopPrimitive(
            node_id="b5",
            primitive=PrimitiveType.BRANCH,
            temperature=0.5,
            parameters={"n": 5},
        )
        assert executor.estimate_cost(three) == 3.0
        assert executor.estimate_cost(five) == 5.0

    def test_executor_estimate_cost_non_parallel(self):
        from loops.primitives import (
            LoopPrimitive,
            PrimitiveExecutor,
            PrimitiveType,
        )

        executor = PrimitiveExecutor()
        gen = LoopPrimitive(
            node_id="g", primitive=PrimitiveType.GENERATE, temperature=0.5
        )
        crit = LoopPrimitive(
            node_id="c", primitive=PrimitiveType.CRITIQUE, temperature=0.5
        )
        # Non-branch primitives are 1.0 cost units.
        assert executor.estimate_cost(gen) == 1.0
        assert executor.estimate_cost(crit) == 1.0

    def test_executor_execute_without_model_raises(self):
        from loops.primitives import (
            LoopPrimitive,
            PrimitiveExecutor,
            PrimitiveType,
        )

        executor = PrimitiveExecutor()
        p = LoopPrimitive(
            node_id="n1", primitive=PrimitiveType.GENERATE, temperature=0.5
        )
        import asyncio

        with pytest.raises(ValueError):
            asyncio.run(executor.execute(p, task="x", preamble={}))