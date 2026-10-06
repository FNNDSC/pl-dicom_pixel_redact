"""Unit tests for dicom_pixel_redact.py's argparse surface.

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

    def test_enabled_by_default(self):
        assert parse([]).metadata_recall is True

    def test_removed_aliases_are_rejected(self):
        # BooleanOptionalAction (--metadata-recall/--no-recall...) is not
        # supported by chris_plugin, so these must not exist.
        for flag in ('--metadata-recall', '--recall', '--no-recall'):
            with pytest.raises(SystemExit):
                parse([flag])


class TestChrisDescriptor:

    def test_parser_serializes_for_chris(self):
        # chris_plugin_info runs this inside the image during release; an
        # unsupported argparse Action raises TypeError there, *after* the
        # image has already been pushed.
        from chris_plugin.parameters import serialize
        specs = serialize(app.parser)
        by_flag = {spec['flag']: spec for spec in specs}
        assert by_flag['--no-metadata-recall']['default'] is True
        assert by_flag['--no-metadata-recall']['action'] == 'store_false'


class TestOcrPsm:

    def test_default_is_11(self):
        assert parse([]).ocr_psm == 11

    @pytest.mark.parametrize('mode', [11, 12, 13])
    def test_allowed_modes(self, mode):
        assert parse(['--ocr-psm', str(mode)]).ocr_psm == mode

    @pytest.mark.parametrize('mode', [*range(11), 14, -1])
    def test_modes_below_11_or_out_of_range_are_rejected(self, mode):
        # e.g. 0 (orientation only) and 2 (no OCR) return no text, which would
        # leave the image un-redacted while the run still exits 0
        with pytest.raises(SystemExit):
            parse(['--ocr-psm', str(mode)])

    def test_serializes_for_chris(self):
        from chris_plugin.parameters import serialize
        by_flag = {spec['flag']: spec for spec in serialize(app.parser)}
        assert by_flag['--ocr-psm']['default'] == 11


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