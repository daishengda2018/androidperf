"""统一会话回归：采集原件、进程边界和失败状态，全部使用离线设备替身。"""

import json
from unittest.mock import Mock, patch

import pytest

from androidperf.capture import CaptureSession
from androidperf.collectors import fps

MEMINFO = """Pss Private Private SwapPss Heap Heap Heap
 Total Dirty Clean Dirty Size Alloc Free
 Native Heap 1024 512 0 0 8192 6144 2048
 Dalvik Heap 2048 1024 0 0 8192 3072 5120
 App Summary
 Java Heap: 1024
 Native Heap: 512
 Graphics: 2048
 TOTAL PSS: 10240
"""


def session(tmp_path):
    device = Mock(started=0)
    device.shell.side_effect = lambda *args: "42" if args[0] == "pidof" else MEMINFO
    device.call.return_value = "PID %CPU\n42 25.0"
    result = {"pid": "42", "parameters": {"sample_interval_s": 1}}
    return CaptureSession(device, tmp_path, result, package="test.app"), device, result


def test_one_meminfo_supplies_pss_and_alloc_and_no_frame_reset(tmp_path):
    capture, device, result = session(tmp_path)
    capture.start()
    capture.finish()
    samples = json.loads((tmp_path / "samples.json").read_text())["samples"]
    assert samples[0]["native_kb"] == 512
    assert samples[0]["native_alloc_kb"] == 6144
    assert samples[0]["runtime_alloc_kb"] == 3072
    assert len(result["meminfo_samples"]) == 1
    assert (tmp_path / "meminfo-0000.txt").read_text() == MEMINFO
    assert (tmp_path / "ready.json").is_file()
    assert sum(c.args[:2] == ("dumpsys", "meminfo") for c in device.shell.call_args_list) == 1
    assert not any("gfxinfo" in str(c) for c in device.mock_calls)
    assert result["androidperf"]["status"] == "recorded"


@pytest.mark.parametrize("failure", ["pid_before", "pid_after", "cpu", "pss", "interrupt"])
def test_invalid_sample_never_creates_ready_and_keeps_partial_data(tmp_path, failure):
    capture, device, result = session(tmp_path)
    if failure.startswith("pid"):
        count = 0

        def shell(*args):
            nonlocal count
            if args[0] != "pidof":
                return MEMINFO
            count += 1
            return "43" if failure == "pid_before" or count > 1 else "42"

        device.shell.side_effect = shell
    elif failure == "cpu":
        device.call.return_value = ""
    elif failure == "pss":
        device.shell.side_effect = lambda *args: "42" if args[0] == "pidof" else "unavailable"
    else:
        device.call.side_effect = KeyboardInterrupt()
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        capture.start()
    assert not (tmp_path / "ready.json").exists()
    assert json.loads((tmp_path / "samples.json").read_text())["meta"]["status"] == "incomplete"
    with pytest.raises(RuntimeError):
        capture.finish()
    assert result["androidperf"]["status"] == "incomplete"


def test_finalize_write_failure_marks_state_incomplete(tmp_path):
    capture, _, result = session(tmp_path)
    capture.start()
    with patch.object(capture, "persist", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            capture.finish()
    assert result["androidperf"]["status"] == "incomplete"


def test_explicit_frame_window_reads_do_not_reset(tmp_path):
    device = Mock(command=["adb"])
    fps.reset(device, "test.app")
    fps.read_window(device, "test.app")
    fps.read_window(device, "test.app", framestats=True)
    assert [c.args for c in device.shell.call_args_list] == [
        ("dumpsys", "gfxinfo", "test.app", "reset"),
        ("dumpsys", "gfxinfo", "test.app"),
        ("dumpsys", "gfxinfo", "test.app", "framestats"),
    ]
    assert fps.frame_values("Total frames rendered: 0\n95th percentile: 4950ms\n") == {"frames": 0}
    assert fps.frame_values("unavailable") == {}
    assert fps.frame_values("GPU 95th percentile: 5ms\nLegacy 95th percentile: 9ms\n") == {}


def test_ready_write_failure_is_incomplete(tmp_path):
    from androidperf.automation import write_json

    capture, _, result = session(tmp_path)

    def write(path, payload):
        if path.name == "ready.json":
            raise OSError("cannot write ready")
        return write_json(path, payload)

    with patch("androidperf.capture.write_json", side_effect=write):
        with pytest.raises(OSError, match="cannot write ready"):
            capture.start()
    assert result["androidperf"]["status"] == "incomplete"
    assert json.loads((tmp_path / "samples.json").read_text())["meta"]["status"] == "incomplete"


