# Bounded RF, regional products and candidate APIs

Use these APIs from a pinned HPGEE snapshot containing the required `hpgee.compute`, `hpgee.planning`, and `hpgee.workflows` capabilities, not a mutable maintenance checkout. They provide engineering contracts around the existing feature recipe. Offline graph/tests, authenticated requests and accepted saved pixels are distinct evidence levels; consult the release's evidence for each.

## Reuse a validated cohort and lazy model

```python
from hpgee.compute import rf

samples, provenance = rf.input_samples(cfg)
snapshot = rf.freeze_training_snapshot(samples, cfg)
model, model_metadata = rf.build_rf_model(snapshot, cfg)
for unit_cfg in unit_configs:
    scores = rf.predict_rf(model, unit_cfg)
    # Build/execute only explicitly authorized products on each frozen grid.
```

`TrainingSnapshot` owns immutable rows, feature names and split bytes/hashes. Local tables need no remote table-read request; the bounded remote path checks overflow. `build_rf_model` constructs a lazy classifier from validated literal rows, without rereading an asset or split file. `predict_rf` accepts exactly one Config or explicit `features=` image. Its caller must bind that image to the model feature contract.

This is one client-side cohort validation/model graph construction. Separate EE evaluations may train again. Only successfully materialized result IMAGEs remove that upstream graph from subsequent image reads. `train_rf` remains the compatible single-Config wrapper. RF scores remain uncalibrated; a reusable snapshot does not grant sample eligibility.

`workflow.build_products(cfg, scores, pre=pre, post=post, source_manifest_sha256=sha)` accepts already-built composites. Supply all three optional arguments together, from the same frozen source/feature contract. This avoids silently rebuilding a different HLS source selection when packaging predictions. It does not cache server computations or include an original scene cube automatically.

The minimal loop above uses Config's default source recipe. For frozen-scene execution, build `features` from exactly the same frozen `pre`/`post` composites, call `predict_rf(model, features=features)`, then pass those composites and their source SHA to `build_products`. Do not combine default-source scores with a different frozen-source product package.

### Frozen auxiliary context and pixel-only verification

These additions follow regional-v2; check the imported frozen package inventory before using them. Existing regional-v2 releases and runs retain their prior API and contracts.

`build_products(..., aux=auxiliary_image, aux_context_sha256=sha)` accepts both optional values together. The image supplies the four unprefixed `DEM_BANDS`; the 64-hex SHA binds a caller-owned frozen context, whose complete source IDs/order, query support, grid and recipe remain in the calling project's plan. `workflow_contract(..., aux_context_sha256=sha)` records that context identity with `DEM.collection=None` and caller-declared native support: an opaque hash cannot identify the default DEM collection. The actual collection must be read from the bound caller context. The default call retains its previous DEM selection and contract. A context hash is provenance, not authentication or proof of valid terrain pixels.

For whole/subset comparisons, construct terrain once on a common frozen context and analysis lattice, then reuse it for each crop. Do not independently select different DEM mosaics for the controls or remove valid HLS predictions where auxiliary data are absent. Reusing an image expression does not promise cross-task server caching.

`workflow.verify_pixels(path, contract, allow_no_observation=True)` checks saved grid, bands, masks, scores, candidate rule and temporal/quality support without requiring a boundary file. Its default still rejects entirely unobserved products; explicit no-observation acceptance retains masked predictions and reports that state. `verify_artifacts` continues to require both pixels and the full boundary union under its existing default behavior. If bounded polygonization overflows, retain pixel QA with `vector_pending`; neither a prefix GeoJSON nor pixel-only acceptance proves the boundary deliverable complete.

## Plan mutually exclusive cores

`regional.build_region_plan(geometry=..., grid=..., months=..., feature_windows=..., core_size=..., halo_pixels=...)` is offline. Geometry is WGS84 Polygon/MultiPolygon; grid is an explicit north-up 30 m UTM grid. Each `YYYY-MM` has explicit UTC half-open `pre`/`post` windows, separate from its calendar output month.

Each unit carries `owner`, `core_grid`, `query_grid`, `core`, `query`, `output` and `crop_to_core`. Query halos provide algorithm context and may overlap. Crop predictions and QA to their owned core before mosaicking, counting area or indexing components. Sampling buffers around fire candidates are a separate quantity from algorithm halo.

The planner splits large core grids/bboxes to respect Config limits. `execution='batch_export_plan'` is a declaration for unsupported Config duration/halo size, not an executable export. Cross nominal UTM zone/hemisphere, antimeridian and unsupported latitude requests return unavailable; no global grid resolver is implemented. Polygon edges use projected vertices.

Before online execution, verify the source query geometry/windows cover every output/query grid. On a small control, compare saved whole and core mosaics for identical lattice, full coverage, masks, band order and values. A valid plan is not proof that the tiled recipe matches the whole recipe; neighborhood features may need adequate halo.

## Transfer an already-defined workflow result

```python
from hpgee.transfer.products import product_sampler_arguments

arguments = product_sampler_arguments(cfg, products)
result = engine.download_patch_series(
    **arguments, scenario="known-product", run_id=unique_run_id,
)
```

`engine` is the shared `hpgee.transfer.engine.DownloadEngine` with caller-owned project, allocation-local hot state, retry limits and workload identity. `products` has the exact `workflow_contract`, `PRODUCT_BANDS` order and `raster`. To read a successful result Asset, copy the product mapping and replace only `raster` with its `ee.Image(asset_id)`, preserving the contract and bands.

In packages that include frozen auxiliary context support, this validation also preserves its optional context SHA; it must not silently rebuild the default DEM contract.

The helper supplies a single known derived scene/resolver and exact grid. It does not run catalog selection, cloud/quality probes, or remove unknown pixels. It accepts only selection `all/0/0/1`; source enumeration is not bypassed for original HLS/S1 observations. `DownloadEngine` still performs computePixels, writes its normal receipts and owns transfer recovery. Use unique run identities so workload tags cannot collide after truncation.

The synthetic scene's clock is `cfg.start`, a **derived product reference time**, not a source acquisition or burning date. Actual pre/post clocks and QA remain in the saved product bands. Preserve raw TIFF bytes, apply `workflow.finalize_transfer`, then validate pixels and candidate boundaries as usual.

## Index candidates without inventing events

`candidates.index_saved_candidates(path, model_id=..., month=..., calibration_status=..., core_window=...)` reads exactly one named `candidate` band and its actual validity mask. `core_window=(row,column,height,width)` explicitly crops a query halo. The array equivalent is `index_candidates(array, valid, grid, model_id, month, calibration_status, ...)`.

The index retains binary candidate and valid masks separately, stable per-core 8-connected component IDs, candidate pixels/area, unknown support and source TIFF SHA. Observed noncandidate is not independent unburned truth. `month` is inference/packaging context, not an evidenced burning month; confirmed new-burn area and burning/event fields remain unknown.

`relate_components(indices)` records same-month touching and cross-month spatial overlap/adjacency on one fixed UTM lattice/model. Duplicate indices deduplicate; positive-area same-month core overlap is rejected. Different CRS/lattice/models remain unavailable. Relations do not union components into wildfire events, classify intent, infer monthly new burn, close events or distinguish reburn. Those require separately validated temporal/scientific rules and observation evidence.

## Organize supplied time evidence

`monthly.resolve_burn_month_support(target_month=..., as_of=..., last_unburned=..., first_burned=...)` is pure offline organization of caller-supplied state evidence. Each optional endpoint is `{time, evidence_ref}` with a timezone-aware timestamp and a nonempty caller reference; timestamps must precede the exclusive `as_of`. The function preserves references without authenticating their source, pixel identity or scientific eligibility.

Two ordered endpoints define `(u,b]`. The result retains every intersecting month, including project-boundary months outside the 2018–2025 target calendar. A single supported month is distinguished from cross-month ambiguity; either missing endpoint yields unknown month support, not an empty month list or unburned value. A target month outside a fully bounded interval can be excluded by that supplied evidence. Acquisition clocks and RF scores do not qualify as burn-state evidence by themselves.

This interface does not detect fire, create endpoint evidence, identify exact dates, declare new burned area or close events. The complete monthly raster/support manifest remains a calling-project design, separate from this implemented interval utility.
