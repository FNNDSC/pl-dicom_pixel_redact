"""Unit tests for app.py's argparse surface.

Doesn't import presidio/pydicom-heavy code paths -- just exercises
`app.parser`, which is safe/fast to construct.
"""
import sys

import pytest

import dicom_pixel_redact as app


def parse(argv):
    return app.parser.parse_args(argv)


class TestDefaults:

    def test_defaults_with_no_flags(self):
        opts = parse([])
        assert opts.pattern == '**/*.dcm'
        assert opts.fill == 'contrast'
        assert opts.padding_width == 25
        assert opts.ocr_threshold == 50.0
        assert opts.save_bboxes is False
        assert opts.copy_others is False
        assert opts.metadata_recall is True
        assert opts.recall_tags == ','.join(app.DEFAULT_RECALL_TAGS)
        assert opts.recall_min_len == 2
        assert opts.verbose is False

    def test_bare_parser_does_not_yet_have_positional_dirs(self):
        # `app.parser` is the pre-decoration parser; `inputdir`/`outputdir`
        # positional args are spliced in by the @chris_plugin decorator onto
        # an internal deep copy, not onto this module-level object. This
        # guards against relying on the wrong parser instance.
        with pytest.raises(SystemExit):
            parse(['/some/in', '/some/out'])


class TestMetadataRecallFlag:

    def test_no_metadata_recall_disables(self):
        opts = parse(['--no-metadata-recall'])
        assert opts.metadata_recall is False

    def test_recall_alias_enables(self):
        opts = parse(['--recall'])
        assert opts.metadata_recall is True

    def test_no_recall_alias_disables(self):
        opts = parse(['--no-recall'])
        assert opts.metadata_recall is False

    def test_explicit_metadata_recall_enables(self):
        opts = parse(['--metadata-recall'])
        assert opts.metadata_recall is True


class TestOtherFlags:

    def test_fill_rejects_invalid_choice(self):
        with pytest.raises(SystemExit):
            parse(['--fill', 'rainbow'])

    def test_fill_accepts_background(self):
        opts = parse(['--fill', 'background'])
        assert opts.fill == 'background'

    def test_padding_width_is_int(self):
        opts = parse(['--padding-width', '10'])
        assert opts.padding_width == 10
        assert isinstance(opts.padding_width, int)

    def test_padding_width_rejects_non_int(self):
        with pytest.raises(SystemExit):
            parse(['--padding-width', 'ten'])

    def test_ocr_threshold_is_float(self):
        opts = parse(['--ocr-threshold', '75'])
        assert opts.ocr_threshold == 75.0

    def test_custom_pattern(self):
        opts = parse(['-p', '*.dicom'])
        assert opts.pattern == '*.dicom'

    def test_custom_recall_tags(self):
        opts = parse(['--recall-tags', 'PatientID,AccessionNumber'])
        assert opts.recall_tags == 'PatientID,AccessionNumber'

    def test_recall_min_len_is_int(self):
        opts = parse(['--recall-min-len', '4'])
        assert opts.recall_min_len == 4

    def test_save_bboxes_flag(self):
        opts = parse(['--save-bboxes'])
        assert opts.save_bboxes is True

    def test_copy_others_flag(self):
        opts = parse(['--copy-others'])
        assert opts.copy_others is True

    def test_verbose_short_flag(self):
        opts = parse(['-v'])
        assert opts.verbose is True

    def test_version_flag_exits_cleanly(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            parse(['--version'])
        assert exc_info.value.code == 0
        assert app.__version__ in capsys.readouterr().out


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))