"""Tests for loop registry."""

import pytest

from loops.graph import LoopGraph
from loops.registry import LoopRegistry, LoopStats, create_registry


@pytest.fixture
def registry():
    """Create a fresh in-memory registry for testing."""
    return create_registry(db_path=None)


class TestLoopRegistry:
    """Tests for LoopRegistry."""

    def test_count_empty(self, registry):
        """Test count on empty registry."""
        assert registry.count() == 6

    def test_register_template(self, registry):
        """Test registering a new template."""
        graph = LoopGraph.direct_graph("test-new")
        registry.register_template(graph, task_type="general")

        retrieved = registry.get_template("test-new")
        assert retrieved is not None
        assert retrieved.id == "test-new"

    def test_register_existing_updates(self, registry):
        """Test registering existing template updates it."""
        graph = LoopGraph.direct_graph("test-update")
        registry.register_template(graph, task_type="general")

        graph.description = "Updated description"
        registry.register_template(graph, task_type="general")

        retrieved = registry.get_template("test-update")
        assert retrieved.description == "Updated description"

    def test_get_template_not_found(self, registry):
        """Test getting nonexistent template returns None."""
        result = registry.get_template("nonexistent-id")
        assert result is None

    def test_list_templates(self, registry):
        """Test listing all templates."""
        templates = registry.list_templates()
        assert len(templates) >= 6

    def test_list_templates_by_task_type(self, registry):
        """Test listing templates filtered by task type."""
        templates = registry.list_templates(task_type="coding")
        assert all(t.get("task_type") == "coding" for t in templates)

    def test_list_templates_by_min_success_rate(self, registry):
        """Test listing templates filtered by success rate."""
        registry.update_stats("direct", score=8.0, cost=0.001, latency=100, success=True)
        registry.update_stats("direct", score=8.0, cost=0.001, latency=100, success=True)
        registry.update_stats("direct", score=8.0, cost=0.001, latency=100, success=True)

        templates = registry.list_templates(min_success_rate=0.8)
        assert len(templates) >= 1

    def test_update_stats(self, registry):
        """Test updating template statistics."""
        registry.update_stats("direct", score=7.5, cost=0.002, latency=200, success=True)

        stats = registry.get_stats("direct")
        assert stats is not None
        assert stats.usage_count == 1
        assert stats.avg_score == 7.5

    def test_update_stats_incremental_average(self, registry):
        """Test that stats are updated with incremental average."""
        registry.update_stats("cot", score=5.0, cost=0.001, latency=100, success=True)
        registry.update_stats("cot", score=9.0, cost=0.002, latency=200, success=True)

        stats = registry.get_stats("cot")
        assert stats is not None
        assert stats.usage_count == 2
        assert 6.5 < stats.avg_score < 7.5

    def test_update_stats_nonexistent_template(self, registry):
        """Test updating stats for nonexistent template does nothing."""
        registry.update_stats("nonexistent", score=5.0, cost=0.001, latency=100, success=True)

        stats = registry.get_stats("nonexistent")
        assert stats is None

    def test_get_stats(self, registry):
        """Test getting stats for a template."""
        registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)

        stats = registry.get_stats("reflection")
        assert stats is not None
        assert stats.avg_score == 8.0
        assert stats.avg_cost_usd == 0.003
        assert stats.avg_latency_ms == 300

    def test_get_stats_not_found(self, registry):
        """Test getting stats for nonexistent template."""
        stats = registry.get_stats("nonexistent")
        assert stats is None

    def test_get_recommendation(self, registry):
        """Test getting recommendations for a task type."""
        registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)
        registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)

        recs = registry.get_recommendation("coding")
        assert len(recs) <= 3
        assert all("id" in r for r in recs)
        assert all("recommendation_score" in r for r in recs)

    def test_get_recommendation_with_budget(self, registry):
        """Test recommendations respect budget constraint."""
        registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)
        registry.update_stats("reflection", score=9.0, cost=0.050, latency=500, success=True)

        recs = registry.get_recommendation("coding", budget_usd=0.01)
        for rec in recs:
            assert rec["avg_cost_usd"] <= 0.01

    def test_delete_template(self, registry):
        """Test deleting a template."""
        graph = LoopGraph.direct_graph("test-delete")
        registry.register_template(graph)

        assert registry.get_template("test-delete") is not None

        deleted = registry.delete_template("test-delete")
        assert deleted is True

        assert registry.get_template("test-delete") is None

    def test_delete_template_not_found(self, registry):
        """Test deleting nonexistent template returns False."""
        deleted = registry.delete_template("nonexistent")
        assert deleted is False

    def test_insert_premade_templates(self, registry):
        """Test premade templates are inserted at boot."""
        count = registry.count()
        assert count >= 6

        expected_ids = ["direct", "cot", "reflection", "tree", "debate", "ensemble"]
        for template_id in expected_ids:
            template = registry.get_template(template_id)
            assert template is not None


class TestLoopStats:
    """Tests for LoopStats dataclass."""

    def test_default_values(self):
        """Test LoopStats default values."""
        stats = LoopStats()

        assert stats.success_rate == 0.0
        assert stats.avg_score == 0.0
        assert stats.avg_cost_usd == 0.0
        assert stats.avg_latency_ms == 0.0
        assert stats.usage_count == 0

    def test_custom_values(self):
        """Test LoopStats with custom values."""
        stats = LoopStats(
            success_rate=0.85,
            avg_score=7.5,
            avg_cost_usd=0.003,
            avg_latency_ms=250,
            usage_count=10,
        )

        assert stats.success_rate == 0.85
        assert stats.avg_score == 7.5
        assert stats.avg_cost_usd == 0.003
        assert stats.avg_latency_ms == 250
        assert stats.usage_count == 10


class TestCreateRegistry:
    """Tests for create_registry factory."""

    def test_create_registry_creates_premade_templates(self):
        """Test create_registry inserts premade templates."""
        registry = create_registry(db_path=None)

        assert registry.count() >= 6

        templates = registry.list_templates()
        template_ids = [t["id"] for t in templates]

        assert "direct" in template_ids
        assert "cot" in template_ids
        assert "reflection" in template_ids
        assert "tree" in template_ids
        assert "debate" in template_ids
        assert "ensemble" in template_ids

    def test_create_registry_with_file_path(self, tmp_path):
        """Test create_registry with file path."""
        db_file = tmp_path / "test_registry.db"

        registry = create_registry(db_path=str(db_file))
        assert registry.count() >= 6

        registry2 = create_registry(db_path=str(db_file))
        assert registry2.count() >= 6


class TestRegistryWithFile:
    """Tests for registry persistence with file."""

    def test_registry_persists_data(self, tmp_path):
        """Test that data persists across registry instances."""
        db_file = tmp_path / "test.db"

        registry1 = create_registry(db_path=str(db_file))
        registry1.update_stats("direct", score=8.0, cost=0.001, latency=100, success=True)
        stats1 = registry1.get_stats("direct")
        assert stats1 is not None
        assert stats1.usage_count == 1

        registry2 = create_registry(db_path=str(db_file))
        stats2 = registry2.get_stats("direct")
        assert stats2 is not None
        assert stats2.usage_count == 1