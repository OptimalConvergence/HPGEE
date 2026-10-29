"""Explicit bounded RF probes and conditional area-month cost extrapolation.

Imports/specification/estimation do not authenticate, submit tasks or train.
"""
import argparse
import csv
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import hashlib
import json
import math
from pathlib import Path
import random
import time
import uuid
from hpgee.workflows.config import Config
from hpgee.compute.features import detection_bands, RF_BANDS, DEM_BANDS
from hpgee.workflows.recipe import manifest


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _grid_bounds(cfg):
    _,_,left,_,_,top=cfg.grid['transform']
    return [left,top-30*cfg.grid['height'],left+30*cfg.grid['width'],top]


def benchmark_specs(cfg):
    if not cfg.labels.get('frozen_split_manifest'):
        raise ValueError('benchmark requires frozen_split_manifest')
    from hpgee.compute.rf import load_split_manifest, sample_source_info
    input_source=sample_source_info(cfg)
    _,split_sha=load_split_manifest(cfg.labels['frozen_split_manifest'])
    base=manifest(replace(cfg,rf={'trees':100,'feature_set':'core6'}))
    specs=[]
    for feature_set in ('core6','core6_dem'):
        for trees in (50,100,200):
            variant=replace(cfg,rf={'trees':trees,'feature_set':feature_set})
            item={'trees':trees,'feature_set':feature_set,'features':list(detection_bands(variant)),
                  'comparison_contract_sha256':base['config_sha256'],
                  'input_asset':cfg.labels.get('asset'),'input_source':input_source,'split_manifest_sha256':split_sha,
                  'sample_snapshot':'freeze first read; verify identical for every variant',
                  'query_bbox_wgs84':base['roi_bbox'],
                  'inference_roi':{'crs':cfg.grid['crs'],'bounds':_grid_bounds(cfg)},'grid':base['grid'],
                  'post':base['interval_utc'],'pre':base['pre_interval_utc'],'seed':cfg.seed,
                  'detection_inputs':['HLS_common6']+(['DEM'] if feature_set=='core6_dem' else []),
                  'process_aux':base['process_aux'],'status':'defined_not_executed',
                  'output_measurement':'synchronous fixed-grid mean/count score reduction; not full-payload transfer'}
            item['run_spec_sha256']=_sha(item); specs.append(item)
    return specs


def execute_benchmark(cfg):
    """Only explicit CLI execute calls this; sequential probes, never batch exports."""
    specs=benchmark_specs(cfg)
    from hpgee.cli.computation import initialize
    from hpgee.accounting.usage import read_usage
    from hpgee.compute.rf import train_rf, input_samples
    ee=initialize(cfg)
    samples,input_source=input_samples(cfg)
    if input_source!=specs[0]['input_source']: raise ValueError('sample source changed between specification and execution')
    credentials=ee.ServiceAccountCredentials(cfg.auth['service_account'],str(cfg.auth['key_file'])) if cfg.auth.get('service_account') else None
    order=list(specs); random.Random(cfg.seed).shuffle(order)
    run_id=str(uuid.uuid4()); output=cfg.output/'benchmarks'/run_id; output.mkdir(parents=True)
    (output/'specs.json').write_text(json.dumps(specs,indent=2)+'\n')
    records=[]; sample_snapshot=None
    footprint=ee.Geometry.Rectangle(_grid_bounds(cfg),proj=cfg.grid['crs'],geodesic=False)
    for index,spec in enumerate(order):
        variant=replace(cfg,rf={'trees':spec['trees'],'feature_set':spec['feature_set']})
        tag='gc-bench-'+run_id.replace('-','')[:18]+'-'+str(index)
        start=datetime.now(timezone.utc).isoformat(); tick=time.perf_counter()
        record={'run_id':run_id,'project':cfg.project,'sequence':index,'variant':f"t{spec['trees']}-{spec['feature_set']}",
                'spec':spec,'remote_workload_tag':tag,'remote_operation':None,
                'remote_identity_limitation':'synchronous requests do not expose a batch operation ID',
                'training_reuse':'not persistent; inference graph may reevaluate training; server cache not assumed',
                'synthetic':False,'queue_seconds':None,'payload_bytes':None,'status':'started','timings':{}}
        old_tag=ee.data.getWorkloadTag(); ee.data.setWorkloadTag(tag)
        try:
            stage_tick=time.perf_counter()
            model,scores,metadata=train_rf(samples,variant,snapshot_bands=RF_BANDS+DEM_BANDS)
            record['timings']['sample_read_and_graph_seconds']=time.perf_counter()-stage_tick
            # train_rf snapshots all returned properties. core6/DEM selection must not change rows.
            current=metadata['sample_snapshot_sha256']
            if sample_snapshot is None: sample_snapshot=current
            if current!=sample_snapshot: raise ValueError('fixed sample snapshot changed between benchmark variants')
            record['sample_snapshot_sha256']=current
            stage_tick=time.perf_counter()
            record['training']=model.explain().select(['importance','numberOfTrees']).getInfo()
            record['timings']['training_explanation_seconds']=time.perf_counter()-stage_tick
            reducer=ee.Reducer.mean().combine(ee.Reducer.count(),sharedInputs=True)
            stage_tick=time.perf_counter()
            record['inference']=scores.reduceRegion(reducer=reducer,geometry=footprint,crs=cfg.grid['crs'],
                crsTransform=cfg.grid['transform'],maxPixels=cfg.grid['width']*cfg.grid['height']*2).getInfo()
            record['timings']['inference_seconds']=time.perf_counter()-stage_tick
            counts=[v for k,v in record['inference'].items() if k.endswith('_count')]
            record['status']='evaluated' if counts and all(v and v>0 for v in counts) else 'no_valid_inference_pixels'
        except Exception as exc:
            record['status']='failed'; record['error_type']=type(exc).__name__
        finally:
            ee.data.setWorkloadTag(old_tag)
        end=datetime.now(timezone.utc).isoformat()
        record.update(start=start,end=end,compute_wall_seconds=time.perf_counter()-tick)
        record['usage']=read_usage(cfg.project,start=start,end=end,workload_tag=tag,credentials=credentials)
        record['eecu_status']='PARTIAL_REPORTED' if record['usage']['status']=='REPORTED' else 'UNKNOWN'
        record['reported_eecu_seconds']=record['usage']['guard_hours']*3600 if record['usage']['guard_hours'] is not None else None
        record['telemetry_complete']=False
        record['eecu_seconds']=None
        records.append(record)
        (output/'records.json').write_text(json.dumps(records,indent=2,allow_nan=False)+'\n')
        if record['status']=='failed': break
    return {'status':'failed' if any(r['status']=='failed' for r in records) else 'executed','run_id':run_id,'output':str(output),'records':records,
            'limitation':'delayed EECU may be UNKNOWN; no payload throughput measured; fixed-grid synchronous probe only'}


def refresh_telemetry(cfg,path,*,delay_seconds=300,now=None):
    """Read delayed telemetry only; no EE initialization, training or inference.

    Elapsed delay and reported points never establish complete accounting.
    Complete totals require separate manual/independent reconciliation evidence.
    """
    from hpgee.accounting.usage import read_usage
    from hpgee.workflows.config import utc_datetime
    if type(delay_seconds) is not int or not 1<=delay_seconds<=86400: raise ValueError('delay_seconds must be integer in [1,86400]')
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('refresh clock must have explicit UTC offset')
    now=now.astimezone(timezone.utc)
    path=Path(path); records=json.loads(path.read_text())
    if not isinstance(records,list): raise ValueError('records JSON must be a list')
    credentials=None
    if cfg.auth.get('service_account'):
        import ee
        from hpgee.core.models import AuthConfig
        from hpgee.runtime.auth import validate_auth_config
        auth=AuthConfig(project=cfg.project,**cfg.auth); validate_auth_config(auth)
        credentials=ee.ServiceAccountCredentials(auth.service_account,str(auth.key_file))
    for record in records:
        if record.get('project')!=cfg.project or not record.get('remote_workload_tag'):
            raise ValueError('telemetry refresh requires original matching project and workload tag')
        start=utc_datetime(record['start']); end=utc_datetime(record['end'])
        if start>=end: raise ValueError('invalid original benchmark interval')
        window_end=end+timedelta(seconds=delay_seconds)
        record['telemetry_window']={'start':record['start'],'original_end':record['end'],
            'end':window_end.isoformat().replace('+00:00','Z'),'delay_seconds':delay_seconds}
        if record.get('telemetry_complete') is True:
            record['telemetry_refresh_status']='manually completed record unchanged'; continue
        record['telemetry_complete']=False; record['eecu_seconds']=None
        if now<window_end:
            record['telemetry_refresh_status']='delay_window_not_elapsed'; continue
        usage=read_usage(cfg.project,start=record['start'],end=record['telemetry_window']['end'],
                         workload_tag=record['remote_workload_tag'],credentials=credentials)
        reported=usage['guard_hours']*3600 if usage['guard_hours'] is not None else None
        record.setdefault('telemetry_observations',[]).append({'checked_at':now.isoformat(),
            'window':dict(record['telemetry_window']),'usage':usage,'reported_eecu_seconds':reported})
        record.update(usage=usage,reported_eecu_seconds=reported,
            eecu_status='PARTIAL_REPORTED' if usage['status']=='REPORTED' else 'UNKNOWN',telemetry_refresh_status='refreshed_partial')
    path.write_text(json.dumps(records,indent=2,allow_nan=False)+'\n')
    return {'status':'telemetry_refreshed','records':records,'computation_rerun':False,
            'completion_rule':'manual or independent billing reconciliation required; no automatic completeness claim'}


NUMERIC=('area_month_km2','compute_wall_seconds','eecu_seconds','payload_bytes','queue_seconds')


def read_metrics(path):
    path=Path(path)
    if path.suffix.lower()=='.csv':
        with path.open(newline='') as stream: rows=list(csv.DictReader(stream))
        for row in rows:
            for key in NUMERIC: row[key]=float(row[key]) if row.get(key) not in (None,'') else None
            flag=row.get('synthetic','').lower()
            if flag not in ('true','false'): raise ValueError('CSV synthetic must explicitly be true/false')
            row['synthetic']=flag=='true'
            flag=row.get('telemetry_complete','').lower()
            if flag not in ('true','false',''): raise ValueError('CSV telemetry_complete must be true/false')
            row['telemetry_complete']=flag=='true'
            if row.get('telemetry_confirmation'):
                row['telemetry_confirmation']=json.loads(row['telemetry_confirmation'])
            elif row.get('telemetry_confirmation_method') or row.get('telemetry_confirmation_evidence_reference'):
                row['telemetry_confirmation']={'method':row.get('telemetry_confirmation_method'),
                    'evidence_reference':row.get('telemetry_confirmation_evidence_reference')}
        return rows
    raw=json.loads(path.read_text()); return raw['metrics'] if isinstance(raw,dict) else raw


def estimate_year(metrics,coverage,*,allow_synthetic=False,bandwidth_bytes_per_second=None):
    """sum(area_month_km2 * density); observed extrema are not confidence intervals."""
    def unavailable(reason): return {'status':'cannot_estimate','reason':reason,'production_wall_seconds':None}
    if not metrics: return unavailable('no empirical metrics')
    if not coverage or not any(s.get('activity')=='low_activity_non_event' for s in coverage):
        return unavailable('annual coverage must include low-activity non-event strata')
    if any(type(m.get('synthetic')) is not bool for m in metrics): raise ValueError('synthetic must be explicit boolean')
    synthetic=any(m['synthetic'] for m in metrics)
    if synthetic and not allow_synthetic: return unavailable('synthetic fixtures cannot produce real estimates')
    if synthetic and not all(m['synthetic'] for m in metrics): raise ValueError('do not mix synthetic and measured metrics')
    if not synthetic:
        if any(not m.get('run_id') or not m.get('variant') or not m.get('pipeline_id') for m in metrics):
            return unavailable('measured metrics require run_id, variant and frozen pipeline_id')
        if len({(m['variant'],m['pipeline_id']) for m in metrics})!=1: raise ValueError('metrics mix RF/pipeline variants')
        for metric in metrics:
            proof=metric.get('telemetry_confirmation',{})
            if not isinstance(proof,dict) or proof.get('method') not in ('manual_reconciliation','independent_billing_reconciliation') or not proof.get('evidence_reference'):
                return unavailable('complete EECU requires manual or independent reconciliation evidence')
    if any(m.get('telemetry_complete') is not True for m in metrics):
        return unavailable('EECU telemetry incomplete; reported points are partial, not a total')
    if any(m.get('eecu_seconds') is None for m in metrics): return unavailable('EECU telemetry unknown; not zero')
    if any(m.get(k) is None for m in metrics for k in ('compute_wall_seconds','payload_bytes')):
        return unavailable('missing measured compute wall or payload cost')
    for row in metrics:
        for key in NUMERIC:
            value=row.get(key)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):
                raise ValueError('metric costs must be finite and nonnegative')
        if not row.get('stratum') or not row.get('area_month_km2') or not row.get('run_id'): raise ValueError('positive sampled area and run identity required')
    if len({(m['run_id'],m['stratum']) for m in metrics})!=len(metrics): raise ValueError('duplicate measured run/stratum')
    if len({s['stratum'] for s in coverage})!=len(coverage): raise ValueError('duplicate target stratum')
    grouped={s['stratum']:[m for m in metrics if m['stratum']==s['stratum']] for s in coverage}
    if any(not group for group in grouped.values()): return unavailable('one or more target strata have no measurements')
    totals={k:{'point':0,'low':0,'high':0} for k in ('compute_wall_seconds','eecu_seconds','payload_bytes')}
    details=[]
    for target in coverage:
        area=target['area_month_km2']
        if isinstance(area,bool) or not isinstance(area,(int,float)) or not math.isfinite(area) or area<=0:
            raise ValueError('target area_month_km2 must be finite and positive')
        rows=grouped[target['stratum']]; sampled=sum(r['area_month_km2'] for r in rows)
        density={}
        for key in totals:
            ratios=[r[key]/r['area_month_km2'] for r in rows]
            density[key]={'point':sum(r[key] for r in rows)/sampled,'low':min(ratios),'high':max(ratios)}
            for bound in ('point','low','high'): totals[key][bound]+=area*density[key][bound]
        details.append({'stratum':target['stratum'],'sample_count':len(rows),'sampled_area_month_km2':sampled,'target_area_month_km2':area,'cost_density':density})
    transfer=None
    if bandwidth_bytes_per_second is not None:
        if not math.isfinite(bandwidth_bytes_per_second) or bandwidth_bytes_per_second<=0: raise ValueError('positive bandwidth required')
        transfer={k:v/bandwidth_bytes_per_second for k,v in totals['payload_bytes'].items()}
    return {'status':'synthetic_only' if synthetic else 'conditional_estimate','synthetic':synthetic,
            'formula':'sum(target area_month_km2 * measured stratum cost density)',
            'serial_compute_wall_seconds':totals['compute_wall_seconds'],'eecu_seconds':totals['eecu_seconds'],
            'eecu_hours':{k:v/3600 for k,v in totals['eecu_seconds'].items()},'payload_bytes':totals['payload_bytes'],
            'transfer_seconds_at_declared_bandwidth':transfer,'bandwidth_bytes_per_second':bandwidth_bytes_per_second,
            'production_wall_seconds':None,'queue_seconds_observed_total':sum(m['queue_seconds'] for m in metrics if m.get('queue_seconds') is not None) if any(m.get('queue_seconds') is not None for m in metrics) else None,
            'queue_unknown_samples':sum(m.get('queue_seconds') is None for m in metrics),'strata':details,
            'uncertainty':{'confidence_interval':False,'range_kind':'observed density extrema; no extrapolation-confidence bound',
                           'representativeness':'must verify season/cloud/biome/activity support; one sample provides no uncertainty estimate'},
            'assumptions':['annual area-month coverage supplied externally; avoid overlap/double counting',
                           'serial request wall sum is not global production wall; concurrency/queue require measured schedule',
                           'EECU is compute usage, never interchangeable with wall hours']}


def main(argv=None):
    parser=argparse.ArgumentParser(prog='geecompute-benchmark'); commands=parser.add_subparsers(dest='command',required=True)
    for command in ('specs','execute','refresh-telemetry'):
        child=commands.add_parser(command); child.add_argument('--config',required=True)
        if command=='refresh-telemetry':
            child.add_argument('--records',required=True); child.add_argument('--delay-seconds',type=int,default=300)
    child=commands.add_parser('estimate'); child.add_argument('--metrics',required=True); child.add_argument('--coverage',required=True)
    child.add_argument('--year',type=int,required=True); child.add_argument('--bandwidth-bytes-per-second',type=float)
    child.add_argument('--synthetic-test',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='estimate':
        coverage=json.loads(Path(args.coverage).read_text())
        if not 2018<=args.year<=2025 or coverage.get('year')!=args.year or coverage.get('months')!=12:
            raise ValueError('coverage must freeze requested year 2018–2025 and all 12 months')
        if type(coverage.get('synthetic')) is not bool: raise ValueError('coverage.synthetic must be explicit boolean')
        metrics=read_metrics(args.metrics)
        if coverage['synthetic'] and any(m.get('synthetic') is not True for m in metrics):
            raise ValueError('synthetic coverage cannot combine with measured metrics')
        result=estimate_year(metrics,coverage['strata'],allow_synthetic=args.synthetic_test,
                             bandwidth_bytes_per_second=args.bandwidth_bytes_per_second); result['year']=args.year
    else:
        cfg=Config.load(args.config)
        if args.command=='specs': result=benchmark_specs(cfg)
        elif args.command=='refresh-telemetry': result=refresh_telemetry(cfg,args.records,delay_seconds=args.delay_seconds)
        else: result=execute_benchmark(cfg)
    print(json.dumps(result,indent=2,allow_nan=False))
    return 2 if isinstance(result,dict) and result.get('status') in ('cannot_estimate','failed') else 0


if __name__=='__main__': raise SystemExit(main())
