"""Adapters for GEESampler; no downloader or queue implementation."""
from hpgee.workflows.config import Config
from hpgee.compute.features import HLS_COLLECTIONS, HLS_BANDS, SCENE_BANDS, hls_collection
import hashlib
import json
from pathlib import Path


def manifest(cfg):
    result={'recipe':'hls-common6-v2','project':cfg.project,'collections':dict(HLS_COLLECTIONS),
            'source_bands':{k:list(v) for k,v in HLS_BANDS.items()},'bands':list(SCENE_BANDS),
            'reflectance_scale':1.0,'reflectance_offset':0.0,'reflectance_units':'unitless EE HLS v002 reflectance',
            'time_units':'UTC milliseconds since Unix epoch','time_support':'scene acquisition; per-pixel selected scene',
            'interval_utc':{'start':cfg.start,'end':cfg.end,'closure':'[start,end)'},
            'pre_interval_utc':{'start':cfg.pre_start,'end':cfg.pre_end,'closure':'[start,end)'},
            'roi_bbox':list(cfg.bbox),'rf':dict(cfg.rf),'seed':cfg.seed,
            'selection_scope':{'hls_tile_ids':list(cfg.hls_tile_ids),
                'native_extent_dimensions':list(cfg.hls_native_dimensions),
                'kind':'explicit native extent/tile restrictions' if cfg.hls_tile_ids or cfg.hls_native_dimensions else 'raw filterBounds',
                'cannot_claim_enumeration_completeness':True,
                'note':'optional explicit planning restriction; no automatic zone exclusion; saved-pixel audit required'},
            'rf_definition':'post NBR2/MIRBI; delta=post-pre NBR2/MIRBI/NIR/SWIR1; min-NBR2 same-scene composites',
            'detection_inputs':['HLS_common6']+(['DEM'] if cfg.rf['feature_set']=='core6_dem' else []),
            'process_aux':['S1','GEDI','VIIRS_FRP','fuel','severity','recovery'],
            'sample_storage_dtype':'float64; QA integer values exact, UTC milliseconds exact',
            'labels':dict(cfg.labels),'s1':dict(cfg.s1),'dem':cfg.dem,
            'qa':'Fmask bits 1-4 zero; aerosol bits 6-7 != 3; bit 0 reserved; water retained',
            'grid':cfg.pixel_grid(),'dedup_key':['source_collection','source_index'],
            'source_index':'original system:index preserved before ImageCollection.merge prefixes it',
            'monthly_composite':'minimum NBR2; all bands/time from the selected scene; ties EE-defined',
            'budget_eecu_hours_declared':cfg.budget_eecu_hours,'budget_enforcement':'external monitoring; unknown is not zero',
            'pixel_validation':'required after GEESampler save; metadata/exit code do not constitute acceptance'}
    split=cfg.labels.get('frozen_split_manifest')
    if split: result['split_manifest_sha256']=hashlib.sha256(Path(split).read_bytes()).hexdigest()
    if cfg.labels.get('local_samples'):
        from hpgee.compute.rf import sample_source_info
        result['local_samples_sha256']=sample_source_info(cfg)['sha256']
    result['config_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return result


def hls_scene_collection(sample,*,config_path):
    import ee
    cfg = Config.load(config_path)
    return hls_collection(cfg,geometry=ee.Geometry(sample.geometry))


def frozen_grid(sample,grid_spec,*,config_path):
    """GEESampler grid_builder signature; one config applies to one frozen ROI grid."""
    return Config.load(config_path).pixel_grid()
