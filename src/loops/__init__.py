"""Thinking loops module.

Implements various reasoning patterns for agent workers.
Phase 4 adds dynamic loop assembly with primitives, graphs, and registry.
"""

from .assembler import LoopAssembler, PrebuiltGraphs
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
from .preamble_assembler import assemble, assemble_minimal
from .primitives import (
    BranchPrimitive,
    CritiquePrimitive,
    GeneratePrimitive,
    MergePrimitive,
    Primitive,
    PrimitiveContext,
    PrimitiveResult,
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
    "LoopNode",
    "LoopOptimizer",
    "LoopRecommendation",
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
    "PrimitiveResult",
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
    "ToolExecutor",
    "ToolResult",
    "TreeOfThoughtsLoop",
    "VotePrimitive",
]