import numpy as np

import pytest


@pytest.mark.parametrize("size", [1E9, 10E9, 100E9, 200E9])
def test_memory_limit(size):
    arr = np.ones(int(size), dtype="uint8")
    assert np.mean(arr) == 1
