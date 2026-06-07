"""Tests for the dashboard configuration API (views + layouts)."""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.backend.config import ConfigAPI  # noqa: E402
from dashboard.backend.models import (  # noqa: E402
    LayoutConfigInput,
    ViewConfig,
    ViewConfigInput,
)


@pytest_asyncio.fixture
async def cfg(tmp_path) -> AsyncIterator[ConfigAPI]:
    """An isolated :class:`ConfigAPI`."""
    api = ConfigAPI(tmp_path / "dashboard.db")
    await api.initialize()
    try:
        yield api
    finally:
        await api.close()


async def test_save_and_retrieve_view(cfg):
    body = ViewConfigInput(
        name="Research Panel",
        description="Show research agents",
        view_type="custom",
        data_sources=["/api/agents", "/api/memory"],
        filters={"category": "research"},
        refresh_interval_ms=5000,
        created_by="main-agent",
    )
    view_id = await cfg.save_view(body)
    assert view_id.startswith("view-")
    view = await cfg.get_view(view_id)
    assert view.name == "Research Panel"
    assert view.view_type == "custom"
    assert view.filters == {"category": "research"}


async def test_update_view_replaces_body(cfg):
    body = ViewConfigInput(
        name="V1",
        description="old",
        data_sources=[],
    )
    view_id = await cfg.save_view(body)
    # Update.
    body2 = ViewConfigInput(
        name="V2",
        description="new",
        data_sources=["/api/workflows"],
        filters={"status": "running"},
        refresh_interval_ms=2000,
    )
    merged = ViewConfig(
        view_id=view_id,
        name=body2.name,
        description=body2.description,
        view_type=body2.view_type,
        data_sources=body2.data_sources,
        filters=body2.filters,
        refresh_interval_ms=body2.refresh_interval_ms,
        created_by=body2.created_by,
    )
    await cfg.save_view(merged)
    got = await cfg.get_view(view_id)
    assert got.name == "V2"
    assert got.description == "new"
    assert got.data_sources == ["/api/workflows"]
    assert got.refresh_interval_ms == 2000


async def test_delete_view(cfg):
    body = ViewConfigInput(name="to-delete", description="")
    view_id = await cfg.save_view(body)
    await cfg.delete_view(view_id)
    with pytest.raises(KeyError):
        await cfg.get_view(view_id)


async def test_get_views_returns_all(cfg):
    for i in range(3):
        await cfg.save_view(ViewConfigInput(name=f"v{i}"))
    views = await cfg.get_views()
    assert len(views) == 3


async def test_save_and_retrieve_layout(cfg):
    body = LayoutConfigInput(
        name="Default",
        description="Two columns",
        panes={"left": {"view_id": "x"}, "right": {"view_id": "y"}},
        created_by="main-agent",
    )
    layout_id = await cfg.save_layout(body)
    layout = await cfg.get_layout(layout_id)
    assert layout.name == "Default"
    assert "left" in layout.panes


async def test_delete_layout(cfg):
    body = LayoutConfigInput(name="bye", panes={})
    layout_id = await cfg.save_layout(body)
    await cfg.delete_layout(layout_id)
    with pytest.raises(KeyError):
        await cfg.get_layout(layout_id)


async def test_invalid_view_body_rejected():
    """Pydantic rejects out-of-range values."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ViewConfigInput(name="x", data_sources=["ok"], filters={"k": "v"}, refresh_interval_ms=0)
    with pytest.raises(ValidationError):
        ViewConfigInput(name="x", data_sources=["ok"], filters={"k": "v"}, refresh_interval_ms=10**9)


async def test_get_view_raises_keyerror_on_unknown(cfg):
    with pytest.raises(KeyError):
        await cfg.get_view("does-not-exist")


async def test_get_layout_raises_keyerror_on_unknown(cfg):
    with pytest.raises(KeyError):
        await cfg.get_layout("does-not-exist")
