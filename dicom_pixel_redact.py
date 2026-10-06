#!/usr/bin/env python
"""
pl-dicom_pixel_redact

A ChRIS plugin that de-identifies burned-in PHI/PII in DICOM pixel data
(e.g. patient name, MRN, or DOB rendered directly onto an ultrasound or
secondary-capture image) using Microsoft Presidio's DicomImageRedactorEngine.

Note: this plugin only scrubs *pixel* data. It does not touch PHI that may
live in DICOM metadata tags (PatientName, PatientID, etc.) — pair it with a
tag-scrubbing plugin (e.g. pl-pfdicom_anonymize, or pydicom-based tag removal)
for full de-identification.
"""
import logging
import os
import shutil
import tempfile
from pathlib import Path
from argparse import ArgumentParser, Namespace, ArgumentDefaultsHelpFormatter
import pydicom
from pydicom.multival import MultiValue
from presidio_image_redactor import DicomImageRedactorEngine
from presidio_analyzer import PatternRecognizer

from chris_plugin import chris_plugin, PathMapper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger(__name__)


__version__ = '1.0.0'

# DICOM keywords whose values are, by definition, this study's own ground-truth
# PHI. We use them as a deny-list ad-hoc recognizer per file: if burned-in
# pixel text OCRs to an exact match of the patient's actual name/ID/etc, we
# flag it regardless of whether Presidio's general NER model recognizes it as
# a PERSON/ID -- this is the "recall" boost from the header, same idea as
# comparing OCR output against the known Manufacturer string in template-based
# tools, but applied to patient identifiers instead of scanner metadata.
DEFAULT_RECALL_TAGS = [
    'PatientName',
    'PatientID',
    'OtherPatientIDs',
    'PatientBirthDate',
    'AccessionNumber',
    'ReferringPhysicianName',
]

DISPLAY_TITLE = r"""
       _           _ _                               _          _                _            _   
      | |         | (_)                             (_)        | |              | |          | |  
 _ __ | |______ __| |_  ___ ___  _ __ ___      _ __  ___  _____| |  _ __ ___  __| | __ _  ___| |_ 
| '_ \| |______/ _` | |/ __/ _ \| '_ ` _ \    | '_ \| \ \/ / _ \ | | '__/ _ \/ _` |/ _` |/ __| __|
| |_) | |     | (_| | | (_| (_) | | | | | |   | |_) | |>  <  __/ | | | |  __/ (_| | (_| | (__| |_ 
| .__/|_|      \__,_|_|\___\___/|_| |_| |_|   | .__/|_/_/\_\___|_| |_|  \___|\__,_|\__,_|\___|\__|
| |                                     ______| |              ______                             
|_|                                    |______|_|             |______|                            
"""

RECALL_ENTITY_NAME = 'PHI_FROM_DICOM_HEADER'


def tokens_from_dicom_value(raw: str) -> list[str]:
    """Split one DICOM element's string value into deny-list candidate
    tokens: the value as a whole, plus (for caret-separated PN-style values
    such as ``"Doe^Jane^Q"``) each individual component.

    Pure string logic, no pydicom/file I/O -- kept separate so it's trivial
    to unit test.
    """
    raw = (raw or '').strip()
    if not raw:
        return []
    tokens = [raw]
    if '^' in raw:
        tokens.extend(part.strip() for part in raw.split('^') if part.strip())
    return tokens


def extract_recall_values(ds, tags: list[str], min_len: int = 2) -> set[str]:
    """Pull deny-list values out of an already-loaded ``pydicom.Dataset``'s
    header for the given DICOM keywords, dropping anything shorter than
    ``min_len`` characters.

    Takes a ``Dataset`` (not a file path) so it can be unit tested against
    an in-memory object with no file I/O and no dependency on pydicom's
    file-reading/validation machinery.
    """
    values: set[str] = set()
    for tag in tags:
        if tag not in ds:
            continue
        elem = ds.get(tag)
        # multi-valued elements (e.g. OtherPatientIDs) must be iterated;
        # str() of a MultiValue is "['A1', 'B2']", which matches nothing
        items = list(elem) if isinstance(elem, (list, MultiValue)) else [elem]
        for item in items:
            for token in tokens_from_dicom_value(str(item) if item is not None else ''):
                if len(token) >= min_len:
                    values.add(token)
    return values


def build_recall_recognizer(ds, tags: list[str], min_len: int = 2):
    """Build a Presidio deny-list ``PatternRecognizer`` from one DICOM
    dataset's own PHI header values (``PatientName``, ``PatientID``, ...),
    so exact burned-in matches get flagged even when Presidio's general NER
    model wouldn't otherwise catch them. Returns ``None`` if the dataset has
    no usable values for the given tags.
    """
    values = extract_recall_values(ds, tags, min_len)
    if not values:
        return None
    return PatternRecognizer(
        supported_entity=RECALL_ENTITY_NAME,
        deny_list=sorted(values),
        deny_list_score=1.0,
    )


def unsupported_reason(header) -> str | None:
    """Return why this file cannot be redacted safely, or ``None``.

    Checked from the header alone, *before* the engine runs, so unsupported
    input is refused without ever touching the output directory.

    - Multi-frame images: Presidio's engine only handles a single 2-D frame
      and raises on anything else.

    Compressed transfer syntaxes are passed to Presidio for decompression,
    redaction and, when ng. Native GDCM recompression can
    abort the process on some inputs (notably 16-bit RLE on linux/arm64).
    """
    if header is None:
        return None

    try:
        frames = int(header.get('NumberOfFrames', 1) or 1)
    except (TypeError, ValueError):
        frames = 1
    if frames > 1:
        return f'multi-frame image ({frames} frames) is not supported'
    return None


def looks_like_dicom(path: Path) -> bool:
    """True if ``path`` has the DICOM Part-10 preamble ('DICM' at byte 128),
    regardless of its file name."""
    try:
        with open(path, 'rb') as f:
            return f.read(132)[128:132] == b'DICM'
    except OSError:
        return False


def publish(produced_dir: Path, dest_dir: Path) -> None:
    """Move every file the engine produced into ``dest_dir``.

    Each file is copied to a hidden temporary name and then renamed, so the
    final name only ever appears with complete content.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in produced_dir.iterdir():
        partial = dest_dir / f'.{f.name}.partial'
        shutil.copyfile(f, partial)
        os.replace(partial, dest_dir / f.name)


def redact_one(engine, input_file: Path, dest_dir: Path, header,
               options: Namespace, extra_kwargs: dict) -> None:
    """Redact ``input_file`` into ``dest_dir``, fail-closed.

    ``DicomImageRedactorEngine.redact_from_file`` copies its input into the
    output directory *first* and redacts that copy in place, so an exception
    midway would leave the unredacted original there. We therefore run the
    engine in a throw-away directory and only publish its output after it
    returned successfully. Any failure leaves ``dest_dir`` untouched.
    """
    with tempfile.TemporaryDirectory(prefix='dicom_pixel_redact_') as tmp:
        src_dir, out_dir = Path(tmp) / 'src', Path(tmp) / 'out'
        src_dir.mkdir()
        out_dir.mkdir()
        work = src_dir / input_file.name

        if header is not None and 'IconImageSequence' in header:
            # Presidio only redacts the top-level PixelData; a thumbnail
            # would keep its burned-in text. Drop it from the working copy.
            log.warning('%s: removing IconImageSequence (thumbnail is not redacted)',
                        input_file.name)
            ds = pydicom.dcmread(str(input_file))
            del ds.IconImageSequence
            ds.save_as(str(work))
        else:
            shutil.copyfile(input_file, work)

        engine.redact_from_file(
            str(work),
            str(out_dir),
            padding_width=options.padding_width,
            fill=options.fill,
            save_bboxes=options.save_bboxes,
            ocr_kwargs={'ocr_threshold': options.ocr_threshold, 'config': f'--psm {options.ocr_psm}'},
            use_metadata=options.metadata_recall,
            **extra_kwargs,
        )
        if not (out_dir / input_file.name).is_file():
            raise RuntimeError('redaction engine produced no output file')
        publish(out_dir, dest_dir)


parser = ArgumentParser(
    description=(
        "De-identify burned-in PHI/PII in DICOM pixel data using Microsoft "
        "Presidio (OCR + NER driven redaction)."
    ),
    formatter_class=ArgumentDefaultsHelpFormatter,
)
parser.add_argument(
    '-V', '--version', action='version', version=f'%(prog)s {__version__}'
)
parser.add_argument(
    '-p', '--pattern',
    default='**/*.dcm',
    help='glob pattern (relative to inputdir) used to find DICOM files',
)
parser.add_argument(
    '--fill',
    default='contrast',
    choices=['contrast', 'background'],
    help=(
        "redaction box fill strategy: 'contrast' picks black/white for max "
        "contrast against the surrounding pixels, 'background' samples the "
        "image's background color"
    ),
)
parser.add_argument(
    '--padding-width',
    type=int,
    default=25,
    help=(
        'pixels of padding added around detected text before OCR, which '
        'improves detection but also grows the redacted box'
    ),
)
parser.add_argument(
    '--ocr-threshold',
    type=float,
    default=50.0,
    help='minimum OCR confidence (0-100) for a text region to be considered',
)
parser.add_argument(
    '--ocr-psm',
    type=int,
    default=11,
    choices=range(0, 14),
    metavar='0-13',
    help=(
        'Tesseract page segmentation mode. '
        'Mode 11 treats the image as sparse text and is generally better '
        'suited to burned-in annotations scattered across medical images'
    ),
)
parser.add_argument(
    '--save-bboxes',
    action='store_true',
    default=False,
    help='write a JSON file of redacted bounding boxes next to each output DICOM',
)
parser.add_argument(
    '--copy-others',
    action='store_true',
    default=False,
    help=(
        'copy files that did not match --pattern from inputdir to outputdir '
        'unchanged. Files that look like DICOM (preamble check) are never '
        'copied, because they would be passed through un-redacted; they are '
        'reported as failures instead'
    ),
)
parser.add_argument(
    '--no-metadata-recall',
    dest='metadata_recall',
    action='store_false',
    default=True,
    help=(
        "by default, header values (PatientName, PatientID, ...) are used "
        "to improve detection: (1) a per-file deny-list recognizer built "
        "from --recall-tags, and (2) Presidio's own header-derived "
        "deny-list. Pass this flag to turn BOTH off and rely only on "
        "Presidio's general NER model, e.g. if the header can't be trusted "
        "yet."
    ),
)
parser.add_argument(
    '--recall-tags',
    default=','.join(DEFAULT_RECALL_TAGS),
    help='comma-separated DICOM keywords to pull --metadata-recall values from',
)
parser.add_argument(
    '--recall-min-len',
    type=int,
    default=2,
    help=(
        'minimum token length (characters) for a --metadata-recall value to '
        'be used, to avoid noisy single-character deny-list entries (e.g. a '
        "middle-initial fragment of PatientName)"
    ),
)
parser.add_argument(
    '-v', '--verbose',
    action='store_true',
    default=False,
    help='enable debug-level logging',
)



# The main function of this *ChRIS* plugin is denoted by this ``@chris_plugin`` "decorator."
# Some metadata about the plugin is specified here. There is more metadata specified in setup.py.
#
# documentation: https://fnndsc.github.io/chris_plugin/chris_plugin.html#chris_plugin
@chris_plugin(
    parser=parser,
    title='A ChRIS plugin to detect and redact PHI embedded in DICOM pixel data',
    category='DICOM De-identification',  # ref. https://chrisstore.co/plugins
    min_memory_limit='2Gi',      # en_core_web_lg alone needs ~0.8 GB; supported units: Mi, Gi
    min_cpu_limit='1000m',       # millicores, e.g. "1000m" = 1 CPU core
    min_gpu_limit=0              # set min_gpu_limit=1 to enable GPU
)
def main(options: Namespace, inputdir: Path, outputdir: Path):
    """
    *ChRIS* plugins usually have two positional arguments: an **input directory** containing
    input files and an **output directory** where to write output files. Command-line arguments
    are passed to this main method implicitly when ``main()`` is called below without parameters.

    :param options: non-positional arguments parsed by the parser given to @chris_plugin
    :param inputdir: directory containing (read-only) input files
    :param outputdir: directory where to write output files
    """

    print(DISPLAY_TITLE)
    if options.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        # Presidio logs its recognizer-loading block at INFO/WARNING per file
        for name in ('presidio-analyzer', 'presidio_analyzer', 'presidio_image_redactor'):
            logging.getLogger(name).setLevel(logging.ERROR)

    if not 1 <= options.padding_width <= 500:
        parser.error('--padding-width must be between 1 and 500')
    if not 0 <= options.ocr_threshold <= 100:
        parser.error('--ocr-threshold must be between 0 and 100')

    engine = DicomImageRedactorEngine()
    recall_tags = [t.strip() for t in options.recall_tags.split(',') if t.strip()]

    mapper = PathMapper.file_mapper(
        inputdir, outputdir, glob=options.pattern, fail_if_empty=False
    )
    files = list(mapper)

    n_ok = 0
    n_failed = 0

    for input_file, output_file in files:
        log.info('Scrubbing %s', input_file.relative_to(inputdir))

        try:
            header = pydicom.dcmread(str(input_file), stop_before_pixels=True, force=True)
        except Exception as e:  # noqa: BLE001
            log.warning('Could not read header of %s (%s)', input_file, e)
            header = None

        reason = unsupported_reason(header)
        if reason:
            log.error('Refusing %s: %s. It was NOT redacted and is not written to the output.',
                      input_file, reason)
            n_failed += 1
            continue

        extra_kwargs = {}
        if options.metadata_recall and header is not None:
            recognizer = build_recall_recognizer(header, recall_tags, options.recall_min_len)
            if recognizer is not None:
                extra_kwargs['ad_hoc_recognizers'] = [recognizer]

        try:
            redact_one(engine, input_file, output_file.parent, header, options, extra_kwargs)
            n_ok += 1
        except Exception as e:  # noqa: BLE001 -- one bad file shouldn't kill the run
            log.error('Failed to scrub %s: %s', input_file, e)
            n_failed += 1

    if options.copy_others:
        matched = {p for p, _ in files}
        for src in sorted(inputdir.rglob('*')):
            if not src.is_file() or src in matched:
                continue
            if looks_like_dicom(src):
                log.error('Refusing to copy %s: it looks like DICOM but did not match '
                          "--pattern '%s', so it was NOT redacted.", src, options.pattern)
                n_failed += 1
                continue
            dst = outputdir / src.relative_to(inputdir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    log.info('Done. %d file(s) scrubbed, %d failed.', n_ok, n_failed)
    if n_ok == 0 and n_failed == 0:
        log.warning(
            "No files matched pattern '%s' under %s -- did you mean to pass "
            "a different --pattern?", options.pattern, inputdir
        )
    if n_failed:
        # Fail closed: nothing un-redacted is written to the output (see
        # redact_one), and the job must not look successful downstream.
        log.error('%d file(s) could NOT be redacted and were not written to %s.',
                  n_failed, outputdir)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
