"""Integration test for dicom_pixel_redact.py's `main()`.

Unlike test_recall.py / test_cli.py, this actually runs the full pipeline:
Tesseract OCR -> spaCy NER -> Presidio redaction -> pixel data rewritten.
That means it needs real, heavy dependencies that unit tests should never
require:

  - the `tesseract` binary on PATH
  - the `en_core_web_lg` spaCy model installed
  - presidio-image-redactor and its transitive deps

These are present in the plugin's Docker image (see Dockerfile) but not
necessarily on a bare dev machine or in a lightweight CI job, so this test
skips itself (rather than failing) when the stack isn't available.

Run explicitly with:  pytest tests/test_integration.py -v
"""
import shutil
import sys
from pathlib import Path

import pytest

import dicom_pixel_redact as app


def _stack_available() -> bool:
    if shutil.which('tesseract') is None:
        return False
    try:
        import spacy
        if not spacy.util.is_package('en_core_web_lg'):
            return False
        import presidio_image_redactor  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _stack_available(),
    reason='requires tesseract + en_core_web_lg + presidio-image-redactor '
           '(present in the Docker image, see Dockerfile)',
)


def make_synthetic_dicom(
    path: Path,
    patient_name: str,
    patient_id: str,
    burned_text: str,
    with_pixel_data: bool = True,
) -> None:
    """Write a small single-frame DICOM whose pixel data has `burned_text`
    rendered onto it as white text on a dark background -- a stand-in for a
    secondary-capture / ultrasound frame with identifying info baked into
    the image itself."""
    import datetime

    import numpy as np
    import pydicom
    from PIL import Image, ImageDraw, ImageFont
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

    img = Image.new('L', (800, 300), color=20)
    try:  # Pillow's tiny default bitmap font OCRs poorly; sized default needs >=10.1
        font = ImageFont.load_default(size=32)
    except TypeError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((10, 10), burned_text, fill=255, font=font)
    arr = np.array(img)

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    path.parent.mkdir(parents=True, exist_ok=True)
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\x00' * 128)
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.Modality = 'OT'
    ds.ContentDate = datetime.date.today().strftime('%Y%m%d')
    ds.Rows, ds.Columns = arr.shape
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if with_pixel_data:
        ds.PixelData = arr.tobytes()
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.save_as(str(path), write_like_original=False)


def run_plugin(inputdir: Path, outputdir: Path, extra_args=None):
    argv = list(extra_args or [])
    options = app.parser.parse_args(argv)
    # app.main is the @chris_plugin-decorated function; per its own
    # documented test pattern it can be invoked directly with
    # (options, inputdir, outputdir) for a ds-type plugin.
    app.main(options, inputdir, outputdir)


def ocr_text(dataset) -> str:
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.fromarray(dataset.pixel_array)).upper()


class TestPixelRedaction:

    def test_burned_in_patient_name_is_redacted_from_pixels(self, tmp_path):
        inputdir = tmp_path / 'in'
        outputdir = tmp_path / 'out'
        outputdir.mkdir()

        dcm_path = inputdir / 'sample.dcm'
        make_synthetic_dicom(
            dcm_path,
            patient_name='DOE^JANE^Q',
            patient_id='MRN99887',
            burned_text='PATIENT: JANE DOE   ID: MRN99887',
        )

        run_plugin(inputdir, outputdir)

        out_file = outputdir / 'sample.dcm'
        assert out_file.exists(), 'plugin should produce an output DICOM of the same name'

        import pydicom

        original = pydicom.dcmread(str(dcm_path))
        redacted = pydicom.dcmread(str(out_file))

        # positive control: OCR must be able to read the text in the INPUT,
        # otherwise "not found in the output" below proves nothing
        assert 'JANE' in ocr_text(original)

        # pixel data should have actually changed
        assert redacted.PixelData != original.PixelData

        # and OCR against the *output* pixels should no longer turn up the
        # patient's real name or ID
        redacted_text = ocr_text(redacted)
        assert 'JANE' not in redacted_text
        assert 'MRN99887' not in redacted_text

    def test_metadata_still_present_after_pixel_only_scrub(self, tmp_path):
        # documents the plugin's stated scope: pixel-only. Metadata tags
        # should survive untouched (a separate tag-scrubbing step is needed
        # for those).
        inputdir = tmp_path / 'in'
        outputdir = tmp_path / 'out'
        outputdir.mkdir()

        dcm_path = inputdir / 'sample.dcm'
        make_synthetic_dicom(
            dcm_path,
            patient_name='DOE^JANE',
            patient_id='MRN1',
            burned_text='JANE DOE',
        )

        run_plugin(inputdir, outputdir)

        import pydicom
        redacted = pydicom.dcmread(str(outputdir / 'sample.dcm'))
        assert str(redacted.PatientName) == 'DOE^JANE'
        assert str(redacted.PatientID) == 'MRN1'

    def test_no_metadata_recall_still_redacts_via_generic_ner(self, tmp_path):
        # --no-metadata-recall turns off BOTH our header deny-list and
        # Presidio's own (use_metadata=False), so this exercises NER alone.
        # If this fails, NER recall on this font/size is the limitation.
        inputdir = tmp_path / 'in'
        outputdir = tmp_path / 'out'
        outputdir.mkdir()

        dcm_path = inputdir / 'sample.dcm'
        make_synthetic_dicom(
            dcm_path,
            patient_name='SMITH^JOHN',
            patient_id='MRN2',
            burned_text='John Smith',
        )

        run_plugin(inputdir, outputdir, extra_args=['--no-metadata-recall'])

        out_file = outputdir / 'sample.dcm'
        assert out_file.exists()

        import pydicom
        original = pydicom.dcmread(str(dcm_path))
        redacted = pydicom.dcmread(str(out_file))
        assert 'JOHN' in ocr_text(original)          # positive control
        assert redacted.PixelData != original.PixelData
        assert 'JOHN' not in ocr_text(redacted)

    def test_copy_others_carries_non_matching_files_through(self, tmp_path):
        inputdir = tmp_path / 'in'
        outputdir = tmp_path / 'out'
        inputdir.mkdir()
        outputdir.mkdir()

        (inputdir / 'notes.txt').write_text('not a dicom')
        dcm_path = inputdir / 'sample.dcm'
        make_synthetic_dicom(dcm_path, 'DOE^JANE', 'MRN1', 'JANE DOE')

        run_plugin(inputdir, outputdir, extra_args=['--copy-others'])

        assert (outputdir / 'sample.dcm').exists()
        assert (outputdir / 'sample.dcm').read_bytes() != dcm_path.read_bytes(), \
            'the DICOM must be redacted, not just copied'
        assert (outputdir / 'notes.txt').read_text() == 'not a dicom'

    def test_no_matching_files_does_not_crash(self, tmp_path):
        inputdir = tmp_path / 'in'
        outputdir = tmp_path / 'out'
        inputdir.mkdir()
        outputdir.mkdir()
        (inputdir / 'notes.txt').write_text('not a dicom')

        # should log a warning and exit cleanly, not raise
        run_plugin(inputdir, outputdir)
        assert list(outputdir.iterdir()) == []


class TestRealEngineFailClosed:
    """The real engine copies its input into the output dir before it fails;
    the plugin must make sure nothing un-redacted survives."""

    def test_file_without_pixel_data_leaves_nothing_in_output(self, tmp_path):
        inputdir, outputdir = tmp_path / 'in', tmp_path / 'out'
        outputdir.mkdir()
        make_synthetic_dicom(inputdir / 'nopix.dcm', 'DOE^JANE', 'MRN1',
                             'JANE DOE', with_pixel_data=False)
        with pytest.raises(SystemExit) as exc:
            run_plugin(inputdir, outputdir)
        assert exc.value.code == 1
        assert list(outputdir.rglob('*')) == []

    def test_truncated_pixel_data_leaves_nothing_in_output(self, tmp_path):
        inputdir, outputdir = tmp_path / 'in', tmp_path / 'out'
        outputdir.mkdir()
        good = inputdir / 'trunc.dcm'
        make_synthetic_dicom(good, 'DOE^JANE', 'MRN1', 'JANE DOE')
        good.write_bytes(good.read_bytes()[:-5000])
        with pytest.raises(SystemExit):
            run_plugin(inputdir, outputdir)
        assert list(outputdir.rglob('*')) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
