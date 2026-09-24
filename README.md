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
`DicomImageRedactorEngine`: Tesseract OCR reads text off each frame, spaCy's
NER model classifies it, and any span recognized as PHI gets redacted in
place.

On top of Presidio's default detection, this plugin also builds a **per-file
recall boost** from each DICOM's own header: since a file's `PatientName`,
`PatientID`, and similar tags are already known ground truth, an exact
burned-in match against those values is flagged even when the general NER
model wouldn't otherwise catch it (unusual name formats, MRNs, accession
numbers, etc). This is on by default and can be tuned or disabled — see
`--metadata-recall` below.

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

# rely only on Presidio's general NER model, without the per-file header recall boost
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --no-metadata-recall incoming/ outgoing/

# also copy through any non-DICOM files found in the input directory
apptainer exec docker://fnndsc/pl-dicom_pixel_redact:latest dicom_pixel_redact \
    --copy-others incoming/ outgoing/
```

Run `dicom_pixel_redact --help` for the full list of options.

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

### Testing

Run unit tests using `pytest`.
It's recommended to rebuild the image to ensure that sources are up-to-date.
Use the option `--build-arg extras_require=dev` to install extra dependencies for testing.

```shell
docker build -t localhost/fnndsc/pl-dicom_pixel_redact:dev --build-arg extras_require=dev .
docker run --rm -it localhost/fnndsc/pl-dicom_pixel_redact:dev pytest
```

Tests are split into two groups:

- `tests/test_recall.py`, `tests/test_cli.py` — pure unit tests covering the
  header-recall logic and argument parsing. No OCR, no spaCy model, no
  Tesseract; fast, and run in any environment.
- `tests/test_integration.py` — runs the plugin end-to-end against synthetic
  DICOMs with burned-in PHI text, through the real Tesseract + spaCy +
  Presidio pipeline, and checks the redacted pixels no longer OCR back to the
  original name/ID. It skips itself (rather than failing) if `tesseract` or
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

