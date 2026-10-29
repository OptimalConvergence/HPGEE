"""Frozen small-ROI configuration; no authentication or requests on import."""
from dataclasses import dataclass,field
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path


def utc_datetime(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc))


@dataclass(frozen=True)
class Config:
    project: str
    bbox: tuple
    start: str
    end: str
    pre_start: str
    pre_end: str
    grid: dict
    output: Path
    budget_eecu_hours: float
    seed: int
    auth: dict
    labels: dict
    s1: dict
    dem: bool = False
    rf: dict = field(default_factory=lambda: {'trees':100,'feature_set':'core6'})
    hls_tile_ids: tuple = ()
    hls_native_dimensions: tuple = ()
    workload_tag: str | None = None

    @classmethod
    def from_dict(cls, raw):
        project = raw['project']
        if not isinstance(project, str) or not project.strip():
            raise ValueError('project must be explicit')
        bbox = tuple(raw['bbox'])
        if len(bbox) != 4 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in bbox):
            raise ValueError('bbox requires four finite WGS84 coordinates')
        west,south,east,north = bbox
        if not (-180 <= west < east <= 180 and -90 < south < north < 90) or (east-west)*(north-south) > .25:
            raise ValueError('small ROI required: bbox area <= 0.25 degree squared; no antimeridian wrapping')
        times = [utc_datetime(raw[k]) for k in ('start','end','pre_start','pre_end')]
        start,end,pre_start,pre_end = times
        if not start < end or not pre_start < pre_end or pre_end > start:
            raise ValueError('UTC half-open intervals must be positive and pre must end before post')
        if (end-start).total_seconds() > 62*86400 or (pre_end-pre_start).total_seconds() > 93*86400:
            raise ValueError('small probe intervals limited to 62 post / 93 pre days')
        grid = json.loads(json.dumps(raw['grid']))
        affine = grid['transform']
        if len(affine) != 6 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in affine):
            raise ValueError('grid.transform requires six finite coefficients')
        if affine[0] != 30 or affine[1] != 0 or affine[3] != 0 or affine[4] != -30:
            raise ValueError('grid must use a north-up 30 m transform')
        crs = grid['crs']
        # Restrict to metre-based WGS84 UTM grids; scale=30 in geographic CRS is not 30m.
        if not isinstance(crs,str) or not re.fullmatch(r'EPSG:(326|327)(0[1-9]|[1-5][0-9]|60)',crs):
            raise ValueError('grid.crs must be WGS84 UTM in metres')
        for key in ('width','height'):
            if type(grid[key]) is not int or not 1 <= grid[key] <= 1024:
                raise ValueError('grid dimensions must be integers in [1,1024]')
        budget = raw['budget_eecu_hours']
        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or budget <= 0:
            raise ValueError('budget_eecu_hours must be finite and positive; authorization belongs to the execution entrypoint')
        if type(raw['seed']) is not int or not 0 <= raw['seed'] <= 2147483647:
            raise ValueError('seed must be a nonnegative 32-bit integer')
        if not str(raw['output']).strip(): raise ValueError('output must be explicit')
        s1 = dict(raw.get('s1',{}))
        if s1 and (type(s1.get('relative_orbit')) is not int or not 1 <= s1['relative_orbit'] <= 175 or s1.get('pass') not in ('ASCENDING','DESCENDING')):
            raise ValueError('S1 requires explicit relative_orbit and pass')
        strings = [x.isoformat().replace('+00:00','Z') for x in times]
        rf={'trees':100,'feature_set':'core6',**raw.get('rf',{})}
        if type(rf['trees']) is not int or rf['trees'] not in (50,100,200) or rf['feature_set'] not in ('core6','core6_dem'):
            raise ValueError('RF supports trees 50/100/200 and feature_set core6/core6_dem')
        tiles=raw.get('hls_tile_ids',[])
        if not isinstance(tiles,list) or any(not isinstance(t,str) or not re.fullmatch(r'T(?:0[1-9]|[1-5][0-9]|60)[C-HJ-NP-X][A-HJ-NP-Z]{2}',t) for t in tiles) or len(set(tiles))!=len(tiles):
            raise ValueError('hls_tile_ids must be an explicit unique list of MGRS tile IDs such as T35MRT')
        dims=raw.get('hls_native_dimensions',[])
        if not isinstance(dims,list) or len(dims) not in (0,2) or any(type(n) is not int or not 1<=n<=20000 for n in dims):
            raise ValueError('hls_native_dimensions must be empty or explicit verified [width,height]')
        tag=raw.get('workload_tag')
        if tag is not None and (not isinstance(tag,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?',tag)):
            raise ValueError('workload_tag must be a valid Earth Engine workload tag')
        return cls(project,bbox,*strings,grid,Path(raw['output']),float(budget),raw['seed'],dict(raw.get('auth',{})),dict(raw.get('labels',{})),s1,bool(raw.get('dem',False)),rf,tuple(tiles),tuple(dims),tag)

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))

    def pixel_grid(self):
        from hpgee.core.grid import PixelGrid
        return PixelGrid.from_config(self.grid).to_ee()
