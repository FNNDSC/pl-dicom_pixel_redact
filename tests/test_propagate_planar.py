"""Planar RGB (PlanarConfiguration = 1) frames in a propagated sequence."""
import json
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (ExplicitVRLittleEndian, RLELossless, SecondaryCaptureImageStorage,
                         generate_uid)

import dicom_pixel_redact as app
import sequence_redaction as seq


def planar_rgb(path: Path, base: int, compressed=False, planar=True) -> Path:
    """16x16 8-bit RGB whose three channels hold base, base+1, base+2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.Rows = ds.Columns = 16
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 3, 'RGB'
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    planes = [np.full((16, 16), base + c, dtype=np.uint8) for c in range(3)]
    if planar and not compressed:
        ds.PlanarConfiguration = 1
        ds.PixelData = b''.join(p.tobytes() for p in planes)
    else:                       # pydicom's RLE encoder takes interleaved input and writes planar itself
        ds.PlanarConfiguration = 0
        ds.PixelData = np.stack(planes, axis=-1).tobytes()
    ds.is_little_endian, ds.is_implicit_VR = True, False
    if compressed:
        ds.compress(RLELossless)
    ds.save_as(str(path), write_like_original=False)
    return path


def stage(tmp_path, n=3, **kw):
    paths = []
    for k in range(n):
        p = planar_rgb(tmp_path / f'f{k}.dcm', 100 + k, **kw)
        p.with_suffix('.json').write_text(json.dumps([{'left': 2, 'top': 2, 'width': 4, 'height': 3}]))
        paths.append(p)
    return paths


@pytest.mark.parametrize('compressed', [False, True], ids=['native', 'rle'])
def test_planar_rgb_is_masked_and_written_interleaved(tmp_path, compressed):
    paths = stage(tmp_path, compressed=compressed)
    before = pydicom.dcmread(str(paths[0]))
    assert before.PlanarConfiguration == 1                       # the fixture really is planar
    assert seq.propagate_staged_files(paths) == 12
    for k, p in enumerate(paths):
        ds = pydicom.dcmread(str(p))
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert ds.PlanarConfiguration == 0
        px = ds.pixel_array
        assert px.shape == (16, 16, 3)
        assert (px[2:5, 2:6] == 0).all()
        assert list(px[0, 0]) == [100 + k, 101 + k, 102 + k]     # channels not shuffled


def test_planar_and_interleaved_frames_can_be_mixed(tmp_path):
    paths = stage(tmp_path, n=2)
    mixed = planar_rgb(tmp_path / 'f2.dcm', 102, planar=False)
    mixed.with_suffix('.json').write_text(json.dumps([{'left': 2, 'top': 2, 'width': 4, 'height': 3}]))
    seq.propagate_staged_files(paths + [mixed])
    assert list(pydicom.dcmread(str(mixed)).pixel_array[0, 0]) == [102, 103, 104]


# ------------------------------------------------------------ real engine, planar RGB

from test_integration import _stack_available, run_plugin   # noqa: E402


def burned_planar_rgb(path: Path, text) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGB', (800, 300), color=(20, 20, 20))
    try:
        font = ImageFont.load_default(size=32)
    except TypeError:
        font = ImageFont.load_default()
    if text:
        ImageDraw.Draw(img).text((10, 10), text, fill=(255, 255, 255), font=font)
    arr = np.array(img)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID, ds.Modality = 'DOE^JANE', 'MRN5550123', 'OT'
    ds.Rows, ds.Columns = arr.shape[:2]
    ds.SamplesPerPixel, ds.PhotometricInterpretation, ds.PlanarConfiguration = 3, 'RGB', 1
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = b''.join(arr[:, :, c].tobytes() for c in range(3))
    ds.is_little_endian, ds.is_implicit_VR = True, False
    ds.save_as(str(path), write_like_original=False)
    return path


@pytest.mark.skipif(not _stack_available(), reason='requires tesseract + en_core_web_lg + presidio')
def test_planar_rgb_series_through_the_real_plugin(tmp_path):
    d = tmp_path / 'in' / 'series'
    burned_planar_rgb(d / 'f0.dcm', 'JANE DOE')
    burned_planar_rgb(d / 'f1.dcm', 'JANE DOE')
    burned_planar_rgb(d / 'f2.dcm', None)                        # nothing for OCR to find
    original = pydicom.dcmread(str(d / 'f0.dcm'))
    assert original.PlanarConfiguration == 1
    text = (original.pixel_array[10:55, 10:300] > 100).any(axis=2)
    assert text.sum() > 200

    run_plugin(tmp_path / 'in', tmp_path / 'out', ['--propagate-match-tags', 'PatientName'])

    for k in range(3):
        out = pydicom.dcmread(str(tmp_path / 'out' / 'series' / f'f{k}.dcm'))
        assert out.PlanarConfiguration == 0
        px = out.pixel_array[10:55, 10:300]
        assert px.shape == (45, 290, 3)
        assert (px[text] == 0).all(), f'frame {k}: patient name still visible'
