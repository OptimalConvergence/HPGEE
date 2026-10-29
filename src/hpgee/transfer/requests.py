"""Exact-scene requests shared by observation and derived-product delivery.

This adapter supplies the existing engine's selection interface from frozen
local metadata. It never enumerates a catalog or applies extra scientific QA.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from hpgee.catalog.catalog import SceneRecord
from hpgee.catalog.resolver import ResolverStats
from hpgee.core.models import CollectionBuilder, PatchGrid, SampleRecord, SceneSelection


@dataclass(frozen=True)
class FrozenSceneRequest:
    record: SampleRecord
    scene: SceneRecord
    collection_builder: CollectionBuilder
    bands: Sequence[str]
    grid: PatchGrid
    pixel_grid: Mapping[str, Any]

    def sampler_arguments(self) -> dict[str, Any]:
        """Bind one scene and a defensive copy of its exact rectangular grid."""
        pixel_grid = deepcopy(self.pixel_grid)
        return dict(
            records=[self.record], collection_builder=self.collection_builder,
            bands=list(self.bands), grid=self.grid,
            grid_builder=lambda sample, spec: deepcopy(pixel_grid),
            selection=SceneSelection(mode='all', start_offset_days=0, end_offset_days=0,
                                     max_scenes=1),
            scene_resolver=FrozenSceneResolver(self.record, self.grid, self.scene),
        )


@dataclass(frozen=True)
class FrozenSceneResolver:
    record: SampleRecord
    grid: PatchGrid
    scene: SceneRecord

    inline_quality = probe_quality = cloud_masking = False
    min_clear_fraction = 0.0

    def prepare(self, samples, *, grid, selection, workload_tag):
        if (selection.mode, selection.start_offset_days, selection.end_offset_days,
                selection.max_scenes) != ('all', 0, 0, 1):
            raise ValueError('Frozen scene requires selection all/0/0/1')
        if list(samples) != [self.record] or grid != self.grid:
            raise ValueError('Frozen scene requires its single frozen sample/grid')

    def order_samples(self, samples):
        return list(samples)

    def candidates(self, sample):
        if sample != self.record:
            raise ValueError('Unknown frozen scene sample')
        return [self.scene]

    def cached_quality(self, sample, scene):
        return None

    def should_fallback_without_metadata(self, sample):
        return False

    def stats(self):
        return ResolverStats()
