# Maintenance

Housekeeping for `pl-dicom_pixel_redact` that isn't plugin logic: dependencies,
the SBOM, vulnerability scanning, and how to cut a release. Plugin usage and
development are in [README.md](README.md).

## Dependencies

| File | Role |
|---|---|
| `requirements.txt` | Runtime dependencies, installed in the Docker image with `pip install -r`. Direct dependencies are pinned exactly; transitive ones are not (no hashes), so it is **not** a full lock. |
| `setup.py` | `install_requires` lists the runtime packages (unpinned); extras `dev` adds `pytest`. |
| `Dockerfile` | Also installs the `en_core_web_lg` 3.8.0 spaCy model and the `tesseract-ocr` apt package. |

The five direct dependencies are pinned to exact versions
(`chris_plugin`, `presidio-analyzer`, `presidio-image-redactor`, `pydicom`,
`Pillow`). Their transitive dependencies (spaCy, python-gdcm, numpy, ...) are
still resolved at build time, so two builds on different days can differ.
Treat every rebuild as a dependency change: run the full test suite (below)
before releasing.

### Upgrading dependencies

1. Change the pin in `requirements.txt`.
2. Build the dev image and run all tests, including the integration tests
   that need Tesseract and spaCy (they skip outside the image, so a green run
   on a bare machine proves little):

   ```shell
   docker build -t localhost/fnndsc/pl-dicom_pixel_redact:dev --build-arg extras_require=dev .
   docker run --rm -v "$PWD:/app:ro" -w /app localhost/fnndsc/pl-dicom_pixel_redact:dev pytest -rs
   ```

   The mount is needed because the image does not contain `tests/`. Check the
   `-rs` output: **no test should be skipped** inside the image.
3. Run the vulnerability scan and regenerate the SBOM from the image (below).

Watch-list:

- **pydicom** is pinned to 2.4.5; pydicom 3.x is not yet validated with this plugin.
- **python-gdcm** is native code used by Presidio to re-encode compressed
  pixel data as RLE Lossless. Its RLE encoder aborts on `linux/arm64` builds
  (reported with python-gdcm 3.0.24, 3.2.1 and 3.2.6), which is why the plugin
  probes it at run time (`gdcm_rle_encoder_works`). Upgrade it deliberately and
  re-run the compressed integration tests. `ci.yml` publishes only
  `linux/amd64`; if you enable `linux/arm64`, expect compressed input to be
  refused on that image until the encoder is fixed.
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
pin in `requirements.txt`; if it is transitive, add an explicit line for it
with a comment saying why; then rebuild, retest, re-scan, regenerate the SBOM.

## SBOM

`sbom.cdx.json` should be a software bill of materials of **what is in the
shipped image**, including OS packages such as `tesseract-ocr`, not just
Python packages.

Generate it from the built image, not from a separate virtualenv (a venv
resolves different versions and records local paths):

```shell
# whole image: Python packages and Debian packages (Tesseract, ...)
syft docker.io/fnndsc/pl-dicom_pixel_redact:<version> -o cyclonedx-json=sbom.cdx.json
```

Better still, produce it in CI on every tag, for example with
`sbom: true` on `docker/build-push-action` or a `syft`/`trivy` step on the
pushed tag, and attach it to the release.

**Current file:** `sbom.cdx.json` contains the Python dependency snapshot
supplied with this patch: 76 package components, exact direct dependency
versions matching `requirements.txt`, no local source paths, and the plugin
as the application component. Its generation environment and correspondence
to a shipped image have not been independently verified. It omits OS packages
such as `tesseract-ocr` and `libgl1` and is not a complete image SBOM.
Regenerate it from the actual release image using `syft` as above before
relying on it for vulnerability or deployment decisions.

## Testing

| Suite | Needs | Covers |
|---|---|---|
| `tests/test_cli.py` | nothing | argument parsing and defaults |
| `tests/test_recall.py` | nothing | header-derived deny-list logic |
| `tests/test_failures.py` | nothing (engine is faked) | fail-closed output, unsupported input (multi-frame), compressed input accepted, exit codes, `--copy-others` refusals, option validation, recall wiring |
| `tests/test_integration.py` | Tesseract, `en_core_web_lg` | real OCR → redaction on synthetic DICOMs (incl. RLE-compressed 8/16-bit), and real-engine failure cases |

The fake engine in `test_failures.py` deliberately copies the input into its
output directory *before* failing, like Presidio does. Keep it that way: a
fake that fails before writing cannot detect an un-redacted leak.

`test_cli.py` also runs `chris_plugin.parameters.serialize()` on the parser. An
argparse action `chris_plugin` does not support (e.g. `BooleanOptionalAction`)
would otherwise only fail during the release, after the image was pushed.

CI runs everything inside the dev image, so the integration tests execute there.

## Releasing

1. Confirm CI is green on `main` and no integration test was skipped.
2. Run `pip-audit`; regenerate the SBOM from the image.
3. Make sure `__version__` in `dicom_pixel_redact.py` is the version you are
   about to tag. The ChRIS descriptor takes its version from it, so the tag
   must match (`__version__ = '1.0.0'` → tag `v1.0.0`) or the store version and
   image tag will disagree. Bump it only for versions after one has shipped.
4. Merge, then tag and push: `git tag v1.0.0 && git push origin v1.0.0`.
5. The tag triggers `.github/workflows/ci.yml`: it builds the image, pushes
   `latest` and the version tag to Docker Hub and ghcr.io, uploads the plugin
   to the ChRIS store (`cube.chrisproject.org`), and updates the Docker Hub
   description. Required repo secrets: `DOCKERHUB_USERNAME`,
   `DOCKERHUB_PASSWORD`, `CHRISPROJECT_USERNAME`, `CHRISPROJECT_PASSWORD`.
6. Verify: `docker run --rm fnndsc/pl-dicom_pixel_redact:<version> dicom_pixel_redact --version`
   and that the plugin appears in the store.

Manual fallback steps are in [README.md > Release](README.md#release).

## Scope reminder

See README > Limitations: multi-frame images are refused; compressed input
is re-encoded as RLE Lossless, with native codec crash risks (including the
reported 16-bit RLE SIGABRT on linux/arm64); OCR is best-effort. The plugin redacts
**pixel** data only. DICOM header tags are left intact by
design; pair with a tag-scrubbing plugin for full de-identification. OCR-based
detection is best-effort: absence of a detection is not proof that an image is
PHI-free, so spot-check output on new scanner types.

## Follow-ups

- Move to a hash-locked `requirements.txt` (pip-tools) and install with
  `--require-hashes` in the Dockerfile.
- Scheduled CI job running `pip-audit`.
- Multi-frame support (redact frame by frame).
- Run each file in a subprocess so a native crash in a codec (GDCM) cannot
  take down the whole batch; also verify codecs other than RLE (JPEG, JPEG 2000,
  JPEG-LS) in the integration tests.
- Add `examples/incoming` / `examples/outgoing` so CI's "Run examples" step
  actually runs.
