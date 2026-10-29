"""Known derived products through HPGEE; no metadata RPC or auth on import."""
from functools import partial
from hpgee.catalog.catalog import SceneRecord
from hpgee.transfer.requests import FrozenSceneRequest
from hpgee.workflows.config import utc_datetime
from hpgee.workflows.contracts import PRODUCT_BANDS, product_collection, workflow_contract


def product_sampler_arguments(cfg, products, *, sample_id='product'):
    """Return DownloadEngine arguments for one already-defined 21-band product.

    ``cfg.start`` is a derived product reference clock, neither source acquisition
    nor burning time. Original pre/post observation bands and unknown masks stay
    untouched. The caller owns run identity, transport, retry policy and QA.
    """
    from hpgee.core.models import SampleRecord, PatchGrid
    try:
        contract=products['contract']
        expected=workflow_contract(cfg,contract['threshold'],source_manifest_sha256=contract.get('source_manifest_sha256'),
                                   aux_context_sha256=contract.get('aux_context_sha256'))
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('Invalid fixed product contract') from exc
    if contract!=expected or products.get('bands')!=list(PRODUCT_BANDS) or 'raster' not in products:
        raise ValueError('Fixed product contract/bands differ from workflow')
    sample=SampleRecord(sample_id,{'type':'Point','coordinates':[(cfg.bbox[0]+cfg.bbox[2])/2,(cfg.bbox[1]+cfg.bbox[3])/2]},
        date=utc_datetime(cfg.start),properties={'time_role':'derived_product_reference_time'})
    image=products['raster'].set('system:index','workflow_product').set('system:time_start',int(sample.date.timestamp()*1000))
    grid=PatchGrid(size=cfg.grid['width'],scale=30);pixel_grid=cfg.pixel_grid()
    scene=SceneRecord(collection='derived_workflow_product',asset_id='workflow_product',
        scene_id='workflow_product',acquired_millis=int(sample.date.timestamp()*1000),mgrs_tile='',
        properties={'time_role':'derived_product_reference_time'})
    return FrozenSceneRequest(sample,scene,partial(product_collection,products={'raster':image}),
        PRODUCT_BANDS,grid,pixel_grid).sampler_arguments()
