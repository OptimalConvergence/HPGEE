# GEEComputation

Foundations for retrieval, computation optimization, and controlled scheduling through the GEE Python API. Following the user's division of responsibilities on 2026-10-13, S3+S4 maintains this library and owns GEE training, Global Burned Area Mapping Ver1, and efficiency optimization. Regional computation/output and cost pilots are complete; global V1 is not complete. S1+S2 owns data/sample strategy and consumes published library versions. Per-scene pixel acquisition reuses GEESampler; there is no second downloader or download queue.

## Two-stage version entrypoints

Project status and the protocol are in `/nfs-data0/home/lyh/workspace/projects/GlobFireProcMnt/STATUS.md` and `docs/stages/INTERFACE.md`. S3+S4 publishes `exchange/compute/latest.json` after updating code, the Skill, and calling examples; S1+S2 publishes `exchange/samples/latest.json`. The two entrypoints update atomically and independently. Consumers pin a version at startup without waiting for another handoff document.

```bash
cd /nfs-data0/home/lyh/workspace/projects/GlobFireProcMnt
python3 scripts/stage_exchange.py resolve --channel compute --artifact geecomputation --purpose inspect
python3 scripts/stage_exchange.py resolve --channel samples --artifact weak_rf_8000 --purpose cost_benchmark --feature-contract hls-common6-v2
```

Compute resolution returns the frozen code `pythonpath`, Skill/document paths, and capability contract. Sample resolution returns `labels` that can be merged directly into Config, together with permitted uses and file SHAs. Set the returned PYTHONPATH before running, save both manifest paths/SHAs, and do not switch latest during a run. The canonical external directories are development and maintenance locations; consumers can use immutable snapshots. The current weak table is restricted to cost_benchmark/workflow_smoke_test and cannot automatically become eligible for global training or independent validation. Ready describes the available scope; it does not grant additional resources or execution budget.

## Running

The verified runtime is `/nfs-data0/home/lyh/anaconda3/envs/geesampler/bin/python`, with ee 1.7.42, GEESampler 0.3.0, numpy, and pytest already available. Run the following from the library root:

```bash
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
/nfs-data0/home/lyh/anaconda3/envs/geesampler/bin/python -m geecomputation manifest --config examples/computation/small-roi.json
/nfs-data0/home/lyh/anaconda3/envs/geesampler/bin/python -m pytest -q
```

Replace the example's funded project, actual ROI, dates, output directory, and UTM grid before explicitly invoking:

```bash
python -m geecomputation inspect --config CONFIG.json
python -m geecomputation preview --config CONFIG.json
python -m geecomputation usage --config CONFIG.json --start 2026-10-13T00:00:00Z --end 2026-10-13T01:00:00Z
python -m geecomputation status --config CONFIG.json --operation projects/PROJECT/operations/OPERATION_ID
python -m geecomputation quota --config CONFIG.json --project-number OPTIONAL_NUMERIC_PROJECT
python -m geecomputation train --config CONFIG.json
```

`manifest` is entirely offline. `inspect` queries collection counts, at most 8 indices, and the first scene's band types/projection for a small ROI; it does not train. `preview` computes a monthly RGB PNG (at most 512 px and 5 MiB); signed URLs remain only in memory, and thumbnails are not analytical pixels. `usage` requires a fixed interval with UTC offsets; end defaults to UTC now. Unknown usage returns UNKNOWN and exit 2 and must not be treated as zero. `status` reads only the specified operation. `quota` reads EE config and ServiceUsage without initializing EE; missing permission returns UNKNOWN and exit 2. Any monthly balance still requires verification of the actual tier, period, and usage; no remaining quota is invented. Only `train` explicitly triggers RF training. The CLI does not submit batch Exports or Slurm jobs or open an authentication browser.

Authentication reuses `geesampler.models.AuthConfig` / `geesampler.auth.initialize_earth_engine`. Auth supports service_account, key_file, and high_volume; existing user credentials are the default. Credential contents are not logged.

## Frozen contract

The JSON must specify project, a WGS84 bbox, post `[start,end)`, pre `[pre_start,pre_end)`, grid, output, budget_eecu_hours, and seed. Bare dates mean UTC midnight; timestamps with offsets are converted to UTC. The pre interval must end no later than the post interval starts. Accepted inputs are restricted to bbox area <=0.25 square degrees, post <=62 days, pre <=93 days, an exact WGS84 UTM CRS, a north-up 30 m affine, and explicit width/height from 1–1024. The bbox defines the query extent; the grid defines the saved extent. Verify their coverage matches before actual execution. One configuration binds one sample ROI grid.

The budget (0–10,000 reported EECU-hours) is a declaration for the current session, not measured cost or a GEE hard quota. External telemetry/task APIs use conservative guards; unknown usage cannot authorize submission. The manifest records collection/version, band mapping/order, units, QA, pre/post UTC intervals, ROI, RF definition/configuration, seed, frozen split SHA, affine, and normalized config_sha256. That hash binds the scientific and execution declarations while excluding auth keys and output paths. The CLI refuses to overwrite an existing output directory with a different configuration; choose a new output. Deduplication uses source_collection + source_index, preserving the original system:index before an EE merge adds prefixes.

## Scientific transformations

The HLS v002 common6 order is blue/green/red/nir/swir1/swir2. L30 maps to B2/B3/B4/B5/B6/B7; S30 maps to B2/B3/B4/B8A/B11/B12. EE catalog examples show reflectance directly, so the recipe uses scale=1 and offset=0 without multiplying again by the original files' 0.0001. Production use still requires verification of actual saved pixel ranges and types.

Fmask bits 1–4 must be 0, and high aerosol with bits 6–7=3 is excluded. Bit 0 is reserved and unused; the water flag is retained. Reflectance and time_ms use the common valid common6/QA mask, while QA/clear are also retained. The per-scene callback casts the entire multiband image to Double to support GEESampler's fixed GEO_TIFF requests and preserve exact millisecond clocks and integer QA values; it does not cast only the time band. NBR2=(SWIR1-SWIR2)/(SWIR1+SWIR2), with near-zero denominators marked unknown. Negative reflectance alone is not masked. MIRBI=10*SWIR2-9.8*SWIR1+2.

Monthly compositing uses qualityMosaic(-NBR2); all bands, QA, and time_ms come from the same scene, with ties resolved by EE. All internal bands use double to avoid type mismatches between empty templates and real images. Empty collections remain unknown with valid_count=0. valid_count counts common6/QA-qualified observations and cannot substitute for the number with a valid NBR2 denominator. RF inputs are fixed to post_NBR2/post_MIRBI/delta_NBR2/delta_MIRBI/delta_NIR/delta_SWIR1, with delta=post-pre. Both periods use minimum-NBR2 composites. This is a literature-inspired simplification, not an exact reproduction. The monthly RF cannot directly infer dates from individual scenes, and this package does not implement burn-interval estimation.

An optional s1 configuration such as `{"relative_orbit":34,"pass":"DESCENDING"}` selects the same orbit/pass, IW, 10 m, and VV/VH, converts dB to linear sigma0 power, and averages in power units; the returned values remain power. DEM is fixed to COPERNICUS/DEM/GLO30_2024_1. After mosaicking, it is projected to the specified UTM 30 m grid before slope and aspect_sin/aspect_cos are computed (converting degrees to radians). This is a DSM and may include vegetation/buildings. These optional functions are provided separately; they do not change the six optical features or remove optical samples when a modality is missing. No ablation benefit has been established.

## HLS–S1–DEM fused discovery pilot

`geecomputation.fused` implements the `hls-s1-dem-monthpair-v1` contract separately from the existing `core6` workflow. `feature_image(region, year, month, orbit_pass=..., relative_orbit=..., grid_crs=..., grid_transform=...)` returns 29 RF predictor bands, 8 separate QA bands, and half-open UTC monthly windows as a lazy EE graph. Predictors use only HLS SR/pre-post changes, S1 VV/VH from the same relative orbit and pass, and DEM. MCD64, FireCCI, GlobMap, and hotspots are used only as training candidates or subsequent comparisons, never as predictors. Each month's pre period is `m-1`; the post period is the valid HLS median for `m`. Predictions remain masked where the paired HLS observations are unavailable. This v1 does not equate monthly compositing with a burn date.

S1 GRD in EE is already in dB: convert to linear sigma0, average within the same orbit/pass, and convert back to dB for the pre/post difference. Missing paired S1 uses a `-99` dB sentinel and an explicit `s1_missing=1`, preserving valid HLS pixels. DEM provides Copernicus GLO30 DSM elevation/slope/aspect context. `followup_evidence(region, year, month)` queries HLS NBR, valid counts, and actual dates for `m+1` only within a bounded candidate ROI. Missing persistence must not automatically reject rapidly regreening burns. A more precise `(last valid unburned, first valid burned]` interval requires manual or algorithmic checks of valid original scenes; the current monthly composite clock is not that interval.

`train_rf(qualified_feature_collection)` fixes 100 trees, two classes `0/1`, and an uncalibrated `MULTIPROBABILITY` vote array. `predict_month(model, feature_product, threshold=...)` returns scores, candidates, 8 observation QA bands, and the `s1_missing` flag. When exporting GeoTIFFs, normalize nonfinite fill values to unknown before comparing masks; do not treat `-inf` as a numerical prediction. The training table must be frozen and supplied by a samples release with eligibility for the intended use. The function does not grant label eligibility. The BARD holdout cannot be used for training, thresholds, or distribution distillation. RF `MULTIPROBABILITY` does not support `classifier.confusionMatrix()`; read `classify()` results on the frozen development set and count by event group.

Native QA for product comparisons cannot be inferred from `BurnDate.mask()`: MCD64/FireCCI `BurnDate` may be masked for unburned cells in EE. For MCD64, use the QA land/valid/shortened bits and FirstDay/LastDay to check mapping support on the event date. For FireCCI, read both the `ConfidenceLevel` mask and `ObservedFlag=-1/-2`. Native 500/250 m grid QA establishes product coverage, not 30 m ground truth. [Official MCD64 bands/QA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD64A1); [official FireCCI51 bands/flags](https://developers.google.com/earth-engine/datasets/catalog/ESA_CCI_FireCCI_5_1).

Verification covers the offline 29-band/source-isolation graph, real EE HLS/S1 values at a single point, technical training of a 100-tree EE RF, and exploratory scoring from BARD sources. Training on cost-only labels does not establish a scientific mapping model. Frozen inputs, EECU tags, job receipts, and eligibility limits are recorded in `GlobFireProcMnt/runs/2026-10-19-s1s2-omission-discovery/FUSED-JOBS.md`. Model assets are not exported; Batch may materialize only prediction IMAGEs or interpretation tasks.

## RF inputs and outputs

Add `labels={"asset":"projects/PROJECT/assets/FROZEN_SAMPLES","frozen_split_manifest":"split.json","max_samples":5000}` to the configuration. The frozen local JSON format is:

```json
{"events":{"event_A":"train","event_B":"test"},"supports":{"block_A":"train","block_B":"test"}}
```

Missing files, invalid mappings, unknown groups, and FeatureCollection splits that disagree with the mapping are rejected. The SHA256 of the manifest's original bytes is recorded in the results. This manifest is a grouping contract, not a substitute for manual label verification; retain the version of any remote asset being read. After validation and hashing, the training FC is built from these bounded local rows (geometry=null), and the model graph no longer references the remote asset. Subsequent asset changes do not alter the constructed training graph.

Use either labels.local_samples for a local GeoJSON FeatureCollection or labels.asset for a remote asset, strictly one or the other. Local tables must be ≤max_samples (capped at 10,000) and 10 MiB; the file SHA binds them to the manifest. Small training tables do not need to be uploaded as assets first. rf.input_samples(cfg) returns (ee.FeatureCollection, provenance) without uploading geometry; sample_source_info(cfg) provides a fully local preflight check. The CLI and benchmark share this entrypoint. The FeatureCollection must already contain the six features and label/split/event_id/support_id/label_source/label_eligible. Pixel ground truth is not generated automatically from perimeters. label is an integer: 0=unburned, 1=burned; unobserved samples are not trained on. split is train/validation/test. The same event or support must not cross splits, and support_id must account for spatial blocks and overlapping reference supports. label_eligible must be explicitly true. label_source is manual/reference/agency/media_verified/weak; weak is allowed only in train. Train and every nonempty validation/test split must contain both classes. Nonfinite features and leakage are rejected.

At most max_samples+1 rows (with max_samples capped at 10,000) are requested to detect overflow rather than silently truncate. The rf configuration permits trees=50/100/200 and feature_set=core6/core6_dem (default 100/core6). core6_dem adds elevation/slope/aspect_sin/aspect_cos, which the samples must explicitly contain. Seed and MULTIPROBABILITY are fixed; consecutive integer classes [0,1] fix the uncalibrated score order to score_unburned/score_burned. An explicit explanation query triggers training. Outputs are rf-training.json, rf-model-graph.json, and rf-score-graph.json. The graphs are reproducible computation recipes; evaluating them again reruns training. They are **not persistent weights or EE Asset checkpoints**. No independent validation metrics are provided, and accuracy acceptance is not claimed.

## GEESampler adaptation

The bounded interfaces `rf.freeze_training_snapshot` → `rf.build_rf_model` → `rf.predict_rf` separate one-time sample validation from the inference graph for each core. Multiple EE evaluations may still repeat training; this is not persistent model storage. `regional.build_region_plan` provides core/query/output grids and exclusive ownership within one UTM zone; query halos must be cropped back to their cores. `candidates.index_saved_candidates` / `relate_components` preserve actual candidate/unknown masks and component spatial relations without assigning events or burn dates. `transfer.product_sampler_arguments(cfg, products)` hands known workflow products directly to the existing GEESampler engine, avoiding scene-catalog selection requests. Original HLS/S1 sources still require enumeration and verification.

`monthly.resolve_burn_month_support` works entirely offline to organize callers' existing state evidence into `(last unburned, first burned]` intervals and possible months, retaining cross-month ambiguity, missing endpoints, and the `as_of` cutoff. It does not derive state evidence from current composite clocks/RF scores or implement a monthly detector, complete support raster package, or event closure.

API parameters, minimal examples, limitations, and evidence levels are in `references/computation/bounded-apis.md` of the same Skill version. Offline planning or reuse of a lazy model does not establish that the server computes it only once. These interfaces do not automatically export original scene series together.

`geecomputation.recipe.hls_scene_collection(sample, *, config_path)` matches collection_builder(SampleRecord) and returns per-scene HLS. SCENE_BANDS is common6 + Fmask/clear/NBR2/MIRBI/time_ms. `frozen_grid(sample, grid_spec, *, config_path)` matches grid_builder and returns a fixed computePixels grid. One configuration applies only to the current frozen ROI; the same grid cannot be reused for different events.

```python
from functools import partial
from geecomputation.recipe import hls_scene_collection, frozen_grid
from geecomputation.features import SCENE_BANDS
from geesampler.models import PatchGrid, SceneSelection

builder = partial(hls_scene_collection, config_path='CONFIG.json')
grid_builder = partial(frozen_grid, config_path='CONFIG.json')
# Pass to the existing sampler.download_patch_series(..., builder, bands=SCENE_BANDS,
# grid=PatchGrid(size=128, scale=30), grid_builder=grid_builder,
# selection=SceneSelection(mode='all', start_offset_days=-30,
#                          end_offset_days=31, max_scenes=EXPLICIT_CAP))
```

Per-scene downloads require an explicit anchor date, half-open offset interval, and max_scenes. The callback's configured interval intersects GEESampler's scene selection. A finite cap does not prove complete enumeration. Verify grid/QA/time/values after saving; metadata cloud percentage, exit codes, and PNGs do not establish dataset acceptance. The two-scene validation of actual saved pixels for this adapter is recorded below. Example configurations still require replacement with your own frozen inputs.

## Current verification status (2026-10-13)

Follow-up work has exercised the user-specified workflow: feature extraction from 400 original agency weak sample supports, RF100 training/inference, export of a 21-band result image, GEESampler pixel acquisition, and GeoJSON candidate-boundary verification. The same result-export path also passed with 8000 existing samples. Model assets are no longer part of this workflow. Both runs used a fixed 256×256, 30 m UTM grid, and auxiliary DEM did not enter the core6 RF. Actual timings/EECU and evidence for each stage are retained by the calling project in `runs/2026-10-13-s1s2-workflow-v2-*`; this pilot is not extrapolated to a global completion time.

Saved-result acceptance jointly checks the ranges/sums of both class probabilities, unknown masks, pre/post observation half-open UTC windows, Fmask/clear/integer observation counts, the pixelwise union of candidate boundaries within the grid, and out-of-bounds geometry. Original TIFFs returned by GEESampler may lack band names. `workflow.finalize_transfer` fills in names using the frozen computePixels `bandIds` order, rejects conflicting existing names, and preserves the original transfer file. The `geodesic=False` field on Point geometries returned by EE is removed according to Point reconstruction rules before reentering the Python API; coordinates and source SHA remain unchanged.

Offline tests cover UTC/30 m, QA, negative values/zero denominators, same-scene selection/empty collections, deltas, missing optional modalities, class support/leakage, frozen split/hashes, manifest/CLI, and graph serialization with the actual existing ee client. Tests use EE's bundled algorithms fixture and block network access; they neither authenticate nor train online. Serialization does not prove server executability, source access, pixel correctness, or efficiency targets.

Real EE authentication, inspect, preview, quota/usage reads, and two-scene GEESampler saved pixels have been verified. The quality_nbr2 server-compatibility fix has passed revalidation. Evidence for the anomalous S30 footprint and equal values with the optional native filter for one ROI is below. RF complexity measurements are recorded in separate calling-project run bundles; offline tests are not evidence of online training. Small-ROI evidence covers batch submission of a result IMAGE, terminal-state reads, and subsequent GEESampler saved pixels. Ambiguous-submission recovery remains tested only offline. Online evidence for larger regions, pipeline throughput, and new adapters must be assessed separately for each release bundle.

References: [HLS S30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002), [HLS L30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002), [DEM 2024_1](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_DEM_GLO30_2024_1), [RF API](https://developers.google.com/earth-engine/apidocs/ee-classifier-smilerandomforest).

## Complexity specifications and conditional single-year estimates

```bash
python -m geecomputation.benchmark specs --config CONFIG.json
# Only explicit execute authenticates and computes; this session did not run it:
python -m geecomputation.benchmark execute --config CONFIG.json
python -m geecomputation.benchmark estimate --metrics MEASURED.csv --coverage COVERAGE.json --year 2020
```

Six specifications combine 50/100/200 trees × core6/core6_dem under the same training samples (asset or frozen local table), frozen split JSON, inference grid/ROI, pre/post intervals, QA, and seed. Detection inputs are explicitly HLS; DEM is added only in the specified variant. process_aux such as GEDI/VIIRS_FRP are defined separately and do not enter the detection RF. All specifications share comparison_contract_sha256; each variant has its own run_spec_sha256.

execute is a small sequential experiment with randomized order under a fixed seed; it submits neither batch exports nor Slurm jobs. Every variant reads the same ten features from the input FC and validates the complete samples/splits. Sorted original-property hashes freeze the cohort so missing DEM cannot change the training samples; every variant must match after the baseline snapshot. Two synchronous requests—an explicit explanation and fixed-grid score mean/count—trigger training/inference. This is neither a complete pixel download nor a production performance benchmark. Logs in output/benchmarks/UUID record each stage's wall_seconds, local run_id, and remote workload tag (synchronous APIs have no batch operation ID). Immediate EECU is partial telemetry only: reported_eecu_seconds is a separate field (possibly null), telemetry_complete=false, and eecu_seconds=null; unmeasured payload/queue values are null. A nonzero point does not establish completeness, and EECU is not inferred from wall time.

Estimate inputs support CSV or a JSON list of metrics. Each row must contain at least stratum/area_month_km2/compute_wall_seconds/eecu_seconds/payload_bytes/queue_seconds/synthetic/run_id. Measured rows use synthetic=false and explicitly identify the same variant and frozen pipeline_id. Empty numeric CSV cells become null. area_month_km2 is region area accumulated across months, in km²·month. The coverage JSON format is `{ "year":2020, "months":12, "synthetic":false, "strata":[{"stratum":"NAME","activity":"low_activity_non_event","area_month_km2":ACTUAL_AREA}, ...] }`. It must include a low-activity non-event stratum and cover every target stratum. Total coverage and area by month are supplied and deduplicated upstream; event regions cannot substitute for global coverage.

The formula is sum(target_area_month × measured_stratum_cost_density), with point density=sum(measured_cost)/sum(measured_area_month). The range uses the minimum/maximum observed densities within each stratum; it is **not a confidence interval or a validated extrapolation-error bound**. One observation cannot provide statistical error, and seasonal, cloudy, ecoregion, and activity-stratum representativeness require additional validation. serial_compute_wall_seconds is a conditional reference equal to the sum of sequential-request wall times; production_wall_seconds remains null. EECU, payload, transfer time under a declared bandwidth, and observed queue time are reported separately. Concurrency and production queueing need separate real scheduling measurements; EECU-hours cannot be treated as wall-clock hours.

Missing measurements, any unmeasured target stratum, unknown/incompletely reconciled EECU, or unmeasured payload returns cannot_estimate/exit2 without generating real global numbers. estimate_year requires telemetry_complete=true on every record. Real records also require telemetry_confirmation.method (manual_reconciliation or independent_billing_reconciliation) and evidence_reference. examples/computation/synthetic-metrics.json and annual-coverage.synthetic.json are only formula tests and reject estimation by default. Even --synthetic-test requires explicit telemetry_complete=true before returning a synthetic_only formula test. The current examples without a completion flag still return cannot_estimate. Synthetic values cannot be mixed with measurements or presented as measured evidence in a paper/budget.

### Refresh telemetry without rerunning computation

```bash
python -m geecomputation.benchmark refresh-telemetry --config CONFIG.json --records OUTPUT/benchmarks/RUN_ID/records.json --delay-seconds 300
```

The original project/workload tag/start/end are retained, with the query window extended to original end+delay. If that window has not yet ended, the tool records a waiting state without querying, sleeping, initializing EE, training, or running inference. Refresh saves observation history and the original interval, updates only reported_eecu_seconds, and never automatically marks telemetry_complete. Neither a fixed elapsed delay nor the appearance of a point proves billing completeness.

Complete accounting requires manual or independent billing reconciliation: verify that the workload has terminated, the project/tag and full execution interval match, and delayed/retry requests are included. Then write the independently reconciled total into the measurement input's eecu_seconds with telemetry_complete=true, the reconciliation method, and a reviewable evidence_reference. The latest reported point cannot simply become the complete total. Refresh preserves manually completed records; the tool does not replace completeness verification.

In CSV, declare complete accounting with telemetry_complete=true. The method/reference can be supplied as telemetry_confirmation_method and telemetry_confirmation_evidence_reference columns or as a JSON object in a telemetry_confirmation cell. A true flag alone or a nonzero reported point alone is insufficient for extrapolating real metrics. JSON metrics use a telemetry_confirmation object.

## Actual saved pixels and native-filter evidence (2026-10-13)

The independent run bundle `runs/2026-10-13-s1s2-pixel-verification` used an isolated Japan node and exact tag gfp-s1s2-pixels. The real GEESampler callback downloaded L30 Aug17 and S30 Aug5. Hot SQLite state stayed in /tmp and was copied sequentially after termination. Both TIFFs were 11×128×128, entirely float64, EPSG:32736, with the exact 30 m affine. Independently recomputed QA clear and Fmask matched; time_ms at every pixel exactly equaled the source values 1597652047000 and 1596614769000, respectively; recomputed NBR2/MIRBI differences were 0. Valid pixels were 1944/16384 (11.865%) for L30 and 15551/16384 (94.916%) for S30. Scene metadata cloud percentages 0/4 do not establish pixel acceptance. The two files totaled 662107 compressed bytes, with approximately 14 s of sampler time; this is not sustained throughput that can be extrapolated. Actual nodata was -inf, retained as a string in the JSON evidence.

For the first S30 filterBounds scene T01KAA, image.geometry() returned global bounds/Infinity area. Its actual B2 grid was 3660×3660, EPSG32601, affine(30,0,99960,0,-30,-1899960), with a raw footprint ring at ±179°/-17..-18°, outside the African ROI. Constructing Rectangle([0,0,width,height],proj=B2.projection(),geodesic=false) using the source-band affine Projection gave a measured native area of approximately 12.07e9 m² and intersects ROI=false. Local T35MRT gave approximately 12.08e9 m² and intersects=true, consistent with offline pyproj corner checks. The native rectangle describes stored support, not internal valid/clear support.

The optional hls_native_dimensions=[3660,3660] configuration applies this native-extent intersection filter before spectral preprocessing. Its default is empty; it does not assume every source/future version is 3660, and dimensions must be checked before enabling it. Optional hls_tile_ids=["T35MRT"] is an explicit planning restriction; the default is empty, and tiles are not automatically excluded by zone. The manifest records selection_scope and cannot_claim_enumeration_completeness. This ROI/2020Aug originally had 217 scenes (L4+S213); native and tile35 filtering each retained 9 (L4+S5). On the fixed 128×128, 30 m grid, all 12 bands of the monthly composite (including QA/time/valid_count) had max_abs_difference=0 and mask_difference_counts=0. Equal values have been verified only for this ROI/month, without claims of complete global enumeration, general correctness, or speed gains.

ee.profilePrinting saves query/comparison computation profiles in EECU·s/PeakMem units. It is not independent billing and cannot serve as complete reported usage or a monthly balance. Real reduceRegion requests showed that maxPixels must account for multiple bands: 12×128²=196608 rather than just 16384. Correct maxPixels without changing 30 m/affine and without bestEffort. The benchmark's two-score branch likewise budgets 2×width×height.

## Additional real API boundaries

On 2026-10-13, 8000 actual samples hit the 5000-element limit of FeatureCollection.getInfo. Materialization changed to explicitly bounded toList(max_samples+1), retaining overflow detection and the literal snapshot. Cloud Monitoring queries explicitly bind resource.labels.project_id and validate returned project/tag to avoid charging other projects in a Metrics Scope.

Small classifier Exports verified task registration, submission, and terminal-state reads, but model persistence **did not pass value-equivalence acceptance**. MULTIPROBABILITY export was rejected. Although a CLASSIFICATION asset SUCCEEDED, both paths—selecting the mode before or after training—produced loaded predictions that disagreed with the corresponding fresh graph on 16 of 32 rows (all loaded predictions were 0), with an unknown root cause. These experimental assets cannot be used. Direct inference from the original lazy graph works; it must not currently be replaced with a Classifier.load path. Export SUCCEEDED, a saved graph, or successful loading does not establish prediction correctness.

## End-to-end entrypoint: samples → features → RF → inference → prediction products

The current workflow no longer depends on persistent model assets. `workflow.py` reuses Config, features, rf, tasks, monitoring, and GEESampler. The complete workflow fixes `core6`, with auxiliary DEM excluded from RF. The product has 21 float64 bands: two uncalibrated class scores, a candidate layer with default threshold 0.5, six RF optical features, pre/post valid_count/time_ms/Fmask/clear, and four independent aux_dem_* bands. Here core6 means the six detection features, not raw common6 reflectance. Candidates are not accepted burned area, and time layers identify selected scenes rather than burn intervals. VIIRS/GEDI are currently recorded as excluded and must not be claimed as exported.

```bash
# Freeze the contract offline, without authentication or computation.
python -m geecomputation.workflow --config CONFIG.json --manifest-only
# Use a frozen local training table; synchronously retrieve bounded real boundaries,
# and save the lazy product graph and export plan.
python -m geecomputation.workflow --config CONFIG.json --threshold .5 --boundary-cap 2000
# Explicitly resample in GEE, then train, infer, transfer actual pixels,
# and verify pixel/boundary consistency.
python -m geecomputation.workflow --config CONFIG.json --sample-points POINTS.geojson \
  --threshold .5 --boundary-cap 2000 --transfer --hot-dir /tmp/ALLOCATED_JOB/workflow
```

`POINTS.geojson` is a GeoJSON FeatureCollection of already-authorized labeled point supports. It preserves label/event_id/support_id/split/label_source/label_eligible and original source/version/date/provenance properties; the entrypoint does not grant additional label eligibility. Support points must not contain old RF/DEM features that would conflict with new sampling. `sampleRegions` uses the frozen CRS/affine, drops points whose core is unobserved, saves `sampled-samples.geojson`, and passes it directly to the existing `train_rf` checks for frozen splits, both classes, finite values, and leakage. Sampling includes DEM and pre/post counts. Only in this sampled table, missing DEM is filled with -9999; that does not establish DEM observability or change the optical cohort. Actual product DEM retains its independent mask. Perimeter-interior/exterior ground truth is not constructed automatically; the calling project owns its authorized weak rules and point sources. The default cap is labels.max_samples; any cap overflow is rejected without silent truncation.

The input contract freezes the support-point SHA, source collections/intervals, seed, RF, output grid, threshold, and bands. effective-workflow-contract.json additionally includes the newly sampled table's SHA. Sampling provenance/wall time/response bytes are recorded in stages.json; training.json records the existing RF sample snapshot SHA. The CLI neither exports nor loads model assets and submits neither Slurm jobs nor batch exports. `--transfer` requires an allocated node-local hot-dir and calls the existing single-scene path in GEESampler DownloadEngine. SQLite/retry/resume reuse the existing queue. GEESampler's actual workload tag is recorded for the stage to avoid querying transfer cost with an outer tag. At termination, product.tif and sampler-summary.json are saved sequentially to the calling project's output.

Product support is exactly the rectangle defined by the output affine × width/height. Boundaries use eight-connected `reduceToVectors` in the same CRS/affine for candidate=1 only, returning WGS84 GeoJSON. The bounded client path uses `toList(boundary_cap+1)` and switches to table export on overflow. Saved-result acceptance checks dtype, CRS/affine/shape/band order, core/score/candidate masks, threshold, and valid observation counts. It projects the actual GeoJSON back onto the grid to verify pixelwise agreement of polygon union and candidates. Missing files, unobserved pixels filled as negatives, and incomplete boundaries are rejected. A valid empty boundary set with no candidates still requires valid observations. Missing auxiliaries cannot remove valid core pixels. `verification.json` proves only saved-product consistency, not independent accuracy or global readiness.

To reduce recomputation of lazy graphs, a primary executor can first export a **prediction-product image Asset**, then obtain boundaries/actual pixels from the completed, verified product Asset. This is not a model Asset. The basic interface is:

```python
from geecomputation.workflow import (
    sample_points, build_workflow, prepare_exports, boundaries_from_raster,
    bounded_boundaries, product_collection, sample_grid, verify_artifacts,
)
# FC and provenance: retain input point properties without duplicating RF validation.
sampled_fc, provenance = sample_points(cfg, 'POINTS.geojson')
# After the primary executor freezes sampled_fc to bounded local GeoJSON
# and replaces cfg.labels.local_samples:
products = build_workflow(cfg, threshold=.5)
prepared = prepare_exports(cfg, products, asset_id='projects/PROJECT/assets/FRESH_PRODUCT')
# Pass prepared['image'] to existing tasks.submit_ee_export(..., contract=products['contract'], ...).
# Do not call Task.start; retain the journal, known operation, and reported-budget guard.
# Pass drive_folder for table export; prepare_exports returns table.
# Preparing an export does not submit it.
# After checking the image Asset's terminal state and contract:
boundary_fc = boundaries_from_raster(cfg, ee.Image(FRESH_PRODUCT), threshold=.5)
boundary_geojson = bounded_boundaries(boundary_fc, cap=2000)
```

`build_workflow` returns model/scores/candidate/core6/quality/aux/raster/boundaries/metadata/contract/bands. RF still trains and infers directly through lazy graphs without `Classifier.load`. `product_collection(sample, products=...)` and `sample_grid(sample, grid_spec, cfg=...)` can be passed through functools.partial to the existing sampler.download_patch_series. The single scene uses system:index=workflow_product; system:time_start=the post interval's start is only product packaging and does not claim that every pixel was observed on that day. For the Asset-read path, products['raster'] can be replaced by the completed ee.Image while retaining the same bands/grid/contract.

`StageRecorder.stage(name, evaluates_graph=True)` saves wall time, UTC time, a separate workload tag, a profile file, and an optional payload_bytes value for each sequential request. Feature/training graph construction mainly performs sample-validation RPCs. Model and image dependencies execute during later boundary/export/acquisition requests and may be recomputed. No request's wall time/profile is pure training time; `sum_pure_train_seconds=null`. `refresh_usage(end=UTC_OFFSET_TIME)` uses the existing monitoring to save delayed reported completed/in-progress views separately. They are never added together or automatically marked complete: eecu_seconds=null and telemetry_complete=false until independent external reconciliation. The synchronous CLI accepts --usage-end; a separate executor can retain its recorder and refresh later. profilePrinting is diagnostic evidence, not independent billing. The resampled 400-support workflow and the 8000-existing-sample variant require separately frozen runs/contracts and costs; their costs cannot be combined into a single workflow total.

Additional offline tests cover unknown masks, threshold/contract hashes, output grids and prepared images/tables, sampling properties, actual raster/boundary unions, incomplete-output rejection, permitted missing auxiliaries, offline CLI behavior, and partial cost records. Separate calling-project run bundles supply online evidence. Offline serialization is not server or pixel acceptance.

## Bounded original-observation delivery interface (2026-10-15)

`observations.observation_query_plan` can locally freeze original HLS L30/S30 or S1 collections, a GeoJSON ROI, and a half-open UTC window. Only `enumerate_observations` issues authenticated, paginated metadata requests. Only manifests with `complete && pagination_complete` may enter `planning.build_cube_plan`. Query failures, complete zero-scene results, out-of-contract scenes, and scenes whose native grids do not intersect are retained separately. Native-extent intersection still does not establish clear/valid pixels. `listImages` has no workload-tag EECU accounting; query wall time is not converted to EECU.

```python
from geecomputation.observations import observation_query_plan, enumerate_observations
from geecomputation.planning import build_cube_plan, select_support
from geecomputation.delivery import deliver_unit, verify_delivery

query = observation_query_plan('NASA/HLS/HLSL30/v002', project=PROJECT,
    geometry=ROI_GEOJSON, start='2020-03-30T00:00:00Z',
    end='2020-08-27T00:00:00Z', workload_tag=TAG)
# Execute only after budget/resource checks and ee.Initialize(project=PROJECT):
source = enumerate_observations(**{k: v for k, v in query.items()
                                   if k != 'query_plan_sha256'})
assert source['complete'] and source['pagination_complete']
plan = build_cube_plan(source_manifest=source, supports=ORIGINAL_SUPPORTS,
    grid=FROZEN_UTM_GRID, window=FROZEN_UTC_WINDOW, bands=HLS_FLOAT32_BAND_CONTRACT)
one_support = select_support(plan, SUPPORT_ID)
# The caller delivers plan['units'] sequentially through the existing
# GEESampler DownloadEngine, using for each unit:
# deliver_unit(unit, engine=engine, hot_dir=NODE_LOCAL,
#              archive_dir=PERSISTENT_ARCHIVE, trial_id=TRIAL_ID).
# Finally pass real receipts to verify_delivery({'plan': plan, 'deliveries': receipts}).
```

`build_cube_plan` deduplicates by original scene fingerprint, fixed 30 m UTM chip, and band/QA contract, preserving each original support's in-place geometry, window, and missing/cutoff reasons. The 9 HLS bands comprise common6, Fmask, source_valid_bits, and clear. The 5 S1 bands provide original-observation support for comparable same-orbit/same-pass analysis and do not automatically enter the current RF. `deliver_unit` uses GEESampler download/recovery rather than building another queue. Archive receipts include original scene/time, bands, grid, SHA, and workload tag. `verify_delivery` checks actual TIFFs and valid pixels over the original support. `complete` means all physical units are present; `observed_pixels` and scientific label eligibility must be assessed separately. The calling project must still pin both release SHAs, budget, Slurm allocation, and actual job/task receipts.
