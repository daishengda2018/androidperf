"""外部实验驱动的采集会话；采样时机由调用方控制，没有后台轮询和帧重置。"""

import time
from datetime import datetime, timezone
from pathlib import Path

from .automation import write_json
from .collectors import battery, cpu, memory, procfs, thermal


class CollectorDevice:
    """让通用采集器的单条shell命令沿用实验设备的超时与操作记录。"""

    def __init__(self, device):
        self.device = device

    def shell(self, command):
        return self.device.call("shell", command)


class CaptureSession:
    def __init__(self, device, root, result, *, package):
        self.device, self.root, self.result = device, Path(root), result
        self.package = package
        self.pid = str(result["pid"])
        self.collector_device = CollectorDevice(device)
        self.state = result.setdefault("androidperf", {})
        self.payload = {
            "meta": {
                "package": package,
                "pid": self.pid,
                "device": result.get("device", {}),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "frame_scope": "case_window",
                "heap_capture": False,
                "status": "running",
                "interval_s": result.get("parameters", {}).get("sample_interval_s"),
            },
            "samples": [],
            "events": [],
            "gc_events": [],
        }
        self.finished = False

    def persist(self):
        try:
            write_json(self.root / "samples.json", self.payload)
        except Exception as error:
            self.state.update(status="incomplete", error=str(error))
            self.payload["meta"].update(status="incomplete", error=str(error))
            raise

    def check(self):
        if self.device.shell("pidof", "-s", self.package) != self.pid:
            raise RuntimeError("实验进程改变，不能继续采集")

    def start(self):
        self.state.update(
            status="starting", frame_scope="case_window", memory_scope="PSS_and_Alloc"
        )
        try:
            self.sample("collector_ready")
            write_json(self.root / "ready.json", {"pid": self.pid, "unix_s": time.time()})
            self.state["status"] = "collecting"
        except BaseException as error:
            self.state.update(status="incomplete", error=str(error) or type(error).__name__)
            self.payload["meta"].update(status="incomplete", error=self.state["error"])
            self.persist()
            raise

    def sample(self, phase="periodic"):
        started = time.monotonic()
        row = {"t": started - self.device.started, "phase": phase}
        try:
            self.check()
            text = memory.capture_meminfo(
                self.device, self.package, self.root, self.result, phase, started
            )
            row.update(memory.parse_meminfo(text))
            allocated = memory.memory_values(text)
            for source, target in (
                ("Native Alloc", "native_alloc_kb"),
                ("Runtime Alloc", "runtime_alloc_kb"),
            ):
                if source in allocated:
                    row[target] = allocated[source] * 1024
            for name, collector, kwargs in (
                ("cpu", cpu.sample, {"pid": int(self.pid)}),
                ("procfs", procfs.sample, {"pid": int(self.pid)}),
                ("battery", battery.sample, {}),
                ("thermal", thermal.sample, {}),
            ):
                try:
                    row.update(collector(self.collector_device, **kwargs))
                except Exception as error:
                    row["_" + name + "_error"] = str(error)
            self.check()
            if "cpu_pct" not in row or "pss_kb" not in row:
                raise RuntimeError("sample missing CPU or PSS")
        except BaseException as error:
            self.state.update(status="incomplete", error=str(error))
            self.payload["meta"].update(status="incomplete", error=str(error))
            raise
        finally:
            row["collection_duration_s"] = time.monotonic() - started
            self.payload["samples"].append(row)
            self.persist()

    def finish(self):
        if self.finished:
            return
        self.finished = True
        if not self.payload["samples"]:
            self.state.update(status="incomplete", error="no samples collected")
        if self.state.get("status") != "incomplete":
            self.state["status"] = "recorded"
        self.state["sample_count"] = len(self.payload["samples"])
        self.payload["meta"].update(
            status=self.state["status"],
            sample_count=len(self.payload["samples"]),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            self.persist()
        except Exception as error:
            self.state.update(status="incomplete", error=str(error))
            self.payload["meta"].update(status="incomplete", error=str(error))
            raise
        if self.state["status"] == "incomplete":
            raise RuntimeError(self.state.get("error", "incomplete collection"))
