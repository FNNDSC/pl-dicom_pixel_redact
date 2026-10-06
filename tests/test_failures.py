"""Failure-scenario tests for main().

The Presidio engine is replaced with a fake that mimics the *real* engine's
behaviour: ``redact_from_file`` first copies the input into ``output_dir`` and
then redacts that copy in place, so a failure after the copy leaves the
unredacted original in ``output_dir``. Our plugin must therefore never hand
the real output directory to the engine. These tests run without Tesseract or
spaCy and exercise only the plugin's own control flow.
"""
import json
import sys
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, RLELossless,
                         SecondaryCaptureImageStorage, generate_uid)

import dicom_pixel_redact as app


class FakeEngine:
    """Mimics DicomImageRedactorEngine's copy-then-redact-in-place behaviour.

    - names starting with 'bad' raise AFTER the copy (the real failure mode)
    - names starting with 'crash' raise a BaseException AFTER the copy
    - everything else gets b'REDACTED' appended to the copy
    """
    calls: list = []

    def redact_from_file(self, input_path, output_dir, **kwargs):
        src = Path(input_path)
        icon = 'IconImageSequence' in pydicom.dcmread(
            str(src), stop_before_pixels=True, force=True)
        FakeEngine.calls.append(
            {'name': src.name, 'output_dir': Path(output_dir), 'icon': icon, **kwargs})
        dst = Path(output_dir) / src.name
        dst.write_bytes(src.read_bytes())            # real engine copies first
        if src.name.startswith('bad'):
            raise ValueError('cannot parse DICOM')
        if src.name.startswith('crash'):
            raise KeyboardInterrupt()
        dst.write_bytes(dst.read_bytes() + b'REDACTED')
        if kwargs.get('save_bboxes'):
            dst.with_suffix('.json').write_text(json.dumps([{'top': 1}]))


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    FakeEngine.calls = []
    monkeypatch.setattr(app, 'DicomImageRedactorEngine', FakeEngine)


@pytest.fixture
def dirs(tmp_path):
    i, o = tmp_path / 'in', tmp_path / 'out'
    i.mkdir()
    o.mkdir()
    return i, o


def run(inputdir, outputdir, *argv):
    app.main(app.parser.parse_args(list(argv)), inputdir, outputdir)


def write_dcm(path: Path, name='DOE^JANE', pid='MRN1', frames=None,
              transfer_syntax=ExplicitVRLittleEndian, icon=False,
              other_ids=None) -> Path:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = transfer_syntax
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\x00' * 128)
    ds.PatientName, ds.PatientID = name, pid
    if other_ids:
        ds.OtherPatientIDs = other_ids
    if frames:
        ds.NumberOfFrames = str(frames)
    if icon:
        ds.IconImageSequence = Sequence([Dataset()])
    ds.is_little_endian, ds.is_implicit_VR = True, False
    ds.save_as(str(path), write_like_original=False)
    return path


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())


class TestFailClosed:
    """Nothing un-redacted may ever reach the output directory."""

    def test_engine_failure_exits_nonzero(self, dirs):
        i, o = dirs
        write_dcm(i / 'bad.dcm')
        with pytest.raises(SystemExit) as exc:
            run(i, o)
        assert exc.value.code == 1

    def test_failed_file_leaves_no_copy_in_output(self, dirs):
        # the real engine copies the input to output_dir BEFORE failing
        i, o = dirs
        write_dcm(i / 'bad.dcm')
        with pytest.raises(SystemExit):
            run(i, o)
        assert tree(o) == []

    def test_engine_never_receives_the_real_output_dir(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o)
        assert FakeEngine.calls[0]['output_dir'] != o

    def test_hard_crash_in_engine_leaves_no_copy(self, dirs):
        # BaseException (stand-in for an abort/Ctrl-C) skips `except Exception`
        i, o = dirs
        write_dcm(i / 'crash.dcm')
        with pytest.raises(KeyboardInterrupt):
            run(i, o)
        assert tree(o) == []

    def test_one_bad_file_does_not_stop_the_others(self, dirs):
        i, o = dirs
        for n in ('a.dcm', 'bad.dcm', 'c.dcm'):
            write_dcm(i / n)
        with pytest.raises(SystemExit):
            run(i, o)
        assert tree(o) == ['a.dcm', 'c.dcm']

    def test_successful_output_is_the_redacted_copy(self, dirs):
        i, o = dirs
        src = write_dcm(i / 'a.dcm')
        original = src.read_bytes()
        run(i, o)
        assert (o / 'a.dcm').read_bytes() == original + b'REDACTED'
        assert src.read_bytes() == original            # input untouched

    def test_no_partial_files_left_behind(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o)
        assert not list(o.glob('.*'))

    def test_bbox_json_is_published_next_to_output(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o, '--save-bboxes')
        assert tree(o) == ['a.dcm', 'a.json']

    def test_nested_input_structure_is_preserved(self, dirs):
        i, o = dirs
        (i / 'sub').mkdir()
        write_dcm(i / 'sub' / 'a.dcm')
        run(i, o)
        assert tree(o) == ['sub/a.dcm']

    def test_non_dicom_matching_file_fails_and_is_not_output(self, dirs):
        i, o = dirs
        (i / 'bad.dcm').write_bytes(b'not a dicom')
        with pytest.raises(SystemExit):
            run(i, o)
        assert tree(o) == []

    def test_no_matching_files_exits_cleanly(self, dirs, caplog):
        i, o = dirs
        (i / 'notes.txt').write_text('x')
        run(i, o)
        assert tree(o) == []
        assert 'No files matched' in caplog.text


class TestUnsupportedInput:
    """Multi-frame input is refused; compression alone is not refused."""

    def test_multi_frame_is_refused(self, dirs, caplog):
        i, o = dirs
        write_dcm(i / 'cine.dcm', frames=30)
        with pytest.raises(SystemExit) as exc:
            run(i, o)
        assert exc.value.code == 1
        assert FakeEngine.calls == []
        assert tree(o) == []
        assert 'multi-frame' in caplog.text

    def test_single_frame_with_number_of_frames_1_is_accepted(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm', frames=1)
        run(i, o)
        assert tree(o) == ['a.dcm']

    def test_compressed_transfer_syntax_is_accepted(self, dirs):
        # Header-only fixture tests routing to the fake engine, not codec support.
        i, o = dirs
        write_dcm(i / 'rle.dcm', transfer_syntax=RLELossless)
        run(i, o)
        assert len(FakeEngine.calls) == 1
        assert tree(o) == ['rle.dcm']

    def test_unsupported_file_does_not_block_supported_ones(self, dirs):
        i, o = dirs
        write_dcm(i / 'cine.dcm', frames=30)
        write_dcm(i / 'ok.dcm')
        with pytest.raises(SystemExit):
            run(i, o)
        assert tree(o) == ['ok.dcm']


class TestIconImage:

    def test_icon_sequence_is_removed_from_the_working_copy_only(self, dirs):
        i, o = dirs
        src = write_dcm(i / 'a.dcm', icon=True)
        run(i, o)
        assert FakeEngine.calls[0]['icon'] is False
        assert 'IconImageSequence' in pydicom.dcmread(str(src))   # input unchanged

    def test_files_without_icon_are_copied_as_is(self, dirs):
        i, o = dirs
        src = write_dcm(i / 'a.dcm')
        run(i, o)
        assert (o / 'a.dcm').read_bytes() == src.read_bytes() + b'REDACTED'


class TestCopyOthers:

    def test_copies_non_dicom_files(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        (i / 'notes.txt').write_text('keep me')
        run(i, o, '--copy-others')
        assert (o / 'notes.txt').read_text() == 'keep me'

    def test_failed_file_is_not_copied_as_an_other(self, dirs):
        i, o = dirs
        write_dcm(i / 'bad.dcm')
        (i / 'notes.txt').write_text('keep me')
        with pytest.raises(SystemExit):
            run(i, o, '--copy-others')
        assert tree(o) == ['notes.txt']

    @pytest.mark.parametrize('name', ['IM0001', 'x.DCM', 'x.dicom'])
    def test_unmatched_dicom_is_refused_not_passed_through(self, dirs, name, caplog):
        # PACS exports are often extension-less or upper-case; the default
        # glob is case-sensitive '*.dcm'
        i, o = dirs
        write_dcm(i / name)
        with pytest.raises(SystemExit) as exc:
            run(i, o, '--copy-others')
        assert exc.value.code == 1
        assert tree(o) == []
        assert 'NOT redacted' in caplog.text

    def test_without_copy_others_unmatched_files_are_ignored(self, dirs):
        i, o = dirs
        write_dcm(i / 'IM0001')
        run(i, o)
        assert tree(o) == []


class TestOptionValidation:

    @pytest.mark.parametrize('args', [
        ['--padding-width', '0'], ['--padding-width', '-5'],
        ['--padding-width', '501'], ['--ocr-threshold', '150'],
        ['--ocr-threshold', '-1'],
    ])
    def test_out_of_range_values_are_rejected_before_any_work(self, dirs, args):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        with pytest.raises(SystemExit) as exc:
            run(i, o, *args)
        assert exc.value.code == 2
        assert FakeEngine.calls == [] and tree(o) == []


class TestMetadataRecall:

    def test_use_metadata_follows_the_flag(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o)
        run(i, o, '--no-metadata-recall')
        assert FakeEngine.calls[0]['use_metadata'] is True
        assert FakeEngine.calls[1]['use_metadata'] is False

    def test_no_metadata_recall_passes_no_recognizers(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o, '--no-metadata-recall')
        assert 'ad_hoc_recognizers' not in FakeEngine.calls[0]

    def test_recognizer_contains_patient_values(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm', name='DOE^JANE', pid='MRN123')
        run(i, o)
        recs = FakeEngine.calls[0]['ad_hoc_recognizers']
        assert len(recs) == 1
        assert {'DOE', 'JANE', 'MRN123'} <= set(recs[0].deny_list)

    def test_multi_valued_tags_are_expanded(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm', other_ids=['A1B2', 'C3D4'])
        run(i, o)
        deny = set(FakeEngine.calls[0]['ad_hoc_recognizers'][0].deny_list)
        assert {'A1B2', 'C3D4'} <= deny
        assert not any('[' in v for v in deny)

    def test_unreadable_header_still_redacts_without_recognizer(self, dirs, caplog):
        i, o = dirs
        (i / 'a.dcm').write_bytes(b'\x00' * 10)
        run(i, o)
        assert 'ad_hoc_recognizers' not in FakeEngine.calls[0]
        assert tree(o) == ['a.dcm']

    def test_engine_receives_cli_options(self, dirs):
        i, o = dirs
        write_dcm(i / 'a.dcm')
        run(i, o, '--fill', 'background', '--padding-width', '7',
            '--ocr-threshold', '80', '--ocr-psm', '12', '--save-bboxes')
        kw = FakeEngine.calls[0]
        assert kw['fill'] == 'background'
        assert kw['padding_width'] == 7
        assert kw['ocr_kwargs'] == {'ocr_threshold': 80.0, 'config': '--psm 12'}
        assert kw['save_bboxes'] is True


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
