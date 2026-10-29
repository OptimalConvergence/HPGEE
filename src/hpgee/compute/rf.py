"""Explicit training only; binary class IDs fix MULTIPROBABILITY order."""
import math
import json
import hashlib
from dataclasses import dataclass,field
from pathlib import Path
from hpgee.compute.features import RF_BANDS, detection_bands


def _sample_source(cfg):
    asset=cfg.labels.get('asset'); local=cfg.labels.get('local_samples')
    if bool(asset)==bool(local): raise ValueError('exactly one of labels.asset or labels.local_samples is required')
    if asset:
        if not isinstance(asset,str) or not asset.strip(): raise ValueError('asset must be explicit string')
        return None,{'kind':'ee_asset','asset':asset}
    path=Path(local); cap=cfg.labels.get('max_samples',5000)
    if type(cap) is not int or not 2<=cap<=10000: raise ValueError('local sample cap must be integer in [2,10000]')
    if path.stat().st_size>10*1024*1024: raise ValueError('local sample table exceeds 10 MiB bound')
    payload=path.read_bytes(); parsed=json.loads(payload)
    features=parsed.get('features') if isinstance(parsed,dict) and parsed.get('type')=='FeatureCollection' else None
    if not isinstance(features,list) or not features or len(features)>cap: raise ValueError('local FeatureCollection missing/empty or exceeds sample cap')
    if any(not isinstance(f,dict) or f.get('type')!='Feature' or not isinstance(f.get('properties'),dict) for f in features):
        raise ValueError('local samples require GeoJSON Feature properties')
    return [f['properties'] for f in features],{'kind':'local_feature_collection','path':str(path.resolve()),
        'sha256':hashlib.sha256(payload).hexdigest(),'sample_count':len(features)}


def sample_source_info(cfg):
    """Bounded local validation/provenance only; no EE or authentication."""
    return _sample_source(cfg)[1]


def input_samples(cfg):
    """Return (ee.FeatureCollection, provenance); local geometry is not uploaded."""
    import ee
    rows,source=_sample_source(cfg)
    samples=ee.FeatureCollection(source['asset']) if rows is None else ee.FeatureCollection([ee.Feature(None,row) for row in rows])
    return samples,source


def load_split_manifest(path):
    return _parse_split_manifest(Path(path).read_bytes())


def _parse_split_manifest(payload):
    frozen=json.loads(payload)
    for key in ('events','supports'):
        groups=frozen.get(key)
        if not isinstance(groups,dict) or not groups or any(not isinstance(k,str) or not k or v not in ('train','validation','test') for k,v in groups.items()):
            raise ValueError('split manifest requires nonempty events/supports mappings to train/validation/test')
    return frozen,hashlib.sha256(payload).hexdigest()


def validate_samples(rows,*,split_manifest=None,feature_bands=RF_BANDS):
    groups = {'event_id':{},'support_id':{}}
    classes = {}
    for row in rows:
        split = row.get('split')
        if split not in ('train','validation','test'): raise ValueError('frozen split must be train/validation/test')
        if type(row.get('label')) is not int or row['label'] not in (0,1): raise ValueError('observed binary class labels 0/1 required')
        if row.get('label_eligible') is not True: raise ValueError('label eligibility must be explicitly true')
        source = row.get('label_source')
        if source not in ('manual','reference','agency','weak','media_verified'): raise ValueError('explicit label_source required')
        if source == 'weak' and split != 'train': raise ValueError('weak supervision is train-only')
        for name,seen in groups.items():
            key = row.get(name)
            if not isinstance(key,str) or not key: raise ValueError(f'explicit {name} required')
            if split_manifest is not None:
                table=split_manifest['events' if name=='event_id' else 'supports']
                if table.get(key)!=split: raise ValueError(f'{name}/split differs from frozen manifest or unknown group')
            if key in seen and seen[key] != split: raise ValueError(f'{name} leakage across frozen splits')
            seen[key] = split
        for name in feature_bands:
            value = row.get(name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise ValueError(f'finite observed feature required: {name}')
        classes.setdefault(split,set()).add(row['label'])
    if classes.get('train') != {0,1}: raise ValueError('train lacks required class support [0,1]')
    for split,values in classes.items():
        if values != {0,1}: raise ValueError(f'{split} lacks required class support [0,1]')
    return [0,1]


@dataclass(frozen=True)
class TrainingSnapshot:
    """Validated local cohort; immutable JSON keeps caller-owned rows out of graphs.

    Construction validates the rows once. Split bytes and their original path are
    provenance captured at construction, never a live dependency of later models.
    """
    rows_json: tuple[str,...]
    feature_bands: tuple[str,...]
    split_manifest_payload: bytes
    frozen_split_manifest: str
    sample_snapshot_sha256: str = field(init=False)
    frozen_split_manifest_sha256: str = field(init=False)

    def __post_init__(self):
        if (type(self.rows_json) is not tuple or any(type(row) is not str for row in self.rows_json)
                or type(self.feature_bands) is not tuple or not self.feature_bands
                or any(type(name) is not str or not name for name in self.feature_bands)
                or type(self.split_manifest_payload) is not bytes or type(self.frozen_split_manifest) is not str):
            raise TypeError('TrainingSnapshot requires immutable JSON rows, feature tuple and split bytes/path')
        frozen,manifest_sha=_parse_split_manifest(self.split_manifest_payload)
        rows=[json.loads(row) for row in self.rows_json]
        validate_samples(rows,split_manifest=frozen,feature_bands=self.feature_bands)
        canonical=tuple(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False) for row in rows)
        snapshot_sha=hashlib.sha256(json.dumps(sorted(canonical),separators=(',',':')).encode()).hexdigest()
        object.__setattr__(self,'rows_json',canonical)
        object.__setattr__(self,'sample_snapshot_sha256',snapshot_sha)
        object.__setattr__(self,'frozen_split_manifest_sha256',manifest_sha)


def freeze_training_snapshot(samples,cfg,*,snapshot_bands=None):
    """Read a bounded table and validate once; local inputs need no EE request."""
    if not cfg.labels.get('frozen_split_manifest'):
        raise ValueError('labels.frozen_split_manifest is required before training')
    split_payload=Path(cfg.labels['frozen_split_manifest']).read_bytes()
    _parse_split_manifest(split_payload)
    bands=detection_bands(cfg)
    frozen_bands=tuple(snapshot_bands) if snapshot_bands is not None else bands
    if not set(bands).issubset(frozen_bands): raise ValueError('snapshot lacks requested detection features')
    cap = cfg.labels.get('max_samples',5000)
    if type(cap) is not int or not 2 <= cap <= 10000: raise ValueError('max_samples must be integer in [2,10000]')
    properties = ['label','split','event_id','support_id','label_source','label_eligible',*frozen_bands]
    if cfg.labels.get('local_samples'):
        local_rows,_ = _sample_source(cfg)
        rows = [{name:row[name] for name in properties if name in row} for row in local_rows]
    else:
        import ee
        # cap+1 detects overflow instead of silently validating only a prefix.
        # FC.getInfo aborts above 5000 elements; materialize only the bounded list.
        records = ee.FeatureCollection(samples).select(properties).toList(cap+1).getInfo()
        if len(records)>cap: raise ValueError('sample cap exceeded; subset explicitly before training')
        rows=[f['properties'] for f in records]
    snapshot=TrainingSnapshot(tuple(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False) for row in rows),
                              frozen_bands,split_payload,str(cfg.labels['frozen_split_manifest']))
    expected=cfg.labels.get('expected_sample_sha256')
    if expected and snapshot.sample_snapshot_sha256!=expected: raise ValueError('sample snapshot differs from expected hash')
    return snapshot


def build_rf_model(snapshot,cfg):
    """Return (lazy model, metadata) without reading samples or building features.

    Reusing this graph does not persist a trained server model. Separate EE
    evaluation requests may train again; account for their actual compute cost.
    """
    import ee
    if type(snapshot) is not TrainingSnapshot: raise TypeError('validated TrainingSnapshot required')
    bands=detection_bands(cfg)
    if not set(bands).issubset(snapshot.feature_bands): raise ValueError('snapshot lacks requested detection features')
    expected=cfg.labels.get('expected_sample_sha256')
    if expected and snapshot.sample_snapshot_sha256!=expected: raise ValueError('sample snapshot differs from expected hash')
    rows=[json.loads(row) for row in snapshot.rows_json]
    # The validated, bounded rows are the training data. Never reread a mutable
    # remote asset when the lazy classifier is eventually evaluated.
    training = ee.FeatureCollection([ee.Feature(None,row) for row in rows if row['split']=='train'])
    model = ee.Classifier.smileRandomForest(numberOfTrees=cfg.rf['trees'],seed=cfg.seed).setOutputMode('MULTIPROBABILITY').train(
        features=training,classProperty='label',inputProperties=list(bands))
    return model,{'class_order':[0,1],'class_names':['unburned','burned'],
                         'score_calibrated':False,'trees':cfg.rf['trees'],'feature_set':cfg.rf['feature_set'],'seed':cfg.seed,
                         'frozen_split_manifest':snapshot.frozen_split_manifest,
                         'frozen_split_manifest_sha256':snapshot.frozen_split_manifest_sha256,
                         'sample_snapshot_sha256':snapshot.sample_snapshot_sha256,
                         'training_input':'validated literal rows; geometry omitted; no remote asset dependency',
                         'sample_count':len(rows),'features':list(bands)}


def predict_rf(model,cfg=None,*,features=None):
    """Build binary score bands from exactly one Config or explicit feature image.

    The caller binds an explicit image to the model's feature contract. This is
    lazy graph construction only; no model export or server evaluation occurs.
    """
    if (cfg is None)==(features is None): raise ValueError('exactly one of cfg or features is required')
    if features is None:
        from hpgee.compute.features import feature_image
        features=feature_image(cfg)
    return features.classify(model).arrayFlatten([['score_unburned','score_burned']])


def train_rf(samples,cfg,*,snapshot_bands=None):
    """Compatibility wrapper returning lazy model, scores and original metadata."""
    snapshot=freeze_training_snapshot(samples,cfg,snapshot_bands=snapshot_bands)
    model,metadata=build_rf_model(snapshot,cfg)
    return model,predict_rf(model,cfg),metadata
