"""Small live benchmark across patch sizes, endpoints, and thread counts."""

import os
from functools import partial
from pathlib import Path

from hpgee.benchmarks.sampling import staged_benchmark_patch_downloads
from hpgee.sampling.config import SamplerConfig
from hpgee.recipes.mining import mining_records
from hpgee.recipes.sentinel2 import S2_BANDS, sentinel2_catalog_collection


def main(config_path: str = "examples/configs/mining.yaml") -> None:
    config = SamplerConfig.from_yaml(config_path)
    data_root = Path(os.environ.get("GEESAMPLER_DATA_ROOT", "./geesampler-output"))
    polygons = data_root / "cache" / "global_mining_polygons_v2.parquet"
    staged_benchmark_patch_downloads(
        config,
        mining_records(polygons, limit=128),
        lambda grid: partial(sentinel2_catalog_collection, grid=grid),
        bands=S2_BANDS,
        output_dir=data_root / "benchmarks" / "s2-small-final",
    )


if __name__ == "__main__":
    main()
