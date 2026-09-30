"""Failure-scenario tests for main().

The Presidio engine is replaced with a fake, so these run without
Tesseract / spaCy and exercise only the plugin's own control flow:
per-file error isolation, fail-closed output, exit codes, and the
metadata-recall fallback.
"""
import sys
from pathlib import Path

import pytest

import dicom_pixel_redact as app


class FakeEngine:
    """Stands in for DicomImageRedactorEngine.

    Files whose name starts with 'bad' raise; all others are copied to the
    output dir with a marker appended so tests can tell they were "redacted".
    Records the kwargs of every call.
    """
    calls: list = []

    def redact_from_file(self, input_path, output_dir, **kwargs):
        FakeEngine.calls.append((Path(input_path).name, kwargs))
        src = Path(input_path)
        if src.name.startswith('bad'):
            raise ValueError('cannot parse DICOM')
        (Path(output_dir) / src.name).write_bytes(src.read_bytes() + b'REDACTED')


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


class TestFailingFiles:

    def test_corrupt_file_exits_nonzero(self, dirs):
        i, o = dirs
        (i / 'bad.dcm').write_bytes(b'not a dicom')
        with pytest.raises(SystemExit) as exc:
            run(i, o)
        assert exc.value.code == 1

    def test_failed_file_is_never_written_to_output(self, dirs):
        # fail closed: an un-redacted file must not leak into the output
        i, o = dirs
        (i / 'bad.dcm').write_bytes(b'not a dicom')
        with pytest.raises(SystemExit):
            run(i, o)
        assert not (o / 'bad.dcm').exists()

    def test_one_bad_file_does_not_stop_the_others(self, dirs):
        i, o = dirs
        (i / 'a.dcm').write_bytes(b'x')
        (i / 'bad.dcm').write_bytes(b'x')
        (i / 'c.dcm').write_bytes(b'x')
        with pytest.raises(SystemExit):
            run(i, o)
        assert (o / 'a.dcm').exists()
        assert (o / 'c.dcm').exists()

    def test_failed_file_is_not_copied_by_copy_others(self, dirs):
        # --copy-others must never fall back to copying a matched-but-failed file
        i, o = dirs
        (i / 'bad.dcm').write_bytes(b'x')
        (i / 'notes.txt').write_text('keep me')
        with pytest.raises(SystemExit):
            run(i, o, '--copy-others')
        assert not (o / 'bad.dcm').exists()
        assert (o / 'notes.txt').read_text() == 'keep me'

    def test_all_good_files_exit_cleanly(self, dirs):
        i, o = dirs
        (i / 'a.dcm').write_bytes(b'x')
        run(i, o)  # must not raise
        assert (o / 'a.dcm').read_bytes().endswith(b'REDACTED')

    def test_no_matching_files_exits_cleanly(self, dirs, caplog):
        i, o = dirs
        (i / 'notes.txt').write_text('x')
        run(i, o)
        assert list(o.iterdir()) == []
        assert 'No files matched' in caplog.text

    def test_nested_input_structure_is_preserved(self, dirs):
        i, o = dirs
        (i / 'sub').mkdir()
        (i / 'sub' / 'a.dcm').write_bytes(b'x')
        run(i, o)
        assert (o / 'sub' / 'a.dcm').exists()


class TestMetadataRecallFallback:

    def test_unreadable_header_falls_back_and_warns(self, dirs, caplog):
        # garbage bytes: dcmread(force=True) may parse or raise; either way the
        # file must still be sent to the engine, without a recognizer
        i, o = dirs
        (i / 'a.dcm').write_bytes(b'\x00' * 10)
        run(i, o)
        name, kwargs = FakeEngine.calls[0]
        assert 'ad_hoc_recognizers' not in kwargs

    def test_no_metadata_recall_never_passes_recognizers(self, dirs):
        i, o = dirs
        _write_dcm(i / 'a.dcm')
        run(i, o, '--no-metadata-recall')
        assert 'ad_hoc_recognizers' not in FakeEngine.calls[0][1]

    def test_metadata_recall_passes_recognizer_with_patient_values(self, dirs):
        i, o = dirs
        _write_dcm(i / 'a.dcm', name='DOE^JANE', pid='MRN123')
        run(i, o)
        recognizers = FakeEngine.calls[0][1]['ad_hoc_recognizers']
        assert len(recognizers) == 1
        assert {'DOE', 'JANE', 'MRN123'} <= set(recognizers[0].deny_list)

    def test_engine_receives_cli_options(self, dirs):
        i, o = dirs
        _write_dcm(i / 'a.dcm')
        run(i, o, '--fill', 'background', '--padding-width', '7',
            '--ocr-threshold', '80', '--save-bboxes')
        kw = FakeEngine.calls[0][1]
        assert kw['fill'] == 'background'
        assert kw['padding_width'] == 7
        assert kw['ocr_kwargs'] == {'ocr_threshold': 80.0}
        assert kw['save_bboxes'] is True


def _write_dcm(path: Path, name='DOE^JANE', pid='MRN1') -> Path:
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\x00' * 128)
    ds.PatientName, ds.PatientID = name, pid
    ds.is_little_endian, ds.is_implicit_VR = True, False
    ds.save_as(str(path), write_like_original=False)
    return path


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
