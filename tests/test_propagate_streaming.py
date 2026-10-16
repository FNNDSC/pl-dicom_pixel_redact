"""Propagation must not hold the whole series in memory, and must validate every
frame before it modifies any of them."""
import json
import tracemalloc
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.uid import ExplicitVRLittleEndian

import sequence_redaction as seq
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import RLELossless, SecondaryCaptureImageStorage, generate_uid

def make_dcm(path: Path, fill: int, rows=128, cols=128, compressed=False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.full((rows, cols), fill, dtype=np.uint16).tobytes()
    ds.is_little_endian, ds.is_implicit_VR = True, False
    if compressed:
        ds.compress(RLELossless)
    ds.save_as(str(path), write_like_original=False)
    return path


BOX = {'left': 2, 'top': 2, 'width': 4, 'height': 3}


def stage(d: Path, n: int, **kw):
    paths = []
    for k in range(n):
        p = make_dcm(d / f'f{k:03d}.dcm', 1000 + k, **kw)
        p.with_suffix('.json').write_text(json.dumps([BOX]))
        paths.append(p)
    return paths


def peak_bytes(fn):
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class TestMemoryDoesNotGrowWithSeriesLength:

    def test_peak_memory_is_flat(self, tmp_path):
        small = stage(tmp_path / 'small', 20)
        large = stage(tmp_path / 'large', 140)
        peak_small = peak_bytes(lambda: seq.propagate_staged_files(small))
        peak_large = peak_bytes(lambda: seq.propagate_staged_files(large))
        frame = 128 * 128 * 2
        # 7x more frames must cost (almost) nothing extra; holding every frame would
        # add >= 120 frames' worth of decoded pixels plus their raw bytes
        assert peak_large < peak_small + 6 * frame, (peak_small, peak_large)

    def test_results_are_still_correct(self, tmp_path):
        paths = stage(tmp_path, 25)
        assert seq.propagate_staged_files(paths) == 12
        for k, p in enumerate(paths):
            px = pydicom.dcmread(str(p)).pixel_array
            assert (px[2:5, 2:6] == 0).all() and px[0, 0] == 1000 + k


class TestValidatesBeforeModifying:

    def snapshot(self, paths):
        return [p.read_bytes() for p in paths]

    def test_unsupported_pixel_format_in_a_late_frame_modifies_nothing(self, tmp_path):
        paths = stage(tmp_path, 4)
        bad = pydicom.dcmread(str(paths[3]))
        bad.PhotometricInterpretation = 'YBR_FULL'          # not supported for propagation
        bad.save_as(str(paths[3]))
        before = self.snapshot(paths)
        with pytest.raises(ValueError, match='unsupported pixel representation'):
            seq.propagate_staged_files(paths)
        assert self.snapshot(paths) == before

    def test_unsupported_bit_depth_in_a_late_frame_modifies_nothing(self, tmp_path):
        paths = stage(tmp_path, 3)
        bad = pydicom.dcmread(str(paths[2]))
        bad.BitsAllocated = 32
        bad.save_as(str(paths[2]))
        before = self.snapshot(paths)
        with pytest.raises(ValueError, match='unsupported pixel representation'):
            seq.propagate_staged_files(paths)
        assert self.snapshot(paths) == before

    def test_multiframe_in_a_late_frame_modifies_nothing(self, tmp_path):
        paths = stage(tmp_path, 3)
        bad = pydicom.dcmread(str(paths[2]))
        bad.NumberOfFrames = 2
        bad.save_as(str(paths[2]))
        before = self.snapshot(paths)
        with pytest.raises(ValueError, match='single-frame'):
            seq.propagate_staged_files(paths)
        assert self.snapshot(paths) == before

    def test_geometry_mismatch_modifies_nothing(self, tmp_path):
        paths = stage(tmp_path, 3)
        make_dcm(paths[2], 5, rows=8, cols=8)
        paths[2].with_suffix('.json').write_text(json.dumps([BOX]))
        before = self.snapshot(paths)
        with pytest.raises(ValueError, match='incompatible'):
            seq.propagate_staged_files(paths)
        assert self.snapshot(paths) == before

    def test_empty_mask_modifies_nothing(self, tmp_path):
        paths = stage(tmp_path, 3)
        for p in paths:
            p.with_suffix('.json').write_text('[]')
        before = self.snapshot(paths)
        with pytest.raises(ValueError, match='cannot propagate safely'):
            seq.propagate_staged_files(paths)
        assert self.snapshot(paths) == before


class TestCompressedInput:

    def test_rle_frames_come_out_native_and_correct(self, tmp_path):
        paths = stage(tmp_path, 4, compressed=True)
        assert pydicom.dcmread(str(paths[0])).file_meta.TransferSyntaxUID.is_compressed
        seq.propagate_staged_files(paths)
        for k, p in enumerate(paths):
            ds = pydicom.dcmread(str(p))
            assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
            assert not ds['PixelData'].is_undefined_length
            assert ds.pixel_array[0, 0] == 1000 + k and (ds.pixel_array[2:5, 2:6] == 0).all()
