import pytest

import self_change


def test_busy_slot_is_released_after_exception():
    with pytest.raises(ValueError), self_change.busy_slot():
        raise ValueError("boom")
    assert not self_change.is_busy()
