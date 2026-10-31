"""Deliver one frozen target; all primary sensors precede one recovery pass."""

from contextlib import ExitStack
import fcntl
import functools
from math import lcm
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote
import uuid
from hpgee.runtime.admission import Admission, LimitedTransport, split_budget
from .catalog import atomic_json
from .delivery import LAYOUT, publish, verified
from .images import request_body
from .manifest import digest, requests_for_task, request_owner
from hpgee.transfer.queue import PreparedRequests, run_queue


class CredentialHeaders:
    def __init__(self, credentials, http):
        self.credentials, self.http = credentials, http
        self.lock = threading.Lock()

    def get(self, http=None):
        with self.lock:
            if not self.credentials.valid:
                from google_auth_httplib2 import Request

                self.credentials.refresh(Request(http or self.http))
            headers = {"Content-Type": "application/json"}
            self.credentials.apply(headers)
            return headers

    def invalidate(self, used):
        with self.lock:
            current = {}
            self.credentials.apply(current)
            if current.get("Authorization", current.get("authorization")) == used.get(
                "Authorization", used.get("authorization")
            ):
                self.credentials.token = None


def execute(
    store,
    *,
    ee,
    http,
    credentials,
    project,
    endpoint,
    tile,
    request_mode,
    workers,
    rank,
    hosts,
    output,
    scratch,
    retry_rounds=1,
    seconds=None,
    make_body=request_body,
    total_workers=None,
    http_budget=40,
    session_epoch=None,
    tag_prefix=None,
    min_free_bytes=0,
    max_bytes=None,
    ownership_pixels=None,
):
    began = time.monotonic()
    epoch = session_epoch if session_epoch is not None else time.time()
    total_workers = total_workers or workers
    ownership_pixels = ownership_pixels or lcm(1024, tile)
    if ownership_pixels % tile:
        raise ValueError("Host macrocell must be divisible by request size")
    if (
        endpoint not in ("standard", "high-volume")
        or request_mode not in ("lazy", "eager")
        or retry_rounds not in (0, 1)
        or hosts < 1
        or not 0 <= rank < hosts
        or workers < 1
        or workers != total_workers // hosts + int(rank < total_workers % hosts)
    ):
        raise ValueError("Invalid fixed-task execution settings")
    limit = split_budget(http_budget, rank, hosts)
    if not isinstance(http, LimitedTransport):
        http = LimitedTransport(http, Admission(limit))
    if http.admission.limit != limit:
        raise ValueError("Transport admission differs from declared global budget")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch)
    settings = dict(
        manifest=store.id,
        project=project,
        endpoint=endpoint,
        tile=tile,
        encoding=LAYOUT,
        requests=request_mode,
        workers=total_workers,
        http_budget=http_budget,
        hosts=hosts,
        rank=rank,
    )
    if ownership_pixels != 1024:
        settings["ownership_pixels"] = ownership_pixels
    signature = digest({k: v for k, v in settings.items() if k != "rank"})
    with (output / "transfer.lock").open("a") as lock, ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contract = output / "contract.json"
        if contract.exists():
            saved = json.loads(contract.read_text())
            if saved["settings"] != settings:
                raise ValueError("Output belongs to a different fixed task or configuration")
        else:
            saved = dict(
                settings=settings,
                first_started_epoch=epoch,
                tag=(tag_prefix or "df-" + uuid.uuid4().hex[:20]),
            )
            atomic_json(contract, saved)

        @functools.lru_cache(maxsize=16)
        def packed(task_id):
            return {r["id"]: r for r in requests_for_task(store[task_id], tile)}

        index = {}
        groups = {sensor: [] for sensor in ("S30", "L30", "S1", "S2", "DW")}
        for task_id in store.index:
            for uid, request in packed(task_id).items():
                if request["sensor"] not in groups:
                    raise ValueError("Unknown target sensor")
                if request_owner(request, hosts, macro_pixels=ownership_pixels) == rank:
                    index[uid] = (task_id, len(request["cells"]))
                    groups[request["sensor"]].append(uid)

        def unit(uid):
            return packed(index[uid][0])[uid]

        if tag_prefix is not None and saved["tag"] != tag_prefix:
            raise ValueError("Workload tag changed within a trial")
        tag = saved["tag"] + "-" + str(rank)
        preparation = time.monotonic()
        body_cache = (
            stack.enter_context(
                PreparedRequests(
                    scratch / ("eager-" + uuid.uuid4().hex),
                    (
                        (uid, make_body(ee, store[index[uid][0]], unit(uid), tag))
                        for uid in index
                    ),
                )
            )
            if request_mode == "eager"
            else None
        )
        body_preparation = time.monotonic() - preparation
        auth = CredentialHeaders(credentials, http)
        writers = threading.BoundedSemaphore(2)
        journal = stack.enter_context((output / "blocks.jsonl").open("a"))
        trace = stack.enter_context((output / "http.jsonl").open("a"))
        trace_lock = threading.Lock()
        old_observer = getattr(http.transport, "observer", None)

        def observe(event):
            if old_observer:
                old_observer(event)
            with trace_lock:
                trace.write(json.dumps(event, sort_keys=True) + "\n")
                trace.flush()

        http.transport.observer = observe
        stack.callback(setattr, http.transport, "observer", old_observer)
        counters = dict(
            successful_requests=0,
            skipped_requests=0,
            failed_attempts=0,
            quota_rejections=0,
            accepted_bytes=0,
            completed_cells=0,
            quality_kept_cells=0,
            quality_rejected_cells=0,
            quality_valid_pixels=0,
            quality_clear_pixels=0,
        )
        completed = set()
        pause = [0.0]
        pause_lock = threading.Lock()
        phase = ["primary"]
        last_tick = [0.0]
        deadline = began + seconds if seconds is not None else float("inf")
        url = (
            (
                "https://earthengine-highvolume.googleapis.com"
                if endpoint == "high-volume"
                else "https://earthengine.googleapis.com"
            )
            + "/v1/projects/"
            + quote(project, safe="")
            + "/image:computePixels"
        )

        def worker(uid):
            request = unit(uid)
            started = time.monotonic()
            row = dict(id=uid, sensor=request["sensor"], status="failed", accepted_bytes=0)
            try:
                receipt = verified(request, output / "payload")
                if receipt:
                    row.update(
                        {
                            k: receipt.get(k, 0)
                            for k in (
                                "quality_kept_cells",
                                "quality_rejected_cells",
                                "quality_valid_pixels",
                                "quality_clear_pixels",
                            )
                        }
                    )
                    row.update(status="skipped", cells=len(receipt["cells"]))
                    return row
                while time.monotonic() < pause[0]:
                    time.sleep(max(0, min(0.2, pause[0] - time.monotonic())))
                body = (
                    body_cache[uid]
                    if body_cache is not None
                    else make_body(ee, store[index[uid][0]], request, tag)
                )
                headers = auth.get()
                response, payload = http.request(url, method="POST", body=body, headers=headers)
                row["http_status"] = response.status
                if response.status != 200:
                    # Keep structured server reasons; never store arbitrary response
                    # messages, which can contain source paths or signed requests.
                    try:
                        detail = json.loads(payload).get("error", {})
                        row["server_status"] = detail.get("status")
                        row["server_reasons"] = [
                            d.get("reason") for d in detail.get("details", []) if d.get("reason")
                        ]
                    except (ValueError, AttributeError, TypeError):
                        pass
                if response.status != 200:
                    if response.status == 429:
                        retry = (
                            response.get("retry-after", "5") if hasattr(response, "get") else "5"
                        )
                        delay = min(60, max(5, int(retry))) if str(retry).isdigit() else 5
                        with pause_lock:
                            pause[0] = max(pause[0], time.monotonic() + delay)
                    if response.status == 401:
                        auth.invalidate(headers)
                    raise RuntimeError("HTTP " + str(response.status))
                with writers:
                    receipt = publish(payload, request, output / "payload")
                row.update(
                    status="success", accepted_bytes=len(payload), cells=len(receipt["cells"])
                )
                row.update(
                    {
                        k: receipt.get(k, 0)
                        for k in (
                            "quality_kept_cells",
                            "quality_rejected_cells",
                            "quality_valid_pixels",
                            "quality_clear_pixels",
                        )
                    }
                )
            except Exception as error:
                row.update(error_type=type(error).__name__)
                if isinstance(error, ValueError):
                    row["error"] = str(error)
            finally:
                row["seconds"] = time.monotonic() - started
            return row

        def record(row):
            row = dict(row, phase=phase[0])
            journal.write(json.dumps(row, sort_keys=True) + "\n")
            journal.flush()
            if row.get("http_status") == 429:
                counters["quota_rejections"] += 1
            if row["status"] == "failed":
                counters["failed_attempts"] += 1
            elif row["id"] not in completed:
                completed.add(row["id"])
                counters["completed_cells"] += row["cells"]
                for key in (
                    "quality_kept_cells",
                    "quality_rejected_cells",
                    "quality_valid_pixels",
                    "quality_clear_pixels",
                ):
                    counters[key] += row.get(key, 0)
                counters[
                    "successful_requests" if row["status"] == "success" else "skipped_requests"
                ] += 1
                counters["accepted_bytes"] += row["accepted_bytes"]

        def tick(force=False):
            elapsed = time.monotonic() - began
            if not force and elapsed - last_tick[0] < 30:
                return
            last_tick[0] = elapsed
            done = counters["completed_cells"]
            required = sum(n for _, n in index.values())
            eta = (required - done) * elapsed / done if done else None
            snapshot = dict(
                counters,
                phase=phase[0],
                rank=rank,
                hosts=hosts,
                started_epoch=epoch,
                elapsed_seconds=elapsed,
                required_cells=required,
                ETA_seconds=eta,
                average_MiB_s=counters["accepted_bytes"] / max(0.001, elapsed) / 1048576,
            )
            atomic_json(output / "progress.json", snapshot)
            fraction = done / required if required else 1
            bar = "#" * int(20 * fraction) + "-" * (20 - int(20 * fraction))
            eta_text = "?" if eta is None else str(int(eta)) + "s"
            print(
                f"rank={rank} [{bar}] {done}/{required} ETA {eta_text} BANDWIDTH(avg) {snapshot['average_MiB_s']:.2f} MiB/s failed_attempts={counters['failed_attempts']}",
                flush=True,
            )

        def admitted():
            import shutil

            return (
                max_bytes is None or counters["accepted_bytes"] < max_bytes
            ) and shutil.disk_usage(output).free >= min_free_bytes

        pending = []
        storage_stopped = False
        for sensor, ids in groups.items():
            if not ids:
                continue
            remaining = deadline - time.monotonic()
            result = run_queue(
                ids,
                worker,
                workers=workers,
                on_result=record,
                retry_rounds=0,
                seconds=max(0, remaining) if seconds is not None else None,
                on_tick=tick,
                before_submit=admitted,
            )
            pending += result["pending"]
            storage_stopped |= result["admission_stopped"]
        if retry_rounds and pending and not storage_stopped and time.monotonic() < deadline:
            phase[0] = "recovery"
            remaining = deadline - time.monotonic()
            result = run_queue(
                pending,
                worker,
                workers=workers,
                on_result=record,
                retry_rounds=0,
                seconds=max(0, remaining) if seconds is not None else None,
                on_tick=tick,
                before_submit=admitted,
            )
            pending = result["pending"]
            storage_stopped |= result["admission_stopped"]
        tick(True)
        elapsed = time.monotonic() - began
        finished = time.time()
        wall = finished - saved["first_started_epoch"]
        global_complete = not pending and hosts == 1
        report = dict(
            counters,
            case_key=signature,
            manifest=store.id,
            rank=rank,
            hosts=hosts,
            complete=global_complete,
            complete_local=not pending,
            failed_requests=len(pending),
            required_requests=len(index),
            required_cells=sum(n for _, n in index.values()),
            global_required_cells=store.summary["required_cells"],
            target_area_km2=store.summary["target_area_km2"],
            first_started_epoch=saved["first_started_epoch"],
            started_epoch=epoch,
            finished_epoch=finished,
            session_seconds=finished - epoch,
            execution_seconds=elapsed,
            task_wall_seconds=wall,
            seconds_per_km2=wall / store.summary["target_area_km2"]
            if global_complete
            and store.summary["target_area_km2"] is not None
            and store.summary["target_area_km2"] > 0
            else None,
            accepted_MiB_s=counters["accepted_bytes"] / max(elapsed, 0.001) / 1048576,
            body_preparation_seconds=body_preparation,
            peak_http=http.admission.peak,
            sampling_status="complete"
            if not pending
            else "storage_limited"
            if storage_stopped
            else "time_limited"
            if time.monotonic() >= deadline
            else "failed",
        )
        atomic_json(output / "pending.json", pending)
        atomic_json(output / "result.json", report)
        return report
