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
from pathlib import Path
from argparse import ArgumentParser, Namespace, ArgumentDefaultsHelpFormatter, BooleanOptionalAction
import pydicom
from presidio_image_redactor import DicomImageRedactorEngine
from presidio_analyzer import PatternRecognizer

from chris_plugin import chris_plugin, PathMapper

logging.basicConfig(
        level=logging.DEBUG,
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
        raw = str(ds.get(tag, ''))
        for token in tokens_from_dicom_value(raw):
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
    '--save-bboxes',
    action='store_true',
    default=False,
    help='write a JSON file of redacted bounding boxes next to each output DICOM',
)
parser.add_argument(
    '--copy-others',
    action='store_true',
    default=False,
    help='copy non-matching files from inputdir to outputdir unchanged',
)
parser.add_argument(
    '--metadata-recall', '--recall',
    dest='metadata_recall',
    action=BooleanOptionalAction,
    default=True,
    help=(
        "for each file, also flag any burned-in text that exactly matches "
        "that file's own PatientName/PatientID/etc. DICOM header values "
        "(a per-file deny-list ad-hoc recognizer). This boosts recall for "
        "identifiers Presidio's general NER model might not catch on its "
        "own -- e.g. unusual name formats, MRNs, accession numbers -- since "
        "we already know the ground-truth value from the header. Disable "
        "with --no-metadata-recall if you don't trust the header to be "
        "accurate yet (e.g. it hasn't been reconciled against the pixels)."
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
    category='',                 # ref. https://chrisstore.co/plugins
    min_memory_limit='100Mi',    # supported units: Mi, Gi
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

    # Typically it's easier to think of programs as operating on individual files
    # rather than directories. The helper functions provided by a ``PathMapper``
    # object make it easy to discover input files and write to output files inside
    # the given paths.
    #
    # Refer to the documentation for more options, examples, and advanced uses e.g.
    # adding a progress bar and parallelism.
    engine = DicomImageRedactorEngine()
    ocr_kwargs = {'ocr_threshold': options.ocr_threshold}
    recall_tags = [t.strip() for t in options.recall_tags.split(',') if t.strip()]

    mapper = PathMapper.file_mapper(
        inputdir, outputdir, glob=options.pattern, fail_if_empty=False
    )

    n_ok = 0
    n_failed = 0

    for input_file, output_file in mapper:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        log.info('Scrubbing %s', input_file.relative_to(inputdir))

        extra_kwargs = {}
        if options.metadata_recall:
            try:
                header = pydicom.dcmread(str(input_file), stop_before_pixels=True, force=True)
                recognizer = build_recall_recognizer(header, recall_tags, options.recall_min_len)
            except Exception as e:
                log.debug('Could not read header of %s for recall: %s', input_file, e)
                recognizer = None
            if recognizer is not None:
                extra_kwargs['ad_hoc_recognizers'] = [recognizer]

        try:
            # redact_from_file writes the scrubbed DICOM (and, if
            # save_bboxes=True, a companion bounding-box JSON) into
            # output_dir under the input file's own name.
            engine.redact_from_file(
                str(input_file),
                str(output_file.parent),
                padding_width=options.padding_width,
                fill=options.fill,
                save_bboxes=options.save_bboxes,
                ocr_kwargs=ocr_kwargs,
                **extra_kwargs,
            )
            produced = output_file.parent / input_file.name
            if produced != output_file and produced.exists():
                produced.rename(output_file)
            n_ok += 1
        except Exception as e:  # noqa: BLE001 — one bad file shouldn't kill the run
            log.error('Failed to scrub %s: %s', input_file, e)
            n_failed += 1

    if options.copy_others:
        import shutil
        matched = {
            p for p, _ in PathMapper.file_mapper(
                inputdir, outputdir, glob=options.pattern, fail_if_empty=False
            )
        }
        for src in inputdir.glob('**/*'):
            if src.is_file() and src not in matched:
                dst = outputdir / src.relative_to(inputdir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    log.info('Done. %d file(s) scrubbed, %d failed.', n_ok, n_failed)
    if n_ok == 0 and n_failed == 0:
        log.warning(
            "No files matched pattern '%s' under %s -- did you mean to pass "
            "a different --pattern?", options.pattern, inputdir
        )


if __name__ == '__main__':
    main()
