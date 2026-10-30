# HPGEE — High Performance Google Earth Engine

HPGEE builds Earth Engine products and delivers their pixels through one resumable
execution system. Source sampling and derived computation share exact grid
contracts, authentication, transport, monitoring, and durable transfer recovery.

## Install and use

Use Python 3.10 or newer:

```bash
python -m pip install -e '.[all,dev]'
hpgee --help
hpgee sample examples/configs/prepared_points.yaml
hpgee compute manifest --config examples/computation/small-roi.json
hpgee workflow --config examples/computation/small-roi.json --manifest-only
```

`sample` requires an authorized, configured Earth Engine project. The two manifest
commands are offline. `python -m hpgee` also works without console scripts.
Use a clean environment when replacing separately installed `geesampler` or
`geecomputation` distributions, whose module paths overlap the compatibility layer.

| Capability | Command | Guide |
| --- | --- | --- |
| Sample selection and resumable pixel delivery | `hpgee sample` | [Sampling](docs/sampling.md) |
| Source catalogs and incremental metadata | `hpgee catalog`, `hpgee metadata` | [Sampling](docs/sampling.md) |
| Frozen contracts, previews, exports and usage | `hpgee compute` | [Computation](docs/computation.md) |
| Samples → features → RF → products and delivery | `hpgee workflow` | [Computation](docs/computation.md) |
| Bounded computation comparisons | `hpgee benchmark` | [Computation](docs/computation.md) |
| Transfer throughput reports | `hpgee progress` | [Sampling](docs/sampling.md) |

Specialized legacy commands remain available through `geesampler`, `geecompute`
and `geecompute-benchmark`. Existing dotted callback paths and Python imports
remain aliases to the canonical implementations; see the
[module migration map](docs/module-migration.json).

## Architecture

```text
src/hpgee/
  core/          Exact pixel grids, data models, storage and file locking
  runtime/       EE authentication, HTTP transport and retry policy
  catalog/       Source metadata, catalog persistence and scene resolution
  sampling/      Sample inputs, configuration and distributed orchestration
  planning/      Observation, cube and regional support plans
  compute/       Lazy feature graphs, RF and scientific recipes
  workflows/     Product contracts and detection orchestration
  transfer/      Shared download engine and frozen observation/product adapters
  acquisition/   Frozen-target recipes, quality cells, delivery and host results
  accounting/    Shared Monitoring reader, usage ledgers, tasks and quotas
  reporting/     Progress, resources, events and visualization
  recipes/       Source collection callbacks
  cli/           Unified command routing and capability handlers
  benchmarks/    Sampling and computation measurements
compat/          Thin aliases for previous import paths; no business logic
skills/          Versioned agent instructions, independent of runtime packages
```

[Architecture and migration](docs/architecture.md) explains shared contracts,
dependency boundaries, persisted-state compatibility, and the deliberate
separation of scientific recipes from execution infrastructure.

## Agent skill

The maintained skill source is [skills/efficient-gee](skills/efficient-gee/SKILL.md).
Install a physical copy of only the skill files:

```bash
python tools/install_skill.py --destination /path/to/OutSideSkillLib/efficient-gee
python tools/install_skill.py --destination /path/to/OutSideSkillLib/efficient-gee --check
```

The skill directory contains no repository or Python package. It remains explicitly
invoked; installation does not change automatic discovery settings. On the maintained
cluster the physical code repository is `/nfs-data0/home/lyh/workspace/projects/HPGEE`,
and its skill copy is `/nfs-data0/home/lyh/OutSideSkillLib/efficient-gee`.

## Validation and provenance

```bash
python -m pytest
python -m build
```

The offline suite checks graph serialization, exact grids, real-engine mocked
transfers, resume, accounting failure modes, old import identities, and unified
CLI dispatch. It does not establish live performance, current cloud quotas,
authenticated execution, or scientific acceptance. Validate saved pixels and
research outcomes separately within the calling project's authorized budget.

Existing scientific recipes, persisted receipts and workload prefixes retain their
identities. Original GEESampler Git history and the MIT copyright notice are
preserved. The unified distribution is version 0.2.0. Historical source backups,
Git bundles and local migration records are kept outside the repository under
`/nfs-data0/home/lyh/workspace/home-organization/`.
