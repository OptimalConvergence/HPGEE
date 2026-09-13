# Incremental global metadata

`geesampler metadata sync` lists image metadata without downloading pixels. It scans bounded date windows with paginated responses, writes scenes and the next-page cursor in one transaction, and resumes incomplete windows after interruption. A completed global window also supplies catalog coverage for tiles with no scenes. Existing imported rows alone do not assert completeness.

```bash
geesampler metadata grids ./reference-grids
geesampler metadata sync sampling.yaml s2 2025-01-01 2026-07-01 \
  --database ./sentinel2.sqlite --workers 2 --chunk-days 31
geesampler metadata status ./sentinel2.sqlite
```

Use the same auth configuration as ordinary sampling. Proxy environment variables are supported; the library does not manage VPN processes. Metadata requests use a bounded transport timeout. The grid command requires the `geo` extra. It downloads the official USGS WRS-2 descending and Copernicus Sentinel-2 tiling archives, validates ZIP CRCs, records URLs/SHA-256 hashes and preserves all source polygons in an indexed SQLite grid library. HLS uses the Sentinel-2 MGRS tile identifiers; consult its product documentation for pixel-grid differences.

Supported collection aliases:

| Alias | GEE collection | Native grouping |
| --- | --- | --- |
| `s2` | `COPERNICUS/S2_SR_HARMONIZED` | MGRS |
| `landsat4`, `landsat5`, `landsat7`, `landsat9` | `LANDSAT/{LT04,LT05,LE07,LC09}/C02/T1_L2` | WRS path/row |
| `hlsl30`, `hlss30` | `NASA/HLS/{HLSL30,HLSS30}/v002` | MGRS |
| `viirs` | `NASA/VIIRS/002/VNP09GA` | GEE daily mosaic assets |
| `gedi` | `LARSE/GEDI/GEDI02_A_002_MONTHLY` | GEE raster assets; no assumed WRS/MGRS grid |
| `gedi-index` | `LARSE/GEDI/GEDI02_A_002_INDEX` | Orbit-granule table footprints |

A full collection ID can also be supplied for image metadata listing. The generic scanner reuses `S2SceneCatalog` and its existing schema: the historical `mgrs_tile` column holds native grid keys (zero-padded `001/053` for Landsat). For the supported VIIRS/GEDI products without a WRS/MGRS key, `global` is a grouping label, not a claim of valid measurements everywhere. VIIRS can expose an infinite/unbounded mosaic geometry: its original geometry and band grids are retained, while its RTree uses a conservative world envelope explicitly marked in properties. GEDI index scanning collects metadata pointing to granule tables, not every laser shot.

Dates are **half-open UTC acquisition intervals**. To include June 30, pass July 1 as the end. The scanner explicitly filters `startTime` for both bounds: the SDK's `endTime` shortcut filters an asset's end time and can omit daily/monthly composites at a boundary. An empty completed interval is recorded normally; for example, Landsat 4 has no 2000–2026 acquisitions. Metadata enumeration does not apply a cloud filter or pretend that scene availability is equivalent to usable clear pixels.

## Completeness and recovery

- `metadata_scans` records planned windows, committed pages and completion. Only exhausting a page chain marks that window complete. Invalid rows, duplicate page cursors or failed requests leave it incomplete.
- Repeating a completed range performs no metadata downloads. Overlapping extensions enumerate only missing ranges. Resuming retains original incomplete window boundaries and concurrency, even if a different chunk size is supplied.
- Keep one scanner process per destination. A separate SQLite lock database rejects a second `sync` process; threads serialize scene writes. Call `sync` for process locking; `scan_window` is its lower-level primitive. Keep the database and lock sidecar together. Do not delete a live lock file.
- A stale/rejected pagination token is surfaced as a failure; do not mark its window complete. Retained scenes are safe for idempotent replay, but automatic token-reset/refresh is not implemented in this version.
- Global enumeration is a snapshot observation, not a perpetual guarantee. Upstream backfills can occur after a window completes. This command fills missing windows; it does not automatically refresh completed windows or remove retired assets.
- Validate important builds with an independent GEE count over the same acquisition interval and SQLite integrity checks. RTree envelopes shortlist spatial matches; use stored geometry for exact intersections, especially near the dateline.
- `metadata status` reports recorded scan progress. The number of rows seen is an enumeration counter, not scientific acceptance. Large database checks and complete log scans belong on appropriately allocated compute resources.

For an existing S2 archive, retain a verified snapshot before extending it. The schema and scene identifiers remain compatible with existing S2 sampling; new globally verified windows can satisfy read-through coverage. Old imports without coverage records remain unverified.

## Performance and robustness

Start with a few metadata workers and measure pages/second, request latency, commit time and resource use. Global date-window listing avoids millions of empty per-grid queries, while official grids support spatial lookup and validation. Long, known-empty collection ranges can use one large chunk. Grid completeness does not imply scene availability in every cell.

For image-worker trials, initialize a new `Sampler` with a sufficiently large HTTP pool before each independent run. Increasing a sampler's worker configuration beyond its initialized pool now raises an actionable error rather than silently retaining the smaller blocking pool. Do not reinitialize shared Earth Engine global state during an active run.

The `retry_read` helper retries idempotent reads on transport errors and retryable HTTP statuses, with bounded backoff. Permission and validation errors propagate immediately. It is also used during Earth Engine initialization; catalog metadata retries now distinguish transient failures from terminal errors. It is not a retry wrapper for export submission or mutations.

```bash
geesampler progress path/to/metrics.jsonl --minutes 30
```

The progress command streams timestamped task metrics, excludes cache hits from received-image bytes and reports malformed lines. It does not infer final dataset completion or scientific acceptance. Full workflow throughput includes metadata/provenance/QA gaps; download-stage throughput measures a different denominator. Neither establishes raw NIC capacity or EECU usage.

References: [Earth Engine listing](https://developers.google.com/earth-engine/apidocs/ee-data-listimages), [USGS WRS-2](https://www.usgs.gov/media/files/landsat-world-reference-system-2-descending-daytime-shapefile), [Sentinel-2 grid](https://sentiwiki.copernicus.eu/web/s2-products), [HLS tiling](https://hls.gsfc.nasa.gov/products-description/tiling-system/), [GEDI index](https://developers.google.com/earth-engine/datasets/catalog/LARSE_GEDI_GEDI02_A_002_INDEX).
