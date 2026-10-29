"""Frozen transfers share transport contracts without importing orchestration."""
import subprocess
import sys


def test_product_adapter_import_does_not_load_workflow_runner():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import hpgee.transfer.products; "
            "assert 'hpgee.workflows.detection' not in sys.modules"
        )],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
