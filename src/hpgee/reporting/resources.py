"""Low-frequency own-process resource counters; node networking is shared."""
from datetime import datetime,timezone
import os
from pathlib import Path
import threading


class ResourceMonitor:
    def __init__(self,append,*,scratch,destination,interval=10):
        self.append=append;self.scratch=scratch;self.destination=destination;self.interval=interval
        self.stop_event=threading.Event();self.thread=None

    def snapshot(self):
        import psutil
        process=psutil.Process();own=[process]+process.children(recursive=True)
        rows=[]
        for p in own:
            try:
                rows.append(dict(pid=p.pid,cpu=p.cpu_times()._asdict(),rss=p.memory_info().rss,io=p.io_counters()._asdict()))
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        return dict(timestamp=datetime.now(timezone.utc).isoformat(),cpu_affinity=process.cpu_affinity(),
            slurm_cpus=os.environ.get('SLURM_CPUS_PER_TASK'),processes=rows,
            node_network_shared={k:v._asdict() for k,v in psutil.net_io_counters(pernic=True).items()},
            scratch_own_bytes=sum(p.stat().st_size for p in Path(self.scratch).rglob('*') if p.is_file()),
            scratch_free=psutil.disk_usage(str(self.scratch)).free,persistent_free=psutil.disk_usage(str(self.destination)).free,
            limitations='Node network includes other jobs; logical process I/O is not physical NFS IOPS.')

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:row=self.snapshot()
                except Exception as exc:row={'timestamp':datetime.now(timezone.utc).isoformat(),'error_type':type(exc).__name__}
                self.append('resources.jsonl',row);self.stop_event.wait(self.interval)
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=10)
