"""Run-scoped telemetry with durable start, request tags and late reconciliation."""
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import json
import threading
import time
from hpgee.accounting.sampling import CloudEECUReader


class UsageRun:
    def __init__(self,directory,*,project,tag_prefix,credentials,poll_seconds=120,client=None,start=None):
        self.credentials=credentials
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/'usage-identity.json'
        identity={'project':project,'tag_prefix':tag_prefix}
        if path.exists():
            saved=json.loads(path.read_text())
            if any(saved[k]!=v for k,v in identity.items()):raise ValueError('Usage run identity mismatch')
        else:
            saved=dict(identity,start=start or datetime.now(timezone.utc).isoformat())
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(saved));temp.replace(path)
        self.reader=CloudEECUReader(project,tag_prefix,start=saved['start'],credentials=credentials,tag_prefix=True,client=client)
        self.interval=poll_seconds;self.stop_event=threading.Event();self.lock=threading.Lock();self.thread=None

    def append(self,name,row):
        with self.lock,(self.directory/name).open('a') as stream:stream.write(json.dumps(row,sort_keys=True)+'\n')

    def observe_http(self,event):
        self.append('http-attempts.jsonl',event)

    def snapshot(self):
        started=time.monotonic()
        calls_before=getattr(self.reader._client,"http_attempts",None)
        try:
            snapshot=self.reader.read(); row=dict(asdict(snapshot),raw_points=self.reader.raw_points)
        except Exception as exc:
            row=dict(timestamp=datetime.now(timezone.utc).isoformat(),available=False,completed_seconds=None,
                     in_progress_seconds=None,error=type(exc).__name__,raw_points=[])
        row.update(monitor_wall_seconds=time.monotonic()-started,telemetry_complete=False,
                   metric_queries_planned=2,monitoring_api_calls=(self.reader._client.http_attempts-calls_before if calls_before is not None else None),monitoring_eecu_seconds=None,
                   unknown_cost='failed/unreported requests and reporting delay remain unknown')
        self.append('usage-snapshots.jsonl',row);return row

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                self.snapshot();self.stop_event.wait(self.interval)
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=65)
        if self.thread and self.thread.is_alive():raise RuntimeError('Usage reader did not drain')
        return self.snapshot()
