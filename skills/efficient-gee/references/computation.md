# Computation, source contracts, and products

Use HPGEE's `compute`, `planning`, and `workflows` capabilities. The canonical repository is `/nfs-data0/home/lyh/workspace/projects/HPGEE`; the installed Skill is `/nfs-data0/home/lyh/OutSideSkillLib/efficient-gee`; the computation guide is `docs/computation.md` in that repository. Follow current user scope; a computational budget is an upper bound, not a target for repeated work.

For GlobFireProcMnt tasks only, read [project integration](project-integration.md) for stage ownership, immutable releases, sample eligibility, and the designated BARD holdout. Those conventions do not apply to unrelated HPGEE projects.

## Bind inputs before computing

Freeze collection/version, ROI, UTC half-open intervals, native support, bands/order, scale/offset, masks and output CRS/affine. Preserve scene identity before collection merges and per-pixel acquisition time through composites. A failed enumeration is not an empty collection. Monthly packaging does not determine the observation time.

For original HLS/S1 source manifests, call `observations.enumerate_observations` on an explicitly frozen small ROI and UTC window. `listImages(region=...)` can return distant raster tiles; the adapter checks each listed scene's native band-grid footprint and retains excluded IDs/reasons. A missing grid extent makes the manifest incomplete. For AOIs wider than 5° in longitude or latitude, native exclusion is conservatively skipped; do not interpret catalog hits as measured pixel coverage. Metadata enumeration wall time is not EECU because `listImages` has no workload tag.

Labels carry valid interpretation support, source provenance, dates, geometry accuracy, intended use and frozen event/spatial split. Agency perimeters may contain unburned islands. Model products are weak supervision; media are event leads until independently interpreted. Never treat unobserved as unburned or let missing optional modalities remove valid core observations.

For the implemented HLS/SAR/DEM recipes and limitations, read [scientific recipes](computation/recipes.md). Validate saved pixels before changing scientific defaults.

For reusable RF snapshots, fixed regional core/query/output plans, known-product transfer callbacks and candidate component indices, read [bounded APIs](computation/bounded-apis.md). These interfaces preserve the existing scientific recipe; they do not implement event tracking or select a production monthly detector.

## Keep the compute graph small

Filter space/time and select bands early; perform image math/reducers server-side. Avoid client-side per-pixel loops, collection-wide `getInfo()` and gratuitous reproject/clip calls. Declare reducer/export grids explicitly. Only retrieve bounded summaries, selected IDs and previews to the client.

Use the standard endpoint first for complex composites/classification. High-volume has less intermediate caching and can consume more EECU; compare it for simple repeated pixel fetches instead of assuming it accelerates all workloads. Repeated expensive graphs may justify an EE Asset export before transfer; measure queue+compute+transfer+QA, not only remote runtime.

## Separate compute from transfer

For full-support HLS30 and S1 10/30m scene sequences, read [raw-series graph and delivery contract](computation/raw-series.md). These APIs are separate from RF features and derived observation packages.

The computation package builds lazy recipes, trains representative RF models, creates small previews and prepares computation/export tasks. `hpgee.transfer.engine.DownloadEngine` owns pixel transfer, retry, resume and file acceptance. For an already-defined workflow result use `transfer.product_sampler_arguments(cfg, products)` with the existing engine; its resolver avoids catalog selection requests. Keep actual computePixels, transfer recovery and saved-pixel QA. Source-scene discovery still uses the source adapters; do not build a second pixel queue.

Use the same frozen recipe/grid/scene identity for API, EE Asset and Drive paths. Previews are visualization-only. ComputePixels has a 48 MB uncompressed response limit; calculate actual band dtype/QA payload and leave margin. Drive is an optional measured fallback, not a mandatory staging hop. Credentials and signed URLs stay out of manifests/logs.

## Observe and recover deliberately

Read [operations and telemetry](computation/operations.md) before submitting or scaling tasks. Unknown usage is not zero; reported EECU is delayed and is not wall time. Keep run usage separate from project monthly allowance and other users' work. Do not add completed/in-progress metrics together.

Persist task identity and intent before submission. On timeout, reconcile the known request/operation before retrying; never blindly create a new task. Inspect only own journaled tasks for recovery, leaving unrelated jobs untouched. Task completion does not establish pixel correctness.

For this user's shared cluster, apply `slurm-workflow` before long downloads or substantial CPU/I/O, and `with-mihomo` for requested route tests. Use modest CPU-only allocations, node-local hot state and sequential durable receipts. Never modify Codex's active proxy or shared services.

## Acceptance

For the user's samples → features → RF → inference → output workflow, use `hpgee.workflows.detection`. Do not export model assets. A result IMAGE asset may avoid repeating the feature/classifier graph when fetching boundaries and pixels; measure its queue, computation, transfer and QA before choosing it over direct transfer. Export scores, threshold candidates, frozen detection features, observation quality/time and independent auxiliaries. An acquisition clock is not a burning-time bound.

Real `DownloadEngine` TIFFs may omit band descriptions. Package them with `workflow.finalize_transfer` using the frozen computePixels bandIds; preserve raw bytes and reject contradictory identities. Validate both binary probabilities, observation clocks/QA, unknown masks and boundary extent plus pixel union. EE Point GeoJSON may contain `geodesic=False`; omit that field only for Point reconstruction without changing coordinates or provenance.

Exercise the actual CLI, not just a mocked helper. Distinguish offline tests, serialized EE graph checks, authenticated source queries, trained/evaluated models, saved-pixel validation and accepted datasets. Report each at its actual evidence level. Keep source lists, job/task IDs, failures and current budgets in the calling project; add reusable skill lessons only after they are verified.

## Fused HLS–S1–DEM discovery contract

When a task explicitly requires fused HLS/S1/DEM detection, read `hpgee.compute.fused` and the fusion contract in `docs/computation.md`. The existing `core6` workflow must not be described as using S1. Freeze the training samples' allowed uses, event/spatial exclusions, and designated holdouts before building the 29-band `hls-s1-dem-monthpair-v1` graph. S1 comparisons use the same pass and relative orbit; convert native GRD dB to linear power before aggregation and back to dB afterward. Missing S1 retains HLS with an explicit missingness flag; missing HLS remains unknown. Call `followup_evidence` only for selected candidates to check the following month, and do not automatically reject fire because regreening removes persistent evidence.

`predict_month` must retain the 8 observation QA bands and `s1_missing`, allowing radar-coverage checks after saving. For GeoTIFF comparisons, normalize masks and nonfinite fill values to unknown before comparing the common valid pixels.

Before judging product negatives or omissions, check **native** quality: an MCD64 BurnDate mask is not proof of missing observation. Read QA bits 0/1/2 and FirstDay/LastDay; for FireCCI, read the ConfidenceLevel mask and ObservedFlag -1/-2. Hotspots, 250/500 m burned cells, and annual GlobMap labels are candidates/comparisons only, not 30 m monthly truth or RF predictor variables. Record observable points, product coverage, label eligibility, and event-level manual confirmation separately. Unconfirmed model-only patches remain an interpretation queue.

Compare standard interactive requests and Python-submitted asynchronous Batch on the same graph and 30 m affine. Verify matching pixels and complete outputs before comparing queue+computation+acceptance wall time and workload-tag EECU. For `MULTIPROBABILITY` RF, use `classify()` on a frozen eligible development set and check thresholds by group instead of calling `confusionMatrix()`. Delayed EECU remains partial accounting; completed and in-progress views must not be added. The GlobFireProcMnt-specific BARD exclusions are in [project integration](project-integration.md).

## Samples-to-products entrypoint

Use `hpgee workflow --config CONFIG --manifest-only` (equivalently `python -m hpgee.workflows.detection`) for an offline frozen contract. Explicit computation runs samples → core6 features → direct lazy RF → inference → product raster and threshold candidate boundaries. `--sample-points` accepts already-authorized labeled Point supports, preserves provenance, samples on the frozen grid, and freezes the resulting local table before the existing RF validator; it does not invent labels or eligibility. `--transfer --hot-dir ALLOCATED_LOCAL_DIR` reuses the shared `DownloadEngine`. The current workflow keeps DEM separate from RF, leaves VIIRS/GEDI unimplemented and explicit, and preserves unknown predictions as masked.

For measured runs, prefer `workflow.build_workflow` and `prepare_exports(..., asset_id=FRESH_PRODUCT)` when materializing the predicted image reduces graph recomputation. Submit this image with the existing task journal, reconcile its known operation, then obtain boundaries and pixels from that product. This is a prediction product Asset, not a classifier. Do not export/load model assets in this path. Keep image/table export prepared-only unless execution is authorized. `boundaries_from_raster` uses the identical output grid; bounded geometry overflow must switch to table export, never accept a prefix. `verify_artifacts` checks actual TIFF bands/grid/masks/threshold and actual GeoJSON candidate union before accepting saved products. Pixel consistency is not independent scientific accuracy.

Record sample construction, graph/validation, boundary, image export, transfer and verification requests with `StageRecorder` or equivalent sequential receipts. Lazy dependencies may rerun in multiple requests: a train/explain/export wall time is not pure training time. Preserve profiles and actual workload tags; delayed reported completed/in-progress usage overlaps and cannot be summed. Partial telemetry stays partial until independent reconciliation. Different sample sizes or source builds belong to distinct frozen runs, not a mixed cost total. Detailed interfaces and CLI examples are in `docs/computation.md` of the HPGEE repository.
