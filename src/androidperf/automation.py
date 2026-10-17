"""Headless, attach-only collection for externally controlled experiments."""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2))
    temporary.replace(path)


def collect(device, *, package, pid, output, stop_file, interval, on_ready=lambda: None):
    # 实验宿主拥有帧统计窗口；这里不调用gfxinfo reset、不启动应用、不抓堆。
    from .collectors import battery, cpu, memory, procfs, thermal
    from .device import get_pid

    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('interval must be finite and positive')
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    payload = {'meta': {'package': package, 'pid': pid, 'serial': device.serial,
                        'device': {}, 'started_at': datetime.now(timezone.utc).isoformat(),
                        'interval_s': interval, 'frame_scope': 'external_owner',
                        'heap_capture': False, 'status': 'running',
                        'started_unix_s': time.time()}, 'samples': [], 'events': [], 'gc_events': []}
    collectors = [('cpu', lambda: cpu.sample(device, pid=pid)),
                  ('memory', lambda: memory.sample(device, package=package)),
                  ('procfs', lambda: procfs.sample(device, pid=pid)),
                  ('battery', lambda: battery.sample(device)),
                  ('thermal', lambda: thermal.sample(device))]
    try:
        while not stop_file.exists():
            tick = time.monotonic()
            if get_pid(device, package) != pid:
                raise RuntimeError('experiment process changed or exited')
            sample = {'t': round(tick - started, 3)}
            for name, collector in collectors:
                try:
                    sample.update(collector())
                except Exception as error:
                    sample['_' + name + '_error'] = repr(error)
            if get_pid(device, package) != pid:
                raise RuntimeError('experiment process changed during sample')
            sample['collection_duration_s'] = time.monotonic() - tick
            payload['samples'].append(sample)
            write_json(output / 'samples.json', payload)
            if 'cpu_pct' not in sample or 'pss_kb' not in sample:
                raise RuntimeError('sample missing CPU or PSS')
            if len(payload['samples']) == 1:
                on_ready()
            deadline = tick + interval
            while not stop_file.exists() and time.monotonic() < deadline:
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        if not payload['samples']:
            raise RuntimeError('no samples collected')
        payload['meta']['status'] = 'recorded'
    except BaseException as error:
        payload['meta'].update(status='incomplete', error=str(error))
        raise
    finally:
        payload['meta'].update(sample_count=len(payload['samples']), ended_unix_s=time.time(), ended_at=datetime.now(timezone.utc).isoformat())
        write_json(output / 'samples.json', payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--interval', type=float, default=1)
    args = parser.parse_args()
    from .device import pick_device
    from .report.generate import generate_report
    device, info = pick_device(args.serial)
    collect(device, package=args.package, pid=args.pid, output=args.output,
            stop_file=args.output / 'stop', interval=args.interval,
            on_ready=lambda: write_json(args.output / 'ready.json', {'pid': args.pid, 'unix_s': time.time()}))
    path = args.output / 'samples.json'
    payload = json.loads(path.read_text())
    payload['meta']['device'] = {'serial': info.serial, 'model': info.model,
                                 'manufacturer': info.manufacturer, 'sdk': info.sdk}
    write_json(path, payload)
    generate_report(path, args.output / 'report.html')


if __name__ == '__main__':
    main()
