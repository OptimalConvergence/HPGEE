"""High Performance Earth Engine: shared contracts and execution capabilities.

Public objects load on demand; importing HPGEE does not authenticate or start work.
"""
from importlib import import_module

__version__ = "0.2.0"

_EXPORTS = {'CatalogStats': ('hpgee.catalog.catalog', 'CatalogStats'),
 'S2SceneCatalog': ('hpgee.catalog.catalog', 'S2SceneCatalog'),
 'SceneRecord': ('hpgee.catalog.catalog', 'SceneRecord'),
 'AccountProfile': ('hpgee.sampling.config', 'AccountProfile'),
 'CatalogSettings': ('hpgee.sampling.config', 'CatalogSettings'),
 'DistributedRunConfig': ('hpgee.sampling.config', 'DistributedRunConfig'),
 'DistributedSampler': ('hpgee.sampling.distributed', 'DistributedSampler'),
 'AuthConfig': ('hpgee.core.models', 'AuthConfig'),
 'EECUMonitorConfig': ('hpgee.core.models', 'EECUMonitorConfig'),
 'PatchGrid': ('hpgee.core.models', 'PatchGrid'),
 'RunConfig': ('hpgee.core.models', 'RunConfig'),
 'RunSummary': ('hpgee.core.models', 'RunSummary'),
 'SampleRecord': ('hpgee.core.models', 'SampleRecord'),
 'SceneSelection': ('hpgee.core.models', 'SceneSelection'),
 'CatalogResolverConfig': ('hpgee.catalog.resolver', 'CatalogResolverConfig'),
 'ResolverStats': ('hpgee.catalog.resolver', 'ResolverStats'),
 'S2CatalogResolver': ('hpgee.catalog.resolver', 'S2CatalogResolver'),
 'Sampler': ('hpgee.sampling.sampler', 'Sampler'),
 'EESampleSource': ('hpgee.sampling.sources', 'EESampleSource'),
 'FileSampleSource': ('hpgee.sampling.sources', 'FileSampleSource'),
 'WorkflowConfig': ('hpgee.workflows.config', 'Config'),
 'PixelGrid': ('hpgee.core.grid', 'PixelGrid'),
 'SamplerConfig': ('hpgee.sampling.config', 'SamplerConfig'),
 'build_workflow': ('hpgee.workflows.detection', 'build_workflow')}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
