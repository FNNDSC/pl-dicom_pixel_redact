# Maintenance

Housekeeping for `pl-dicom_pixel_redact` that isn't plugin logic: dependencies,
the SBOM, vulnerability scanning, and how to cut a release. Plugin usage and
development are in [README.md](README.md).

## Dependencies

| File | Role |
|---|---|
| `requirements.txt` | Runtime dependencies, installed in the Docker image with `pip install -r`. Compatible-release ranges (`~=`), **not** a full lock. |
| `setup.py` | `install_requires=['chris_plugin']`; extras `dev` adds `pytest`. |
| `Dockerfile` | Also installs the `en_core_web_lg` 3.8.0 spaCy model and the `tesseract-ocr` apt package. |

Because `requirements.txt` uses ranges, two builds on different days can
resolve different transitive versions (e.g. `presidio-image-redactor~=0.0.53`
currently resolves to 0.0.60). Treat every rebuild as a dependency change:
run the full test suite (below) before releasing.

### Upgrading dependencies

1. Edit the range in `requirements.txt`.
2. Build the dev image and run all tests, including the integration tests
   that need Tesseract and spaCy (they skip outside the image, so a green run
   on a bare machine proves little):

   ```shell
   docker build -t localhost/fnndsc/pl-dicom_pixel_redact:dev --build-arg extras_require=dev .
   docker run --rm localhost/fnndsc/pl-dicom_pixel_redact:dev pytest -rs
   ```

   Check the `-rs` output: **no test should be skipped** inside the image.
3. Run the vulnerability scan and regenerate the SBOM (below).

Watch-list:

- **pydicom** is `~=2.4`; pydicom 3.x is not yet validated with this plugin.
- **cryptography** is not used directly. It arrives via presidio's optional
  Azure OCR backend, which this plugin never calls (it uses Tesseract). Scans
  may still flag it; see Vulnerability scanning.
- **spaCy model** is pinned by wheel URL in the `Dockerfile`. If spaCy is
  upgraded, confirm the model version is still compatible and update both the
  Dockerfile and this file.

## Vulnerability scanning

```shell
pip install pip-audit
pip-audit -r requirements.txt
```

Run after any dependency change and roughly monthly. For each finding:
check for a fixed version; if the package is a direct dependency bump its
range in `requirements.txt`; if it is transitive, add an explicit line for it
with a comment saying why; then rebuild, retest, re-scan, regenerate the SBOM.

## SBOM

`sbom.cdx.json` is a CycloneDX 1.6 inventory of what is installed at runtime.
Regenerate it **from the built image** so it matches what ships:

```shell
pip install cyclonedx-bom
python -m venv /tmp/sbom-env
/tmp/sbom-env/bin/pip install -r requirements.txt
/tmp/sbom-env/bin/pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
/tmp/sbom-env/bin/pip install --no-deps .
cyclonedx-py environment --output-reproducible --of json -o sbom.cdx.json /tmp/sbom-env/bin/python
```

Regenerate on every dependency change and before each release.

**Known gap:** the checked-in SBOM lists `pydicom 3.0.2`, while
`requirements.txt` allows only `~=2.4`, so it was generated from a different
environment than the Docker image. Regenerate before the next release.

## Testing

| Suite | Needs | Covers |
|---|---|---|
| `tests/test_cli.py` | nothing | argument parsing and defaults |
| `tests/test_recall.py` | nothing | header-derived deny-list logic |
| `tests/test_failures.py` | nothing (engine is faked) | corrupt files, fail-closed output, exit code, recall fallback, option pass-through |
| `tests/test_integration.py` | Tesseract, `en_core_web_lg` | real OCR → redaction on synthetic DICOMs |

CI runs everything inside the dev image, so the integration tests execute there.

## Releasing

1. Confirm CI is green on `main` and no integration test was skipped.
2. Run `pip-audit`; regenerate `sbom.cdx.json`.
3. Bump `__version__` in `dicom_pixel_redact.py` (semver: behaviour or
   exit-code changes are at least a minor bump).
4. Merge, then tag and push: `git tag v1.0.1 && git push origin v1.0.1`.
5. The tag triggers `.github/workflows/ci.yml`: it builds the image, pushes
   `latest` and the version tag to Docker Hub and ghcr.io, uploads the plugin
   to the ChRIS store (`cube.chrisproject.org`), and updates the Docker Hub
   description. Required repo secrets: `DOCKERHUB_USERNAME`,
   `DOCKERHUB_PASSWORD`, `CHRISPROJECT_USERNAME`, `CHRISPROJECT_PASSWORD`.
6. Verify: `docker run --rm fnndsc/pl-dicom_pixel_redact:<version> dicom_pixel_redact --version`
   and that the plugin appears in the store.

Manual fallback steps are in [README.md > Release](README.md#release).

## Scope reminder

The plugin redacts **pixel** data only. DICOM header tags are left intact by
design; pair with a tag-scrubbing plugin for full de-identification. OCR-based
detection is best-effort: absence of a detection is not proof that an image is
PHI-free, so spot-check output on new scanner types.

## Follow-ups

- Move to a hash-locked `requirements.txt` (pip-tools) and install with
  `--require-hashes` in the Dockerfile.
- Scheduled CI job running `pip-audit`.
- Add `examples/incoming` / `examples/outgoing` so CI's "Run examples" step
  actually runs.
