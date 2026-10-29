from datetime import datetime, timezone
from hpgee.transfer.engine import DownloadEngine
from hpgee.core.models import RunConfig, EECUMonitorConfig, SampleRecord, TaskResult

class EE: pass

def test_frozen_callback_reuses_engine_and_skips_only_verified(tmp_path):
    engine=DownloadEngine('project-x',RunConfig(tmp_path,workers=2,eecu=EECUMonitorConfig(enabled=False)),ee_module=EE())
    sent=[];published=[]
    def download(task,sample,image,bands,grid,tag,output,mask,scene,date,**kwargs):
        sent.append((sample,tag));output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'tiff')
        return TaskResult(task,sample,'success',str(output),4,0.1,1)
    engine._download_pixels=download
    records=[SampleRecord(x,{'type':'Point','coordinates':[0,0]},datetime.now(timezone.utc)) for x in ['a','b']]
    result=engine.download_frozen_blocks(records,image_builder=lambda s:object(),bands=['x'],grid_builder=lambda s:{},
        verified=lambda s:s.sample_id=='a',publish=lambda s,r:published.append(s.sample_id),run_id='trial')
    assert result.succeeded==1 and result.skipped==1
    assert [s[0] for s in sent]==['b'] and published==['b']


def test_frozen_progress_reports_published_skipped_and_failed_results(tmp_path):
    engine = DownloadEngine('project-x', RunConfig(tmp_path, workers=2,
        eecu=EECUMonitorConfig(enabled=False)), ee_module=EE())
    published = []
    def download(task, sample, image, bands, grid, tag, output, mask, scene, date, **kwargs):
        if sample == 'c':
            raise RuntimeError('expected offline failure')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'tiff')
        return TaskResult(task, sample, 'success', str(output), 4, 0.1, 1)
    engine._download_pixels = download
    seen = []
    def progress(result):
        if result.status == 'success':
            assert result.sample_id in published
        seen.append((result.sample_id, result.status))
    records = [SampleRecord(x, {'type': 'Point', 'coordinates': [0, 0]}) for x in ('a', 'b', 'c')]
    result = engine.download_frozen_blocks(records, image_builder=lambda s: object(), bands=['x'],
        grid_builder=lambda s: {}, verified=lambda s: s.sample_id == 'a',
        publish=lambda s, r: published.append(s.sample_id), progress=progress, run_id='progress')
    assert dict(seen) == {'a': 'skipped', 'b': 'success', 'c': 'failed'}
    assert len(seen) == 3 and result.failed == 1


def test_frozen_progress_failure_does_not_interrupt_downloads(tmp_path):
    engine = DownloadEngine('project-x', RunConfig(tmp_path, workers=1,
        eecu=EECUMonitorConfig(enabled=False)), ee_module=EE())
    calls = []
    def progress(result):
        calls.append(result.sample_id)
        raise OSError('display unavailable')
    engine._download_pixels = lambda task, sample, *a, **kw: TaskResult(task, sample, 'success', None, 4, 0.1, 1)
    records = [SampleRecord(x, {'type': 'Point', 'coordinates': [0, 0]}) for x in ('a', 'b')]
    result = engine.download_frozen_blocks(records, image_builder=lambda s: object(), bands=['x'],
        grid_builder=lambda s: {}, progress=progress, run_id='broken-progress')
    assert result.succeeded == 2
    assert len(calls) == 1
