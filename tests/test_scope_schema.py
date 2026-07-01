from __future__ import annotations

from archon_horizon.core.roadmap import Roadmap, RoadmapItem
from archon_horizon.core.scope import ItemScope
from archon_horizon.store import serde
from archon_horizon.store.codec import YamlCodec
from archon_horizon.store.filesystem import FilesystemRoadmapStore


def test_scope_serializes_compact_strings_and_structured_overrides() -> None:
    scope = ItemScope(
        projects=("ag-main",),
        files=(
            "Main.lean",
            {"target": "Context.lean", "access": "read", "layers": {"signature": "frozen"}},
        ),
    )

    assert serde.to_jsonable(scope) == {
        "projects": ["ag-main"],
        "files": [
            "Main.lean",
            {"target": "Context.lean", "access": "read", "layers": {"signature": "frozen"}},
        ],
    }


def test_roadmap_store_saves_items_as_shards(tmp_path) -> None:
    store = FilesystemRoadmapStore(tmp_path / ".archon-horizon" / "roadmap", YamlCodec())
    store.save(
        Roadmap(
            items=(
                RoadmapItem(
                    id="A.3",
                    title="Pic0 tangent",
                    projects=("ag-main",),
                    scope=ItemScope(
                        projects=("ag-main",),
                        declarations=(
                            {
                                "target": "AlgebraicJacobian.Picard.Pic0.tangentSpaceIso",
                                "layers": {"signature": "frozen"},
                            },
                        ),
                    ),
                ),
            )
        )
    )

    shard = tmp_path / ".archon-horizon" / "roadmap" / "items" / "A.3.yaml"
    assert shard.exists()
    data = YamlCodec().loads(shard.read_text("utf-8"))
    assert data["scope"]["projects"] == ["ag-main"]
    assert data["scope"]["declarations"][0]["layers"] == {"signature": "frozen"}
    assert store.load().items[0].scope.declarations[0].target.endswith("tangentSpaceIso")
