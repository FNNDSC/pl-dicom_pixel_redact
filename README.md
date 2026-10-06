# A ChRIS plugin to detect and redact PHI embedded in DICOM pixel data

[![Version](https://img.shields.io/docker/v/fnndsc/pl-dicom_pixel_redact?sort=semver)](https://hub.docker.com/r/fnndsc/pl-dicom_pixel_redact)
[![MIT License](https://img.shields.io/github/license/fnndsc/pl-dicom_pixel_redact)](https://github.com/FNNDSC/pl-dicom_pixel_redact/blob/main/LICENSE)
[![ci](https://github.com/FNNDSC/pl-dicom_pixel_redact/actions/workflows/ci.yml/badge.svg)](https://github.com/FNNDSC/pl-dicom_pixel_redact/actions/workflows/ci.yml)

`pl-dicom_pixel_redact` is a [_ChRIS_](https://chrisproject.org/)
_ds_ plugin which takes a directory of **DICOM files with PHI burned into
their pixel data** (e.g. an ultrasound or secondary-capture frame with the
patient's name, MRN, or DOB rendered directly onto the image) as input files
and creates a directory of the same DICOMs, with that burned-in PHI
**redacted from the pixels**, as output files.

## Abstract

Some DICOM images carry identifying information not just in header tags, but
burned directly into the pixels themselves — this is common on ultrasound,
secondary-capture, and scanned-in images, and it's invisible to any tool that
only sanitizes DICOM metadata.

`pl-dicom_pixel_redact` finds and blacks out that text using
[Microsoft Presidio](https://microsoft.github.io/presidio/)'s
`DicomImageRedactorEngine`: Tesseract OCR reads text off the image, spaCy's
NER model classifies it, and any span recognized as PHI gets redacted in
place.

On top of Presidio's default detection, this plugin also builds a **per-file
recall boost** from each DICOM's own header: since a file's `PatientName`,
`PatientID`, and similar tags are already known ground truth, an exact
burned-in match against those values is flagged even when the general NER
model wouldn't otherwise catch it (unusual name formats, MRNs, accession
numbers, etc). This is on by default and can be tuned or disabled — see
`--no-metadata-recall` below.

**Scope note:** this plugin only redacts *pixel* data. It does not modify
PHI living in DICOM metadata tags (`PatientName`, `PatientID`, ...) — pair it
with a tag-scrubbing plugin for full de-identification.

## Installation

`pl-dicom_pixel_redact` is a _[ChRIS](https://chrisproject.org/) plugin_, meaning it can
run from either within _ChRIS_ or the command-line.

## Local Usage

To get started with local command-line usage, use [Apptainer](https://apptainer.org/)
(a.k.a. Singularity) to run `pl-dicom_pixel_redact` as a container:

```shell
apptainer exec docker://fnndsc/pl-dicom_pixel_redact dicom_pixel_redact [--args values...] input/ output/
```

To print its available options, run:

```shell
apptainer exec docker://fnndsc/pl-dicom_pixel_redact dicom_pixel_redact --help
```

## Examples

`dicom_pixel_redact` requires two positional arguments: a directory containing
input data, and a directory where to create output data.
First, create the input directory and move input data into it.

```shell
mkdir incoming/ outgoing/
mv patient1.dcm patient2.dcm incoming/

apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact incoming/ outgoing/
```

By default, every `*.dcm` file found (recursively) under `incoming/` is
scanned and redacted into a matching structure under `outgoing/`, using each
file's own header (`PatientName`, `PatientID`, ...) to boost recall on top of
Presidio's general PHI detection.

A few common variations:

```shell
# only process files matching a narrower pattern
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --pattern '**/*.dicom' incoming/ outgoing/

# fill redacted regions by sampling the background instead of high-contrast black/white,
# and save the redacted bounding boxes as JSON next to each output file
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --fill background --save-bboxes incoming/ outgoing/

# rely only on Presidio's general NER model: turns off BOTH the per-file header
# deny-list built by this plugin AND Presidio's own header-derived matching
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --no-metadata-recall incoming/ outgoing/

# also copy through non-DICOM files found in the input directory
# (files that look like DICOM but did not match --pattern are refused, not copied)
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --copy-others incoming/ outgoing/
```

Run `dicom_pixel_redact --help` for the full list of options.

### Exit status and failures

The plugin is **fail-closed**: a file that cannot be redacted is never written
to the output directory. Presidio's engine copies its input into the output
location first and redacts the copy in place, so the plugin runs it in a
temporary directory and publishes the result only after it succeeded.

If any file fails, the remaining files are still processed and the plugin
exits with status `1`. Check the log for `Failed to scrub` and `Refusing`
lines. A header that can't be read only disables `--recall-tags` matching for
that file (a warning is logged); redaction still runs.

### Limitations

Read these before relying on the output for de-identification.

- **Single-frame, uncompressed images only.** Multi-frame images (cine,
  ultrasound loops) and compressed transfer syntaxes (RLE, JPEG, JPEG 2000...)
  are refused up front, reported as failures and not written to the output.
  Multi-frame is where burned-in PHI is most common, so for those inputs this
  plugin currently does not help; they must be handled another way.
  Compression is refused because re-compression through GDCM can abort the
  whole process on some inputs.
- **Files without pixel data** (e.g. structured reports) fail and are not
  output.
- **OCR is best-effort.** Not finding text is not proof an image is clean. On
  small images (e.g. 256x256) Tesseract can miss lines entirely; the same text
  on a larger image may be fully found. Spot-check output for each new scanner
  or image type.
- **Dates:** header recall uses stored values like `19700102`. Burned-in dates
  are rarely written that way, so header matching will not catch a date of
  birth; that relies on the NER model.
- **Thumbnails:** only the top-level `PixelData` is redacted. If an
  `IconImageSequence` (0088,0200) is present, the plugin removes it from the
  output.
- **File matching:** `--pattern` defaults to the case-sensitive `**/*.dcm`.
  PACS exports named `IM0001`, `x.DCM` or `x.dicom` are not processed. Without
  `--copy-others` they are ignored; with it they are refused and the run fails.
  Use e.g. `--pattern '**/*'` to process everything.
- **Header tags are not touched** (see the scope note above).
- **Memory:** the spaCy `en_core_web_lg` model needs roughly 1 GB; the plugin
  requests 2 GiB.

## Development

Instructions for developers.

### Building

Build a local container image:

```shell
docker build -t localhost/fnndsc/pl-dicom_pixel_redact .
```

### Running

Mount the source code `dicom_pixel_redact.py` into a container to try out changes without rebuild.

```shell
docker run --rm -it --userns=host -u $(id -u):$(id -g) \
    -v $PWD/dicom_pixel_redact.py:/usr/local/lib/python3.12/site-packages/dicom_pixel_redact.py:ro \
    -v $PWD/in:/incoming:ro -v $PWD/out:/outgoing:rw -w /outgoing \
    localhost/fnndsc/pl-dicom_pixel_redact dicom_pixel_redact /incoming /outgoing
```

## Testing

### Install development dependencies

```bash
pip install -r requirements.txt
pip install -e .
pip install pytest
```

### Run the test suite

```bash
pytest -v
```

or run a specific test:

```bash
pytest tests/test_recall.py -v
```

### Test inside Docker

```bash
docker build --build-arg extras_require=dev -t pl-dicom_pixel_redact:dev .
docker run --rm \
    -v "$PWD:/app:ro" \
    -w /app \
    pl-dicom_pixel_redact:dev \
    pytest -v -o cache_dir=/tmp/pytest
```


Tests are split into two groups:

- `tests/test_recall.py`, `tests/test_cli.py`, `tests/test_failures.py` — pure
  unit tests covering the header-recall logic, argument parsing, and failure
  handling (corrupt files, fail-closed output, exit codes, recall fallback;
  the Presidio engine is faked). No OCR, no spaCy model, no
  Tesseract; fast, and run in any environment.
- `tests/test_integration.py` — runs the plugin end-to-end against synthetic
  DICOMs with burned-in PHI text, through the real Tesseract + spaCy +
  Presidio pipeline. It first checks that OCR can read the text in the *input*
  (positive control), then that the redacted pixels no longer OCR back to the
  original name/ID, and that failing inputs leave nothing in the output. It skips itself (rather than failing) if `tesseract` or
  the `en_core_web_lg` spaCy model aren't available, so it degrades
  gracefully outside the `:dev` image.

## Release

Steps for release can be automated by [Github Actions](.github/workflows/ci.yml).
This section is about how to do those steps manually.

### Increase Version Number

Increase the version number in `dicom_pixel_redact.py` (`__version__`) and commit this file.

### Push Container Image

Build and push an image tagged by the version. For example, for version `1.2.3`:

```
docker build -t docker.io/fnndsc/pl-dicom_pixel_redact:1.2.3 .
docker push docker.io/fnndsc/pl-dicom_pixel_redact:1.2.3
```

### Get JSON Representation

Run [`chris_plugin_info`](https://github.com/FNNDSC/chris_plugin#usage)
to produce a JSON description of this plugin, which can be uploaded to _ChRIS_.

```shell
docker run --rm docker.io/fnndsc/pl-dicom_pixel_redact:1.2.3 chris_plugin_info -d docker.io/fnndsc/pl-dicom_pixel_redact:1.2.3 > chris_plugin_info.json
```

Intructions on how to upload the plugin to _ChRIS_ can be found here:
https://chrisproject.org/docs/tutorials/upload_plugin

