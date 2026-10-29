# GlobFireProcMnt integration

These conventions apply **only to `/nfs-data0/home/lyh/workspace/projects/GlobFireProcMnt`**, not to HPGEE use in other projects. Read that project's current instructions and protocol when doing its work. Repository consolidation does not change ownership, label eligibility, historical verification, or execution authorization.

For GlobFireProcMnt, S3+S4 owns the computation APIs/Skill guidance, GEE model training, Mapping V1 and computation-efficiency work. S1+S2 owns acquisition, sample strategy and sample publication; it consumes released computation APIs. Project status/protocol: `/nfs-data0/home/lyh/workspace/projects/GlobFireProcMnt/STATUS.md` and `/nfs-data0/home/lyh/workspace/projects/GlobFireProcMnt/docs/stages/INTERFACE.md`.

Use the project's `scripts/stage_exchange.py resolve --channel compute --artifact geecomputation --purpose inspect` to obtain the ready code/Skill snapshot and Python path. At task launch pin the returned release/manifest SHA; use its pythonpath before imports. The HPGEE canonical repository is `/nfs-data0/home/lyh/OutSideSkillLib/efficient-gee`; compatibility paths may point there. It is a maintenance location, not an immutable release. Use `--channel samples --artifact weak_rf_8000 --purpose cost_benchmark --feature-contract hls-common6-v2` for the existing permitted weak table; resolution returns its labels configuration. New independent validation/global training eligibility must be published explicitly, never inferred from a source name or label_eligible field.

S3+S4 publishes code, Skill/examples, API/feature compatibility and verification evidence together, then atomically moves compute/latest.json. S1+S2 independently moves samples/latest.json after sample/observation/qualification checks. Keep immutable releases and the last compatible ready version; do not wait for a new handoff article or change versions mid-run. A failed or incomplete producer does not replace latest. Neither channel grants new execution scope or budget.

## Frozen project data and evidence

Preserve existing runs, code/Skill snapshots, manifests, manifest hashes, and the last compatible ready release. Repository consolidation does not rewrite historical releases, automatically publish a new one, change `compute/latest.json` or `samples/latest.json`, or extend allowed uses. Keep feature contracts and numerical recipes unchanged unless the corresponding scientific change is explicitly made and versioned.

In this project's fused HLS–S1–DEM work, freeze the BARD holdout and event/spatial exclusions before graph construction. BARD holdouts cannot participate in training, threshold selection, or distribution distillation. Use a frozen non-BARD development set for grouped `classify()` threshold checks. Cost-only training and exploratory scores on previously exposed supports do not establish a scientifically qualified mapping model or independent validation.

Keep source lists, input SHAs, actual job/task IDs, failures, budgets, and measured evidence in this project. Respect its stage owner and single-writer channel rules. HPGEE consolidates the `geesampler` and `geecomputation` code; it does not merge the sample and compute release channels or grant new execution permissions.
