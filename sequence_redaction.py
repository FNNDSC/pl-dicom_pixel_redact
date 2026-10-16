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


SUPPORTED_BITS = (8, 16)
SUPPORTED_PHOTOMETRIC = ('MONOCHROME1', 'MONOCHROME2', 'RGB')


def _validated_geometry(path):
    """Read one staged file's header only (no pixel data) and return its geometry.

    Raises for anything propagation cannot handle, so the whole series can be
    checked before any frame is modified.
    """
    import pydicom
    ds = pydicom.dcmread(str(path), stop_before_pixels=True)
    if int(ds.get('NumberOfFrames', 1) or 1) != 1:
        raise ValueError('propagation requires single-frame DICOM')
    if ds.BitsAllocated not in SUPPORTED_BITS or ds.PhotometricInterpretation not in SUPPORTED_PHOTOMETRIC:
        raise ValueError('unsupported pixel representation for propagation')
    # PlanarConfiguration is deliberately not part of the geometry: pydicom
    # returns (rows, cols, 3) for either layout and frames are written
    # interleaved, so planar and interleaved frames can be mixed safely.
    return (int(ds.Rows), int(ds.Columns), int(ds.SamplesPerPixel),
            int(ds.BitsAllocated), int(ds.PixelRepresentation),
            str(ds.PhotometricInterpretation))


def propagate_staged_files(paths, matcher=None):
    """Apply a common PHI mask to staged 2D DICOMs, including compressed input.

    With ``matcher`` (a ``header_match.HeaderMatcher``) only the boxes whose
    detected text matches the chosen header values are propagated to the other
    frames. Every frame keeps whatever Presidio already redacted on it.

    Output is always Explicit VR Little Endian, avoiding unsafe recompression.
    The caller is responsible for withholding all output if any frame fails.

    Two passes, so memory does not grow with the length of the series: the first
    reads only headers and sidecars (validating every frame and building the
    common mask before anything is modified); the second loads, masks, writes
    and verifies one frame at a time.
    """
    import pydicom
    from pydicom.uid import ExplicitVRLittleEndian

    if not paths:
        raise ValueError('empty frame sequence')

    # ---- pass 1: headers and sidecars only
    boxes = []
    shape = None
    for path in paths:
        current = _validated_geometry(path)
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
    mask = union_mask(boxes, shape[0], shape[1])
    del boxes
    if not mask.any():
        raise ValueError(
            'no PHI locations matching the selected header values were detected in '
            'sequence; cannot propagate safely' if matcher is not None else
            'no PHI locations detected in sequence; cannot propagate safely')

    # ---- pass 2: one frame in memory at a time
    # Modify *already-redacted* frames; existing Presidio redactions are preserved.
    for path in paths:
        ds = pydicom.dcmread(str(path))
        masked = apply_mask(ds.pixel_array, mask)        # decodes compressed input once
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.is_little_endian = True
        ds.is_implicit_VR = False
        if ds.SamplesPerPixel == 3:
            ds.PlanarConfiguration = 0              # pixel_array, and so the output, is interleaved
        # A fresh element (rather than assigning .PixelData) drops the "undefined
        # length" flag an encapsulated element carries, and avoids decompressing
        # the dataset a second time.
        ds.add_new(0x7FE00010, 'OB' if ds.BitsAllocated <= 8 else 'OW',
                   masked.astype(masked.dtype.newbyteorder('<'), copy=False).tobytes())
        ds.save_as(str(path))
        del ds
        check = pydicom.dcmread(str(path))
        if check.file_meta.TransferSyntaxUID != ExplicitVRLittleEndian or not np.array_equal(check.pixel_array, masked):
            raise ValueError('post-write pixel verification failed')
        del check, masked
    return int(mask.sum())
