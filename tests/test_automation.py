"""Automation must never reset gfxinfo, launch an app, or capture a heap."""
import json
from unittest.mock import Mock, patch

import pytest

from androidperf.automation import collect


def test_stop_after_first_sample(tmp_path):
    device = Mock(serial='device')
    stop = tmp_path / 'stop'
    with patch('androidperf.device.get_pid', return_value=42), \
         patch('androidperf.collectors.cpu.sample', return_value={'cpu_pct': 3}), \
         patch('androidperf.collectors.memory.sample', return_value={'pss_kb': 100}), \
         patch('androidperf.collectors.procfs.sample', return_value={'threads': 4}), \
         patch('androidperf.collectors.battery.sample', return_value={}), \
         patch('androidperf.collectors.thermal.sample', return_value={}):
        payload = collect(device, package='example.app', pid=42, output=tmp_path,
                          stop_file=stop, interval=1, on_ready=stop.touch)
    assert payload['meta']['status'] == 'recorded'
    assert len(payload['samples']) == 1
    device.shell.assert_not_called()
    assert json.loads((tmp_path / 'samples.json').read_text())['meta']['sample_count'] == 1


def test_pid_change_preserves_incomplete_result(tmp_path):
    with patch('androidperf.device.get_pid', return_value=43):
        with pytest.raises(RuntimeError, match='process changed'):
            collect(Mock(serial='device'), package='example.app', pid=42,
                    output=tmp_path, stop_file=tmp_path / 'stop', interval=1)
    assert json.loads((tmp_path / 'samples.json').read_text())['meta']['status'] == 'incomplete'


def test_missing_first_metrics_fails(tmp_path):
    with patch('androidperf.device.get_pid', return_value=42), \
         patch('androidperf.collectors.cpu.sample', return_value={}), \
         patch('androidperf.collectors.memory.sample', return_value={}), \
         patch('androidperf.collectors.procfs.sample', return_value={}), \
         patch('androidperf.collectors.battery.sample', return_value={}), \
         patch('androidperf.collectors.thermal.sample', return_value={}):
        with pytest.raises(RuntimeError, match='missing CPU or PSS'):
            collect(Mock(serial='device'), package='example.app', pid=42,
                    output=tmp_path, stop_file=tmp_path / 'stop', interval=1)
    assert json.loads((tmp_path / 'samples.json').read_text())['meta']['status'] == 'incomplete'
