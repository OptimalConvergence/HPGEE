# HPGEE — High Performance Google Earth Engine

HPGEE combines bounded Earth Engine computation with concurrent, resumable pixel
sampling in one repository and one Python distribution. It preserves the
`geecomputation` and `geesampler` import namespaces and their existing commands.

## Install

Use Python 3.10 or newer. From this repository:

```bash
python -m pip install -e '.[all,dev]'
geesampler --help
geecompute --help
geecompute-benchmark --help
```

The distribution is named `hpgee`; it includes both packages. Use a clean
environment when replacing separately installed `geesampler` or `geecomputation`
distributions, because they own the same Python module paths. An existing
installation is not automatically updated by moving source files.

## Choose a workflow

| Task | Entry point | Guide |
| --- | --- | --- |
| Source catalogs, sample planning, pixel transfer, retry and resume | `geesampler` | [Sampling](docs/sampling.md) |
| Frozen contracts, features, RF workflows, product preparation and accounting | `geecompute` | [Computation](docs/computation.md) |
| Bounded computation comparisons | `geecompute-benchmark` | [Computation](docs/computation.md) |
| Samples through inference, transfer and saved-product checks | `python -m geecomputation.workflow` | [Computation](docs/computation.md) |
| Agent instructions for both paths | `SKILL.md` | [Efficient GEE](SKILL.md) |

Computation builds lazy graphs and prepares products. Sampling owns pixel
transport and durable recovery. The merged package does not create a second
download queue or change scientific recipes. Authentication, remote requests and
exports still require the caller's applicable authorization and budget.

## Repository layout

```text
src/geesampler/          Sampling, metadata, transport and recovery
src/geecomputation/      Computation, products, contracts and accounting
tests/                  Sampling tests
tests/computation/      Computation tests and fixtures
examples/               Sampling examples
examples/computation/   Computation examples
docs/                   User guides
SKILL.md                Unified, explicitly invoked agent skill
references/             Detailed skill guidance
```

Run the offline suite with `python -m pytest`. Build the wheel with
`python -m build`. Offline tests exercise local contracts and mocked Earth Engine
graphs; they do not prove authenticated remote execution, current quotas or
scientific accuracy. Optional geospatial and monitoring packages are included in
the `all` extra used above.

## Local organization and compatibility

On the maintained cluster, the physical repository and skill directory is
`/nfs-data0/home/lyh/OutSideSkillLib/efficient-gee`.
`/nfs-data0/home/lyh/workspace/projects/HPGEE` is its project entry point.
The previous `GEESampler` and `GEEComputation` project entries have been
retired. Their original sources and Git history are archived under
`/nfs-data0/home/lyh/workspace/home-organization/backups`. Use the HPGEE
path for new work; frozen historical runs may retain the former path names.

The `geesampler-upstream` remote preserves the former repository URL; `origin`
targets `OptimalConvergence/HPGEE`. Configuring a remote does not create or publish
that repository. This integration is prepared locally for later publication.
Existing immutable research releases and data remain outside this repository.

The GEESampler Git history is retained, including a checkpoint of its local
development changes before integration. The MIT license and original copyright
notice are retained. Component `__version__` values identify the retained
components; the unified distribution version is recorded in `pyproject.toml`.
