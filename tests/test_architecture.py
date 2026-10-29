"""Canonical package, compatibility and end-to-end offline command contracts."""
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
MAPPING = json.loads((ROOT / 'docs/module-migration.json').read_text())


@pytest.mark.parametrize('legacy,canonical', [
    (old, new) for old, new in MAPPING.items()
    if not old.endswith('__main__') and old != 'geesampler.recipes'
])
def test_legacy_module_is_the_canonical_object(legacy, canonical):
    assert importlib.import_module(legacy) is importlib.import_module(canonical)


def test_canonical_workflows_do_not_import_legacy_packages():
    code = ('import sys; import hpgee; assert "ee" not in sys.modules; '
            'import hpgee.sampling.sampler, hpgee.workflows.detection, hpgee.transfer.products; '
            'assert not any(n.split(".")[0] in {"geesampler", "geecomputation"} for n in sys.modules)')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_old_yaml_callable_and_new_callable_are_identical():
    from hpgee.sampling.config import load_callable
    assert load_callable('geesampler.recipes.sentinel2:sentinel2_collection') is load_callable(
        'hpgee.recipes.sentinel2:sentinel2_collection')


def test_unified_compute_command_builds_real_offline_manifest(tmp_path):
    from hpgee.cli.main import main
    raw = json.loads((ROOT / 'examples/computation/small-roi.json').read_text())
    raw['output'] = str(tmp_path / 'out')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw))
    assert main(['compute', 'manifest', '--config', str(path)]) == 0
    result = json.loads((tmp_path / 'out/recipe-manifest.json').read_text())
    assert result['grid']['dimensions']['width'] == raw['grid']['width']


def test_unified_catalog_command_needs_no_authentication(tmp_path, capsys):
    from hpgee.cli.main import main
    path = tmp_path / 'sampling.yaml'
    path.write_text(f'auth:\n  project: example-project\nrun:\n  output_dir: {tmp_path}/out\n'
                    f'catalog:\n  enabled: true\n  path: {tmp_path}/catalog.sqlite\n')
    assert main(['catalog', 'stats', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['scenes'] == 0


@pytest.mark.parametrize('module', ['hpgee', 'geecomputation', 'geecomputation.workflow',
                                   'geecomputation.benchmark', 'geesampler.cli'])
def test_module_entrypoints_remain_executable(module):
    result = subprocess.run([sys.executable, '-m', module, '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()
