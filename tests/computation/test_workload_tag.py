import pytest
from geecomputation.config import Config
from .test_contract import configuration


def test_workload_tag_is_explicit_and_validated():
    raw=configuration()
    assert Config.from_dict(raw).workload_tag is None
    raw['workload_tag']='gfp-s1s2-sampling'
    assert Config.from_dict(raw).workload_tag==raw['workload_tag']
    raw['workload_tag']='Bad / Tag'
    with pytest.raises(ValueError,match='workload_tag'):
        Config.from_dict(raw)
