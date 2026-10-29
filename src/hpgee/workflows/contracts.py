"""Frozen product identity shared by workflow construction and pixel transfer."""
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from hpgee.compute.features import RF_BANDS, DEM_BANDS, DEM_COLLECTION
from hpgee.workflows.recipe import manifest


QUALITY_BANDS = tuple(f'{period}_{band}' for period in ('pre','post')
                      for band in ('valid_count','time_ms','Fmask','clear'))
PRODUCT_BANDS = ('score_unburned','score_burned','candidate',*RF_BANDS,*QUALITY_BANDS,
                 *(f'aux_dem_{band}' for band in DEM_BANDS))


def _threshold(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('candidate threshold must be finite in [0,1]')
    return float(value)


def workflow_contract(cfg,threshold=.5,*,sample_points_path=None,source_manifest_sha256=None,aux_context_sha256=None):
    if cfg.rf['feature_set']!='core6':
        raise ValueError('workflow requires core6; auxiliary variables do not enter RF')
    result={'recipe':manifest(cfg),'grid':cfg.pixel_grid(),'threshold':_threshold(threshold),
            'rf_inputs':list(RF_BANDS),'raster_bands':list(PRODUCT_BANDS),'dtype':'float64',
            'candidate_meaning':'uncalibrated RF score >= threshold; demonstration, not accepted burned area',
            'unknown':'scores, candidate and core6 remain masked where pre/post features unavailable',
            'quality_time':'selected pre/post scene acquisition UTC ms; not burning-time interval',
            'aux':{'DEM':{'collection':DEM_COLLECTION,'bands':list(DEM_BANDS),'enters_rf':False,
                         'native_support':'GLO30 DSM; terrain derived on declared 30 m UTM grid',
                         'missing_policy':'independent mask; cannot remove valid core pixels'},
                   'VIIRS':{'included':False,'enters_rf':False},'GEDI':{'included':False,'enters_rf':False}},
            'boundary_grid':'same CRS/affine/width/height as raster; eight-connected candidate cells',
            'eecu_seconds':None,'telemetry_complete':False}
    if source_manifest_sha256 is not None:
        if not isinstance(source_manifest_sha256,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',source_manifest_sha256):
            raise ValueError('source_manifest_sha256 must be a 64-hex SHA256')
        result.update(source_selection='frozen_original_scene_list',source_manifest_sha256=source_manifest_sha256)
    if aux_context_sha256 is not None:
        if not isinstance(aux_context_sha256,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',aux_context_sha256):
            raise ValueError('aux_context_sha256 must be a 64-hex SHA256')
        result['aux']['DEM']['collection']=None
        result['aux']['DEM']['native_support']='caller-declared in frozen auxiliary context'
        result['aux']['DEM']['selection']='caller_frozen_auxiliary_context'
        result['aux_context_sha256']=aux_context_sha256
    if sample_points_path:
        result['sample_supports_sha256']=hashlib.sha256(Path(sample_points_path).read_bytes()).hexdigest()
    result['contract_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return result


def product_collection(sample,*,products):
    import ee
    return ee.ImageCollection.fromImages([products['raster']])
