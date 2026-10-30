import numpy as np
from .test_contract import task


def test_cloud_rejections_are_not_counted_as_accepted_land_pixels():
    from hpgee.acquisition.manifest import freeze_unfiltered, requests_for_task
    from hpgee.acquisition.delivery import validate_array

    t = task()
    t["grid"]["dimensions"]["width"] = 256
    r = requests_for_task(freeze_unfiltered(t), 512)[0]
    x = np.zeros((8, 128, 256), dtype="int16")
    x[7] = 127
    x[6, :, 128:] = 2
    accepted = validate_array(x, r)
    assert accepted["valid_pixels"] == 32768
    assert accepted["quality_valid_pixels"] == 16384 and accepted["quality_clear_pixels"] == 16384
    assert accepted["quality_rejected_cells"] == 1
