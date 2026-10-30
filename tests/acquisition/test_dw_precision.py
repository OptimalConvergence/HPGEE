import numpy as np
from .test_contract import task


def test_native_dynamic_world_probability_mass_error_is_reported_without_renormalizing():
    from hpgee.acquisition.manifest import freeze_unfiltered, requests_for_task
    from hpgee.acquisition.delivery import validate_array

    t = task()
    t["sensor"] = "DW"
    r = requests_for_task(freeze_unfiltered(t), 128)[0]
    vector = np.array(
        [
            0.04828646779060364,
            0.023939553648233414,
            0.02503305673599243,
            0.038679298013448715,
            0.04379573091864586,
            0.025791959837079048,
            0.049745824187994,
            0.08110536634922028,
            0.6633338928222656,
        ],
        dtype="float32",
    )
    values = np.zeros((11, 128, 128), dtype="float32")
    values[:9] = vector[:, None, None]
    values[9] = 8
    values[10] = 1
    before = values.copy()
    result = validate_array(values, r)
    assert np.array_equal(before, values)
    assert 0.0002 < result["probability_mass_max_error"] < 0.0004
