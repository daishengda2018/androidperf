"""Memory usage via `dumpsys meminfo <package>`.

We read the `App Summary` block which provides a stable, documented set of
buckets (Java Heap, Native Heap, Graphics, TOTAL PSS). Older Android versions
may not include every row; unknown rows are simply absent from the result.
"""

from __future__ import annotations

import re

from adbutils import AdbDevice

# Match "<label>:   <number>" at the start of a line. The App Summary block
# sometimes has trailing RSS columns on the same row, so we don't anchor to
# end-of-line.
_ROW_RE = re.compile(r"^\s*([A-Za-z][\w /\.]+?):\s+([\d,]+)")

_FIELDS = {
    "Java Heap": "java_kb",
    "Native Heap": "native_kb",
    "Code": "code_kb",
    "Stack": "stack_kb",
    "Graphics": "gfx_kb",
    "Private Other": "private_other_kb",
    "System": "system_kb",
    "TOTAL PSS": "pss_kb",
    "TOTAL": "pss_kb",  # very old Android shortcut
}


def parse_meminfo(output: str) -> dict[str, float]:
    """Pull numeric rows out of the `App Summary` section."""
    lines = output.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if "App Summary" in ln)
    except StopIteration:
        return {}

    result: dict[str, float] = {}
    for line in lines[start:]:
        match = _ROW_RE.match(line)
        if not match:
            continue
        label, num = match.group(1).strip(), match.group(2)
        key = _FIELDS.get(label)
        if not key:
            continue
        try:
            result[key] = float(num.replace(",", ""))
        except ValueError:
            continue
        if label.startswith("TOTAL"):
            break
    return result


def sample(device: AdbDevice, *, package: str, **_: object) -> dict[str, float]:
    # `-s` (short) skips the per-allocation breakdown that makes the default
    # dump take 3-6 s on apps with large heaps; the `App Summary` block we
    # parse below is still present.
    out = device.shell(f"dumpsys meminfo -s {package}")
    return parse_meminfo(out)


# Objects block has labels both at line-start and mid-line, e.g.:
#   AppContexts:   4           Activities:   3
_OBJ_RE = re.compile(r"\b(Activities|Views|AppContexts):\s+(\d+)")


def parse_meminfo_objects(output: str) -> dict[str, float]:
    """Extract object counts from the Objects section of dumpsys meminfo."""
    result: dict[str, float] = {}
    for m in _OBJ_RE.finditer(output):
        label, val = m.group(1), m.group(2)
        if label == "Activities":
            result["obj_activities"] = float(val)
        elif label == "Views":
            result["obj_views"] = float(val)
        elif label == "AppContexts":
            result["obj_app_contexts"] = float(val)
    return result


def sample_objects(device: AdbDevice, *, package: str, **_: object) -> dict[str, float]:
    # No -s flag so the Objects block is present; grep keeps it fast.
    out = device.shell(f"dumpsys meminfo {package} | grep -A10 '^ Objects'")
    return parse_meminfo_objects(out)


def memory_values(text):
    """按表头读取 Heap Alloc 和 Graphics/Total PSS，统一转换为 MiB；缺字段不填零。"""
    result = {}
    # RSS会使Heap列右移；按第二行表头定位Alloc，不把Size当作已分配内存。
    header = re.search(r"^[ \t]*Total[ \t]+Dirty[^\n]*[ \t]+Size[ \t]+Alloc[ \t]+Free[ \t]*$", text, re.MULTILINE)
    columns = header.group().split() if header else None
    for label, key, index in (
        ("Native Heap", "Native Alloc", None),
        ("Dalvik Heap", "Runtime Alloc", None),
        ("Graphics", "Graphics PSS", 0),
    ):
        match = re.search(r"^[ \t]*" + label + r"[ \t]+(\d+(?:[ \t]+\d+)*)[ \t]*$", text, re.MULTILINE)
        if match:
            numbers = match[1].split()
            if index is None:
                if columns:
                    if len(numbers) != len(columns):
                        continue
                    index = columns.index("Alloc")
                elif len(numbers) in (7, 8):
                    # 兼容只保留数据行的旧产物；仅接受已知布局，末三列是Size/Alloc/Free。
                    index = len(numbers) - 2
                else:
                    continue
            if len(numbers) > index:
                result[key] = int(numbers[index]) / 1024
    if "Graphics PSS" not in result:
        match = re.search(r"^\s*Graphics:\s*(\d+)", text, re.MULTILINE)
        if match:
            result["Graphics PSS"] = int(match[1]) / 1024
    # 优先App Summary中的TOTAL PSS，兼容旧版TOTAL表格。
    match = re.search(r"TOTAL PSS:\s*(\d+)", text)
    if not match:
        match = re.search(r"^\s*TOTAL\s+(\d+)", text, re.MULTILINE)
    if match:
        result["Total PSS"] = int(match[1]) / 1024
    return result


def capture_meminfo(device, package, root, result, phase, started):
    """同一份原件解析PSS及Alloc，保留时间与阶段用于重建。"""
    import time

    text = device.shell("dumpsys", "meminfo", package)
    if "No process found" in text:
        raise RuntimeError("meminfo未找到实验进程")
    samples = result.setdefault("meminfo_samples", [])
    name = f"meminfo-{len(samples):04d}.txt"
    (root / name).write_text(text)
    samples.append({"file": name, "phase": phase, "at_s": started - device.started, "duration_s": time.monotonic() - started})
    return text
