"""Thinking loops module.

Implements various reasoning patterns for agent workers.
Phase 4 adds dynamic loop assembly with primitives, graphs, and registry.
Phase 10 adds the Pydantic-style LoopPrimitive / LoopGraph facade plus
the Critic, TrialStore, MetaAgent and LoopOptimizer on top.
"""

from .assembler import (
    AssemblerError,
    LoopAssembler,
    LoopGraph as PydanticLoopGraph,
    LoopEdge as PydanticLoopEdge,
    PrebuiltGraphs,
    StopConditionError,
)
from .base_loop import BaseLoop, LoopResult
from .cot import CoTLoop
from .debate import DebateLoop
from .direct import DirectLoop
from .ensemble import EnsembleLoop
from .graph import GraphValidationError, LoopEdge, LoopGraph, LoopNode
from .model_router import (
    LLMClient,
    ModelExhaustedError,
    ModelResponse,
    ModelRouter,
)
from .optimizer import CriticScore, LoopOptimizer, LoopRecommendation
from .preamble_assembler import (
    ContextAssembler,
    PermissionOverrideError,
    PreambleAssembler,
    assemble,
    assemble_minimal,
)
from .primitives import (
    BranchPrimitive,
    CritiquePrimitive,
    GeneratePrimitive,
    LoopPrimitive,
    MergePrimitive,
    Primitive,
    PrimitiveContext,
    PrimitiveExecutor,
    PrimitiveOutput,
    PrimitiveResult,
    PrimitiveType,
    RevisePrimitive,
    VotePrimitive,
    get_primitive,
)
from .reflection import ReflectionLoop
from .registry import LoopRegistry, LoopStats, create_registry
from .router import LoopRouter, LOOPS, run_custom_loop, run_loop
from .meta_stub import MetaAgentStub
from .tool_executor import ToolExecutor, ToolResult
from .tree import TreeOfThoughtsLoop

__all__ = [
    "AssemblerError",
    "BaseLoop",
    "LoopResult",
    "CoTLoop",
    "CriticScore",
    "DebateLoop",
    "DirectLoop",
    "EnsembleLoop",
    "GraphValidationError",
    "LLMClient",
    "LoopAssembler",
    "LoopEdge",
    "LoopGraph",
    "PydanticLoopEdge",
    "PydanticLoopGraph",
    "LoopNode",
    "LoopOptimizer",
    "LoopRecommendation",
    "LoopPrimitive",
    "LoopRegistry",
    "LoopRouter",
    "LoopStats",
    "LOOPS",
    "MetaAgentStub",
    "ModelExhaustedError",
    "ModelResponse",
    "ModelRouter",
    "PrebuiltGraphs",
    "Primitive",
    "PrimitiveContext",
    "PrimitiveExecutor",
    "PrimitiveOutput",
    "PrimitiveResult",
    "PrimitiveType",
    "ContextAssembler",
    "PermissionOverrideError",
    "PreambleAssembler",
    "assemble",
    "assemble_minimal",
    "BranchPrimitive",
    "CritiquePrimitive",
    "create_registry",
    "GeneratePrimitive",
    "get_primitive",
    "MergePrimitive",
    "ReflectionLoop",
    "RevisePrimitive",
    "run_loop",
    "run_custom_loop",
    "StopConditionError",
    "ToolExecutor",
    "ToolResult",
    "TreeOfThoughtsLoop",
    "VotePrimitive",
]