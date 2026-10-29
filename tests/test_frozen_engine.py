from datetime import datetime, timezone
from geesampler.engine import DownloadEngine
from geesampler.models import RunConfig,EECUMonitorConfig,SampleRecord,TaskResult

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
