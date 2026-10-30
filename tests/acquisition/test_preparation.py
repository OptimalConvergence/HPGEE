from types import SimpleNamespace
import pytest


def test_metadata_only_preparation_never_requests_qa_pixels(tmp_path):
    from hpgee.acquisition.prepare import prepare_tasks
    from .test_contract import task

    ee = SimpleNamespace(
        data=SimpleNamespace(
            computePixels=lambda *a, **k: pytest.fail("metadata-only path called computePixels")
        )
    )
    result = prepare_tasks(
        ee, [task()], tmp_path / "target.gz", tmp_path / "cache", selection="post", project="test"
    )
    assert result["tasks"] == 1 and result["required_cells"] == 1


def test_preparation_rejects_s1_non_30m_grid_before_network(tmp_path):
    from hpgee.acquisition.prepare import prepare_tasks
    from .test_contract import task

    t = task()
    t["sensor"] = "S1"
    t["grid"]["affineTransform"]["scaleX"] = 10
    with pytest.raises(ValueError, match="30"):
        prepare_tasks(
            None, [t], tmp_path / "target.gz", tmp_path / "cache", selection="post", project="test"
        )
    assert not (tmp_path / "target.gz").exists()
