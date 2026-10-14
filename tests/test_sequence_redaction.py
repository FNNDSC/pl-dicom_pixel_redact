import numpy as np
import pytest
from sequence_redaction import apply_mask, union_mask


def test_missed_middle_frame_is_covered():
    detections = [
        [{'left': 3, 'top': 4, 'width': 5, 'height': 2}],
        [],
        [{'left': 4, 'top': 4, 'width': 5, 'height': 2}],
    ]
    mask = union_mask(detections, 16, 16)
    middle = np.full((16, 16), 1234, dtype=np.uint16)
    result = apply_mask(middle, mask)
    assert np.all(result[4:6, 3:9] == 0)
    assert result[0, 0] == 1234
    assert result.dtype == middle.dtype
    assert middle[4, 3] == 1234


def test_rgb_channel_mask():
    mask = union_mask([[{'left': 1, 'top': 1, 'width': 2, 'height': 2}]], 5, 5)
    pixels = np.ones((5, 5, 3), dtype=np.uint8) * 255
    result = apply_mask(pixels, mask)
    assert np.all(result[1:3, 1:3, :] == 0)
    assert np.all(result[0, :, :] == 255)


@pytest.mark.parametrize('bad', [
    {'left': -1, 'top': 0, 'width': 1, 'height': 1},
    {'left': 3, 'top': 0, 'width': 2, 'height': 1},
    {'left': 0, 'top': 0, 'width': 0, 'height': 1},
    {'left': 0.5, 'top': 0, 'width': 1, 'height': 1},
    {'left': 0, 'top': 0, 'height': 1},
])
def test_bad_boxes_fail_closed(bad):
    with pytest.raises(ValueError):
        union_mask([[bad]], 4, 4)


def test_geometry_mismatch():
    with pytest.raises(ValueError):
        apply_mask(np.zeros((4, 5), dtype=np.uint8), np.zeros((5, 5), dtype=bool))
