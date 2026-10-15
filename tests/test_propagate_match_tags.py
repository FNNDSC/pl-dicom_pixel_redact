"""Tests for --propagate-match-tags: only text matching selected DICOM header
values is propagated across the frames of a sequence."""
import json
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

import dicom_pixel_redact as app
import sequence_redaction as seq
from header_match import (HeaderMatcher, date_display_forms, header_values, normalize,
                          validate_keywords)


def ds_with(**kw) -> Dataset:
    ds = Dataset()
    for k, v in kw.items():
        setattr(ds, k, v)
    return ds


# --------------------------------------------------------------------------- matcher

class TestHeaderValues:

    def test_person_name_yields_whole_and_parts(self):
        vals = header_values([ds_with(PatientName='DOE^JANE^Q')], ['PatientName'])
        assert {'doejaneq', 'doe', 'jane'} <= vals

    def test_other_tags_are_ignored(self):
        vals = header_values([ds_with(PatientName='DOE^JANE', PatientID='MRN5550123')], ['PatientID'])
        assert 'mrn5550123' in vals and 'jane' not in vals

    def test_missing_and_empty_tags_give_nothing(self):
        assert header_values([ds_with(PatientName='')], ['PatientName', 'AccessionNumber']) == set()

    def test_date_values_include_burned_in_renderings(self):
        vals = header_values([ds_with(PatientBirthDate='19620314')], ['PatientBirthDate'])
        for shown in ('03/14/1962', '14MAR1962', '14-03-62', 'March 14, 1962', '1962-03-14'):
            assert normalize(shown) in vals

    def test_values_from_every_dataset_are_combined(self):
        vals = header_values([ds_with(PatientID='AAA111'), ds_with(PatientID='BBB222')], ['PatientID'])
        assert {'aaa111', 'bbb222'} <= vals

    def test_min_len_drops_short_parts(self):
        assert 'q' not in header_values([ds_with(PatientName='DOE^JANE^Q')], ['PatientName'], min_len=2)

    def test_multivalued_elements(self):
        vals = header_values([ds_with(OtherPatientIDs=['OLD111', 'OLD222'])], ['OtherPatientIDs'])
        assert {'old111', 'old222'} <= vals

    def test_validate_keywords(self):
        assert validate_keywords(['PatientID', 'NotAKeyword', 'StudyDate']) == ['NotAKeyword']

    def test_date_forms_reject_non_dates(self):
        assert date_display_forms('ABC') == [] and date_display_forms('20261345') == []


class TestHeaderMatcher:

    @pytest.fixture
    def matcher(self):
        return HeaderMatcher(header_values(
            [ds_with(PatientName='DOE^JANE', PatientID='MRN5550123', PatientBirthDate='19620314',
                     AccessionNumber='ACC-77821', InstitutionName='General Hospital')],
            ['PatientName', 'PatientID', 'PatientBirthDate', 'AccessionNumber', 'InstitutionName']))

    @pytest.mark.parametrize('text', [
        'JANE DOE', 'Doe', 'jane', 'MRN5550123', 'ID: MRN5550123', 'ACC-77821', 'acc 77821',
        '14 MAR 1962', '03/14/1962', 'General Hospital', 'HOSPITAL',
    ])
    def test_matches(self, matcher, text):
        assert matcher.matches(text)

    @pytest.mark.parametrize('text', [
        '', '   ', 'Michael Johnson', 'LEFT VENTRICLE', '12345', '03/15/1962', 'MRN0000000',
    ])
    def test_does_not_match(self, matcher, text):
        assert not matcher.matches(text)

    def test_empty_matcher_is_falsy(self):
        assert not HeaderMatcher(set())


# ----------------------------------------------------------------- selecting boxes

def box(x):
    return {'left': x, 'top': 2, 'width': 4, 'height': 3}


class TestSelectMatchingBoxes:

    def test_keeps_only_matching_boxes_in_order(self):
        m = HeaderMatcher({'doe', 'jane'})
        boxes = [box(0), box(5), box(10)]
        assert seq.select_matching_boxes(boxes, ['JANE', 'Kowalski', 'DOE'], m) == [box(0), box(10)]

    @pytest.mark.parametrize('texts', [[], ['A'], ['A', 'B', 'C'], None, 'AB'])
    def test_misaligned_text_list_is_an_error(self, texts):
        with pytest.raises(ValueError, match='line up'):
            seq.select_matching_boxes([box(0), box(5)], texts, HeaderMatcher({'a'}))


def make_dcm(path: Path, fill: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.Rows = ds.Columns = 16
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.full((16, 16), fill, dtype=np.uint16).tobytes()
    ds.is_little_endian, ds.is_implicit_VR = True, False
    ds.save_as(str(path), write_like_original=False)
    return path


def stage(tmp_path, per_frame):
    """per_frame: list of [(box, text), ...]; returns staged paths."""
    paths = []
    for k, items in enumerate(per_frame):
        p = make_dcm(tmp_path / f'f{k}.dcm', 1000 + k)
        p.with_suffix('.json').write_text(json.dumps([b for b, _ in items]))
        seq.entities_path(p).write_text(json.dumps([t for _, t in items]))
        paths.append(p)
    return paths


class TestPropagateWithMatcher:
    HEADER = box(2)                                   # x 2..5, y 2..4
    OTHER = {'left': 8, 'top': 8, 'width': 4, 'height': 3}

    def per_frame(self):
        return [[(self.HEADER, 'JANE DOE'), (self.OTHER, 'Michael Johnson')], [], []]

    def test_only_matching_box_reaches_other_frames(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        seq.propagate_staged_files(paths, HeaderMatcher({'doe', 'jane'}))
        for p in paths:
            px = pydicom.dcmread(str(p)).pixel_array
            assert (px[2:5, 2:6] == 0).all()                       # header text: everywhere
            assert (px[8:11, 8:12] != 0).all()                     # unrelated text: not propagated

    def test_without_matcher_everything_is_propagated(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        seq.propagate_staged_files(paths)
        for p in paths[1:]:
            px = pydicom.dcmread(str(p)).pixel_array
            assert (px[8:11, 8:12] == 0).all()

    def test_nothing_matching_fails_and_leaves_files_unchanged(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        before = [p.read_bytes() for p in paths]
        with pytest.raises(ValueError, match='matching the selected header values'):
            seq.propagate_staged_files(paths, HeaderMatcher({'zzzzzz'}))
        assert [p.read_bytes() for p in paths] == before

    def test_missing_text_sidecar_fails_closed(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        seq.entities_path(paths[1]).unlink()
        with pytest.raises(ValueError, match='missing detected-text sidecar'):
            seq.propagate_staged_files(paths, HeaderMatcher({'doe'}))

    def test_misaligned_text_sidecar_fails_closed(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        seq.entities_path(paths[0]).write_text(json.dumps(['JANE DOE']))      # 1 text, 2 boxes
        with pytest.raises(ValueError, match='line up'):
            seq.propagate_staged_files(paths, HeaderMatcher({'doe'}))

    def test_text_sidecar_is_not_needed_without_matcher(self, tmp_path):
        paths = stage(tmp_path, self.per_frame())
        for p in paths:
            seq.entities_path(p).unlink()
        seq.propagate_staged_files(paths)


# ------------------------------------------------------------------------ CLI options

class TestCli:

    def run(self, tmp_path, *args):
        (tmp_path / 'in').mkdir(exist_ok=True)
        (tmp_path / 'out').mkdir(exist_ok=True)
        options = app.parser.parse_args(list(args))
        app.main(options, tmp_path / 'in', tmp_path / 'out')

    def test_default_is_off(self):
        assert app.parser.parse_args([]).propagate_match_tags is None

    def test_implies_propagate_bboxes(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(app, 'DicomImageRedactorEngine', lambda *a, **k: object())
        monkeypatch.setattr(app, 'EntityTextCapture', lambda engine: seen.setdefault('capture', True))
        options = app.parser.parse_args(['--propagate-match-tags', 'PatientID'])
        assert options.propagate_bboxes is False                  # not set by the user ...
        (tmp_path / 'in').mkdir(); (tmp_path / 'out').mkdir()
        app.main(options, tmp_path / 'in', tmp_path / 'out')
        assert options.propagate_bboxes is True and seen        # ... but switched on by the tag list

    def test_unknown_keyword_is_rejected(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as e:
            self.run(tmp_path, '--propagate-bboxes', '--propagate-match-tags', 'PatientID,Bogus')
        assert e.value.code == 2 and 'Bogus' in capsys.readouterr().err

    def test_empty_list_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            self.run(tmp_path, '--propagate-bboxes', '--propagate-match-tags', ' , ')
        assert e.value.code == 2


# ------------------------------------------------------------ real engine end to end

from test_integration import _stack_available, run_plugin   # noqa: E402

LINE1, LINE2 = 'JANE DOE', 'Michael Johnson'
LINE1_BOX = (slice(10, 55), slice(10, 300))       # rows, cols where LINE1 is drawn
LINE2_BOX = (slice(150, 195), slice(10, 450))


def burned_frame(path: Path, lines) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('L', (800, 300), color=20)
    try:
        font = ImageFont.load_default(size=32)
    except TypeError:
        font = ImageFont.load_default()
    for (x, y), text in lines:
        ImageDraw.Draw(img).text((x, y), text, fill=255, font=font)
    arr = np.array(img)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID, ds.Modality = 'DOE^JANE', 'MRN5550123', 'OT'
    ds.Rows, ds.Columns = arr.shape
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = arr.tobytes()
    ds.is_little_endian, ds.is_implicit_VR = True, False
    ds.save_as(str(path), write_like_original=False)
    return path


@pytest.mark.skipif(not _stack_available(), reason='requires tesseract + en_core_web_lg + presidio')
class TestRealEngine:
    """Frame 0 shows the patient's name and an unrelated person's name; frame 1
    shows only the patient's name; frame 2 is blank (e.g. OCR would have missed
    it). The patient's name must reach every frame; the other name must not."""

    def series(self, tmp_path):
        d = tmp_path / 'in' / 'series'
        burned_frame(d / 'f0.dcm', [((10, 10), LINE1), ((10, 150), LINE2)])
        burned_frame(d / 'f1.dcm', [((10, 10), LINE1)])
        burned_frame(d / 'f2.dcm', [])
        return tmp_path / 'in', tmp_path / 'out'

    def out(self, outdir, k):
        return pydicom.dcmread(str(outdir / 'series' / f'f{k}.dcm')).pixel_array

    def text_pixels(self, indir, region):
        """Where the text was drawn in frame 0 of the INPUT (bright pixels)."""
        original = pydicom.dcmread(str(indir / 'series' / 'f0.dcm')).pixel_array
        mask = np.zeros(original.shape, dtype=bool)
        mask[region] = original[region] > 100
        assert mask.sum() > 200, 'test image has no text to check'
        return mask

    def test_control_without_option_everything_is_propagated(self, tmp_path):
        indir, outdir = self.series(tmp_path)
        run_plugin(indir, outdir, ['--propagate-bboxes'])
        # positive control: the unrelated name WAS detected, and spread to the blank frame
        blank = self.out(outdir, 2)
        assert (blank[self.text_pixels(indir, LINE2_BOX)] == 0).all()
        assert (blank[self.text_pixels(indir, LINE1_BOX)] == 0).all()

    def test_only_header_matching_text_is_propagated(self, tmp_path):
        indir, outdir = self.series(tmp_path)
        run_plugin(indir, outdir, ['--propagate-match-tags', 'PatientName,PatientID'])
        blank = self.out(outdir, 2)
        line1, line2 = self.text_pixels(indir, LINE1_BOX), self.text_pixels(indir, LINE2_BOX)
        assert (blank[line1] == 0).all(), 'patient name must be masked on every frame'
        assert (blank[line2] == 20).all(), 'unrelated text must not be propagated'
        assert (self.out(outdir, 1)[line2] == 20).all()
        # frame 0 still has BOTH redacted by Presidio itself
        first = self.out(outdir, 0)
        original = pydicom.dcmread(str(indir / 'series' / 'f0.dcm')).pixel_array
        for line in (line1, line2):
            assert (first[line] != original[line]).mean() > 0.99

    def test_no_header_match_fails_and_writes_nothing(self, tmp_path):
        indir, outdir = self.series(tmp_path)
        with pytest.raises(SystemExit) as e:
            run_plugin(indir, outdir, ['--propagate-match-tags', 'AccessionNumber'])
        assert e.value.code == 1
        assert not list(outdir.rglob('*.dcm'))
