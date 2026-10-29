---
name: efficient-gee
description: Use when the user explicitly requests HPGEE or this Skill for Earth Engine computation, sampling, transfer recovery, or measured throughput tuning.
---

# Efficient GEE with HPGEE

The canonical code repository is `/nfs-data0/home/lyh/workspace/projects/HPGEE`. This installed Skill contains instructions and helpers only; its versioned source is `skills/efficient-gee` in the repository. HPGEE uses one `hpgee` package organized by capability: catalog and sampling, planning and computation, shared transfer and accounting. Use `hpgee --help` or `python -m hpgee --help`; legacy imports and commands remain supported through compatibility aliases. Keep this Skill explicitly invoked and outside automatic discovery.

Use the current task's authorization and budget. A declared ceiling, ready artifact, historical run, or available API does not authorize additional execution. Pin the actual code/configuration used; do not change versions during a run.

Read only the guidance needed for the task:

| Task | Read |
|---|---|
| Source enumeration, features, RF, previews, products, exports, quota | [Computation](references/computation.md) |
| Sampling, metadata, transfer, retry/resume, saved-pixel acceptance, tuning | [Sampling](references/sampling.md) |
| Raw HLS/S1 scene series | [Graph/delivery contract](references/computation/raw-series.md) and [transfer/accounting](references/sampling/raw-series.md) |
| GlobFireProcMnt ownership, releases, or sample eligibility | [Project integration](references/project-integration.md), only for that project |
| This user's cluster paths and historical helper wrappers | [Machine notes](references/sampling/machine.md), after confirming the environment |

Freeze collection/version, scene identity, native support, UTC half-open windows, bands/order, units, masks, grid/affine, and intended label use. A failed/capped enumeration is not a complete empty source. Unobserved is not unburned; acquisition clocks are not burn dates. Missing optional modalities must not discard valid core observations.

Keep image math and reducers server-side and client reads bounded. Use `hpgee.transfer.engine.DownloadEngine` for transfer; observation and product adapters share `FrozenSceneRequest`. Preserve nonempty common valid support, per-band finite pixels, source/grid identities, original bytes, and independent QA before accepting saved outputs. Task success, thumbnails, and matching counts do not prove dataset acceptance.

Persist task intent and stable identity before submission; reconcile ambiguous owned requests/operations before retrying. Keep credentials and signed URLs out of logs. Unknown/delayed usage stays unknown/partial; completed and in-progress EECU views overlap and cannot be summed. Separate wall time, useful transferred bytes, stored bytes, and EECU.

On the shared cluster, use `slurm-workflow` before substantial CPU/I/O, long downloads, GPU work, or Slurm management. Keep hot SQLite state on allocated local storage and durable checkpoints sequential. Use `with-mihomo` for authorized or demonstrably necessary route tests; preserve shared services and Codex's active proxy.

Exercise the actual CLI and report offline tests, graph serialization, authenticated queries, saved-pixel validation, and scientific acceptance at their actual evidence levels. Keep run-specific measurements, jobs, failures, and budgets in the calling project. Public API guides are `docs/computation.md` and `docs/sampling.md` in the code repository. Read `docs/architecture.md` when changing capability boundaries or shared runtime contracts.
