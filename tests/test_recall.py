"""Unit tests for the metadata-recall helpers in app.py.

These are pure-logic tests: no OCR, no spaCy model, no Tesseract, no file
I/O. They run fast and should pass in any environment with
requirements.txt installed (in particular: pydicom, presidio-analyzer).
"""
import pytest
from pydicom.dataset import Dataset

import dicom_pixel_redact as app


# --------------------------------------------------------------------------
# tokens_from_dicom_value
# --------------------------------------------------------------------------

class TestTokensFromDicomValue:

    def test_empty_and_blank_values_yield_nothing(self):
        assert app.tokens_from_dicom_value('') == []
        assert app.tokens_from_dicom_value('   ') == []
        assert app.tokens_from_dicom_value(None) == []

    def test_plain_value_is_a_single_token(self):
        assert app.tokens_from_dicom_value('MRN12345') == ['MRN12345']

    def test_plain_value_is_stripped(self):
        assert app.tokens_from_dicom_value('  MRN12345  ') == ['MRN12345']

    def test_caret_separated_name_splits_into_components(self):
        tokens = app.tokens_from_dicom_value('DOE^JANE^Q')
        # whole value + each non-empty component
        assert tokens == ['DOE^JANE^Q', 'DOE', 'JANE', 'Q']

    def test_missing_name_components_are_skipped(self):
        # family name only, no given/middle/prefix/suffix
        tokens = app.tokens_from_dicom_value('DOE^^')
        assert tokens == ['DOE^^', 'DOE']

    def test_whitespace_only_components_are_dropped(self):
        tokens = app.tokens_from_dicom_value('DOE^ ^JANE')
        assert tokens == ['DOE^ ^JANE', 'DOE', 'JANE']


# --------------------------------------------------------------------------
# extract_recall_values
# --------------------------------------------------------------------------

class TestExtractRecallValues:

    def make_dataset(self, **kwargs) -> Dataset:
        ds = Dataset()
        for keyword, value in kwargs.items():
            setattr(ds, keyword, value)
        return ds

    def test_pulls_values_for_requested_tags(self):
        ds = self.make_dataset(PatientName='DOE^JANE', PatientID='MRN1')
        values = app.extract_recall_values(ds, ['PatientName', 'PatientID'])
        assert values == {'DOE^JANE', 'DOE', 'JANE', 'MRN1'}

    def test_ignores_tags_not_present_in_dataset(self):
        ds = self.make_dataset(PatientName='DOE^JANE')
        values = app.extract_recall_values(
            ds, ['PatientName', 'PatientID', 'AccessionNumber']
        )
        assert values == {'DOE^JANE', 'DOE', 'JANE'}

    def test_ignores_tags_not_requested(self):
        ds = self.make_dataset(PatientName='DOE^JANE', PatientID='MRN1')
        values = app.extract_recall_values(ds, ['PatientID'])
        assert values == {'MRN1'}

    def test_empty_tag_list_yields_no_values(self):
        ds = self.make_dataset(PatientName='DOE^JANE')
        assert app.extract_recall_values(ds, []) == set()

    def test_empty_dataset_yields_no_values(self):
        ds = Dataset()
        assert app.extract_recall_values(ds, app.DEFAULT_RECALL_TAGS) == set()

    def test_min_len_filters_short_tokens(self):
        # single-char middle initial "Q" should be dropped at min_len=2
        ds = self.make_dataset(PatientName='DOE^JANE^Q')
        values = app.extract_recall_values(ds, ['PatientName'], min_len=2)
        assert 'Q' not in values
        assert values == {'DOE^JANE^Q', 'DOE', 'JANE'}

    def test_min_len_zero_keeps_everything(self):
        ds = self.make_dataset(PatientName='DOE^JANE^Q')
        values = app.extract_recall_values(ds, ['PatientName'], min_len=0)
        assert 'Q' in values

    def test_multiple_tags_are_unioned(self):
        ds = self.make_dataset(
            PatientName='DOE^JANE',
            PatientID='MRN1',
            AccessionNumber='ACC9',
        )
        values = app.extract_recall_values(
            ds, ['PatientName', 'PatientID', 'AccessionNumber']
        )
        assert values == {'DOE^JANE', 'DOE', 'JANE', 'MRN1', 'ACC9'}

    def test_default_recall_tags_cover_common_phi_fields(self):
        # guards against someone accidentally trimming this list
        for expected in ('PatientName', 'PatientID'):
            assert expected in app.DEFAULT_RECALL_TAGS


# --------------------------------------------------------------------------
# build_recall_recognizer
# --------------------------------------------------------------------------

class TestBuildRecallRecognizer:

    def test_returns_none_when_no_values(self):
        ds = Dataset()
        assert app.build_recall_recognizer(ds, app.DEFAULT_RECALL_TAGS) is None

    def test_returns_recognizer_with_deny_list(self):
        ds = Dataset()
        ds.PatientName = 'DOE^JANE'
        ds.PatientID = 'MRN1'
        recognizer = app.build_recall_recognizer(ds, ['PatientName', 'PatientID'])
        assert recognizer is not None
        assert recognizer.supported_entities == [app.RECALL_ENTITY_NAME]
        assert set(recognizer.deny_list) == {'DOE^JANE', 'DOE', 'JANE', 'MRN1'}

    def test_recognizer_matches_burned_in_text_exactly(self):
        ds = Dataset()
        ds.PatientName = 'DOE^JANE^Q'
        ds.PatientID = 'MRN12345'
        recognizer = app.build_recall_recognizer(ds, ['PatientName', 'PatientID'])

        text = 'ULTRASOUND -- DOE JANE -- MRN12345 -- 12-JAN-2024'
        results = recognizer.analyze(text=text, entities=[app.RECALL_ENTITY_NAME])
        matched_spans = {text[r.start:r.end] for r in results}
        assert 'DOE' in matched_spans
        assert 'JANE' in matched_spans
        assert 'MRN12345' in matched_spans

    def test_recognizer_does_not_match_unrelated_text(self):
        ds = Dataset()
        ds.PatientName = 'DOE^JANE'
        ds.PatientID = 'MRN12345'
        recognizer = app.build_recall_recognizer(ds, ['PatientName', 'PatientID'])

        text = 'GENERAL ELECTRIC LOGIQ E9 -- 12-JAN-2024'
        results = recognizer.analyze(text=text, entities=[app.RECALL_ENTITY_NAME])
        assert results == []

    def test_two_patients_do_not_leak_into_each_others_deny_list(self):
        ds_a = Dataset()
        ds_a.PatientName = 'DOE^JANE'
        ds_b = Dataset()
        ds_b.PatientName = 'SMITH^JOHN'

        recognizer_a = app.build_recall_recognizer(ds_a, ['PatientName'])
        recognizer_b = app.build_recall_recognizer(ds_b, ['PatientName'])

        assert 'SMITH' not in recognizer_a.deny_list
        assert 'JOHN' not in recognizer_a.deny_list
        assert 'DOE' not in recognizer_b.deny_list
        assert 'JANE' not in recognizer_b.deny_list


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))