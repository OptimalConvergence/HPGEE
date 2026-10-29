"""Known derived products through GEESampler; no metadata RPC or auth on import."""
from copy import deepcopy
from functools import partial
from .config import utc_datetime
from .workflow import PRODUCT_BANDS, product_collection, workflow_contract


class _ProductResolver:
    inline_quality = probe_quality = cloud_masking = False
    min_clear_fraction = 0.0

    def __init__(self, sample, grid):
        from geesampler.catalog import SceneRecord
        self.sample,self.grid = sample,grid
        self.scene = SceneRecord(collection='derived_workflow_product',asset_id='workflow_product',
            scene_id='workflow_product',acquired_millis=int(sample.date.timestamp()*1000),mgrs_tile='',
            properties={'time_role':'derived_product_reference_time'})

    def prepare(self, samples, *, grid, selection, workload_tag):
        if (selection.mode,selection.start_offset_days,selection.end_offset_days,selection.max_scenes)!=('all',0,0,1):
            raise ValueError('Known product requires selection all/0/0/1')
        if list(samples)!=[self.sample] or grid!=self.grid:
            raise ValueError('Known product requires its single frozen sample/grid')

    def order_samples(self, samples):
        return list(samples)

    def candidates(self, sample):
        if sample!=self.sample: raise ValueError('Unknown product sample')
        return [self.scene]

    def cached_quality(self, sample, scene):
        return None

    def should_fallback_without_metadata(self, sample):
        return False

    def stats(self):
        from geesampler.resolver import ResolverStats
        return ResolverStats()


def product_sampler_arguments(cfg, products):
    """Return DownloadEngine arguments for one already-defined 21-band product.

    ``cfg.start`` is a derived product reference clock, neither source acquisition
    nor burning time. Original pre/post observation bands and unknown masks stay
    untouched. The caller owns run identity, transport, retry policy and QA.
    """
    from geesampler.models import SampleRecord,PatchGrid,SceneSelection
    try:
        contract=products['contract']
        expected=workflow_contract(cfg,contract['threshold'],source_manifest_sha256=contract.get('source_manifest_sha256'),
                                   aux_context_sha256=contract.get('aux_context_sha256'))
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('Invalid fixed product contract') from exc
    if contract!=expected or products.get('bands')!=list(PRODUCT_BANDS) or 'raster' not in products:
        raise ValueError('Fixed product contract/bands differ from workflow')
    sample=SampleRecord('product',{'type':'Point','coordinates':[(cfg.bbox[0]+cfg.bbox[2])/2,(cfg.bbox[1]+cfg.bbox[3])/2]},
        date=utc_datetime(cfg.start),properties={'time_role':'derived_product_reference_time'})
    image=products['raster'].set('system:index','workflow_product').set('system:time_start',int(sample.date.timestamp()*1000))
    grid=PatchGrid(size=cfg.grid['width'],scale=30);pixel_grid=cfg.pixel_grid()
    return dict(records=[sample],collection_builder=partial(product_collection,products={'raster':image}),
        bands=list(PRODUCT_BANDS),grid=grid,grid_builder=lambda sample,spec:deepcopy(pixel_grid),
        selection=SceneSelection(mode='all',start_offset_days=0,end_offset_days=0,max_scenes=1),
        scene_resolver=_ProductResolver(sample,grid))
