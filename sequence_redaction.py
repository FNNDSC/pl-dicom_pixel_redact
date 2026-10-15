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


ENTITY_SUFFIX = '.entities.json'


def entities_path(path):
    """Sidecar holding the OCR'd text behind each box of ``path`` (same order)."""
    path = Path(path)
    return path.with_name(path.stem + ENTITY_SUFFIX)


def select_matching_boxes(boxes, entity_texts, matcher):
    """Keep the boxes whose OCR'd text ``matcher`` accepts.

    ``entity_texts`` must line up one-to-one with ``boxes``; anything else is
    an error rather than a guess, because a misaligned list would propagate the
    wrong regions.
    """
    if not isinstance(entity_texts, list) or len(entity_texts) != len(boxes):
        raise ValueError('detected-text list does not line up with the bounding boxes')
    return [box for box, text in zip(boxes, entity_texts) if matcher.matches(text)]


def propagate_staged_files(paths, matcher=None):
    """Apply a common PHI mask to staged 2D DICOMs, including compressed input.

    With ``matcher`` (a ``header_match.HeaderMatcher``) only the boxes whose
    detected text matches the chosen header values are propagated to the other
    frames. Every frame keeps whatever Presidio already redacted on it.

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
                   str(ds.PhotometricInterpretation))
        # PlanarConfiguration is deliberately not part of the geometry: pydicom
        # returns (rows, cols, 3) for either layout and frames are written
        # interleaved, so planar and interleaved frames can be mixed safely.
        if shape is None:
            shape = current
        elif current != shape:
            raise ValueError('incompatible frame geometry or pixel encoding')
        bbox_path = Path(path).with_suffix('.json')
        if not bbox_path.is_file():
            raise ValueError('missing Presidio bounding-box sidecar')
        frame_boxes = json.loads(bbox_path.read_text())
        if matcher is not None:
            texts_path = entities_path(path)
            if not texts_path.is_file():
                raise ValueError('missing detected-text sidecar for header matching')
            frame_boxes = select_matching_boxes(
                frame_boxes, json.loads(texts_path.read_text()), matcher)
        boxes.append(frame_boxes)
        datasets.append(ds)
    mask = union_mask(boxes, shape[0], shape[1])
    if not mask.any():
        raise ValueError(
            'no PHI locations matching the selected header values were detected in '
            'sequence; cannot propagate safely' if matcher is not None else
            'no PHI locations detected in sequence; cannot propagate safely')
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
        if ds.file_meta.TransferSyntaxUID.is_compressed:
            ds.decompress()
        # pydicom decompression may change pixel representation; use the
        # *original* already-redacted decoded pixels captured above.
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.is_little_endian = True
        ds.is_implicit_VR = False
        if ds.SamplesPerPixel == 3:
            ds.PlanarConfiguration = 0              # pixel_array, and so the output, is interleaved
        ds.PixelData = masked.astype(masked.dtype.newbyteorder('<'), copy=False).tobytes()
        ds['PixelData'].VR = 'OB' if ds.BitsAllocated <= 8 else 'OW'
        ds.save_as(str(path))
        check = pydicom.dcmread(str(path))
        if check.file_meta.TransferSyntaxUID != ExplicitVRLittleEndian or not np.array_equal(check.pixel_array, masked):
            raise ValueError('post-write pixel verification failed')
    return int(mask.sum())
