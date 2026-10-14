"""Sequence-aware propagation of Presidio DICOM redaction bounding boxes.

Only call this for an explicitly designated directory of aligned 2-D frames.
Coordinates follow Presidio's {left, top, width, height} convention.
"""
import json
from pathlib import Path

import numpy as np


def union_mask(box_lists, rows, columns):
    """Return a boolean mask shared by all frames; reject malformed boxes."""
    mask = np.zeros((rows, columns), dtype=bool)
    for boxes in box_lists:
        if not isinstance(boxes, list):
            raise ValueError('invalid bounding-box list')
        for box in boxes:
            if not isinstance(box, dict) or set(box) < {'left', 'top', 'width', 'height'}:
                raise ValueError('invalid bounding box')
            values = [box[k] for k in ('left', 'top', 'width', 'height')]
            if any(type(v) is not int for v in values):
                raise ValueError('noninteger bounding box')
            x, y, w, h = values
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > columns or y + h > rows:
                raise ValueError('bounding box outside frame')
            mask[y:y+h, x:x+w] = True
    return mask


def apply_mask(array, mask, fill_value=0):
    """Apply a 2-D mask to a single grayscale or RGB frame without changing dtype."""
    if array.ndim not in (2, 3) or array.shape[:2] != mask.shape:
        raise ValueError('frame dimensions do not match mask')
    result = array.copy()
    result[mask] = fill_value
    return result


def propagate_staged_files(paths):
    """Apply a common PHI mask to staged 2D DICOMs, including compressed input.

    Output is always Explicit VR Little Endian, avoiding unsafe recompression.
    The caller is responsible for withholding all output if any frame fails.
    """
    import pydicom
    from pydicom.uid import ExplicitVRLittleEndian

    if not paths:
        raise ValueError('empty frame sequence')
    datasets = []
    boxes = []
    shape = None
    for path in paths:
        ds = pydicom.dcmread(str(path))
        syntax = ds.file_meta.TransferSyntaxUID
        if int(ds.get('NumberOfFrames', 1) or 1) != 1:
            raise ValueError('propagation requires single-frame DICOM')
        current = (int(ds.Rows), int(ds.Columns), int(ds.SamplesPerPixel),
                   int(ds.BitsAllocated), int(ds.PixelRepresentation),
                   str(ds.PhotometricInterpretation), int(ds.get('PlanarConfiguration', 0)))
        if shape is None:
            shape = current
        elif current != shape:
            raise ValueError('incompatible frame geometry or pixel encoding')
        bbox_path = Path(path).with_suffix('.json')
        if not bbox_path.is_file():
            raise ValueError('missing Presidio bounding-box sidecar')
        boxes.append(json.loads(bbox_path.read_text()))
        datasets.append(ds)
    mask = union_mask(boxes, shape[0], shape[1])
    if not mask.any():
        raise ValueError('no PHI locations detected in sequence; cannot propagate safely')
    # Modify *already-redacted* frames; existing Presidio redactions are preserved.
    for ds, path in zip(datasets, paths):
        array = ds.pixel_array
        masked = apply_mask(array, mask)
        # Decode already-redacted pixels, then write native uncompressed bytes.
        # This avoids invoking GDCM RLE compression a second time.
        # Only support 8/16-bit integer pixels and standard monochrome/RGB.
        if ds.BitsAllocated not in (8, 16) or ds.PhotometricInterpretation not in (
            'MONOCHROME1', 'MONOCHROME2', 'RGB'
        ):
            raise ValueError('unsupported pixel representation for propagation')
        if ds.SamplesPerPixel == 3 and ds.get('PlanarConfiguration', 0) != 0:
            raise ValueError('planar RGB requires explicit layout conversion')
        if ds.file_meta.TransferSyntaxUID.is_compressed:
            ds.decompress()
        # pydicom decompression may change pixel representation; use the
        # *original* already-redacted decoded pixels captured above.
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.is_little_endian = True
        ds.is_implicit_VR = False
        ds.PixelData = masked.astype(masked.dtype.newbyteorder('<'), copy=False).tobytes()
        ds['PixelData'].VR = 'OB' if ds.BitsAllocated <= 8 else 'OW'
        ds.save_as(str(path))
        check = pydicom.dcmread(str(path))
        if check.file_meta.TransferSyntaxUID != ExplicitVRLittleEndian or not np.array_equal(check.pixel_array, masked):
            raise ValueError('post-write pixel verification failed')
    return int(mask.sum())
