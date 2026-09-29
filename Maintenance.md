# Maintenance

This document covers the parts of keeping `pl-dicom_pixel_redact` healthy
that aren't about the plugin's own logic: dependency pinning, the SBOM, and
vulnerability scanning. For releasing a new version of the plugin itself,
see [README.md > Release](README.md#release).

## Dependency pinning

Dependencies are managed in two tiers, using [pip-tools](https://pip-tools.readthedocs.io/):

| File | Purpose | Edit by hand? |
|---|---|---|
| `requirements.in` | Direct runtime dependencies, loosely versioned | Yes |
| `requirements-dev.in` | Direct test-only dependencies, constrained to the runtime lock | Yes |
| `requirements.txt` | Fully resolved, hash-pinned lock of `requirements.in` + every transitive dependency | No — generated |
| `requirements-dev.txt` | Same, for `requirements-dev.in` | No — generated |

`requirements.txt` is what actually gets installed in the Docker image
(`pip install --require-hashes -r requirements.txt`); `requirements-dev.txt`
is installed on top of it for `--build-arg extras_require=dev` builds. Both
carry a `--hash` for every package, so a build fails loudly if a package
index ever serves something that doesn't match what was reviewed —
supply-chain integrity, not just version pinning.

`setup.py`'s `install_requires` intentionally stays loose (unpinned package
names only). It exists so `pip install .` resolves sensibly for someone
installing outside Docker; inside the image, the local package is installed
with `pip install --no-deps .` specifically so it can't silently re-resolve
and drift away from the hash-pinned versions already installed from
`requirements.txt`.

### Regenerating the lockfiles

Install pip-tools, then recompile from the `.in` files:

```shell
pip install pip-tools

pip-compile --generate-hashes --allow-unsafe --strip-extras \
    requirements.in -o requirements.txt

pip-compile --generate-hashes --allow-unsafe --strip-extras \
    requirements-dev.in -o requirements-dev.txt
```

(`--allow-unsafe` is needed because `setuptools` — normally excluded from
pins — is a real transitive dependency here, of `spacy`/`thinc`.)

Do this whenever:

- a version constraint in `requirements.in` / `requirements-dev.in` changes
- `pip-audit` (below) flags a vulnerable package and a fixed version exists
- on a regular cadence (e.g. monthly) to pick up patch releases, even
  without a specific trigger

After regenerating, **reinstall into a clean environment and run the test
suite** before committing — a resolver change can shift a transitive
dependency in ways that only show up at runtime:

```shell
python -m venv /tmp/verify && /tmp/verify/bin/pip install --upgrade pip
/tmp/verify/bin/pip install --require-hashes -r requirements.txt
/tmp/verify/bin/pip install --require-hashes -r requirements-dev.txt
/tmp/verify/bin/pip install --no-deps -e .
/tmp/verify/bin/pip check
/tmp/verify/bin/python -m pytest tests/ -v
```

Or equivalently, build the `:dev` image and run tests inside it (see
[README.md > Testing](README.md#testing)) — that's the same install path
the production image uses.

### Transitive pins worth knowing about

`requirements.in` includes a couple of constraints that aren't just "latest
compatible version":

- **`pydicom>=2.4,<4`** — the plugin (and its tests) use pydicom 2.x/3.x
  APIs that pydicom itself has marked deprecated for removal in 4.0. Capped
  until that migration is done deliberately, not as a side effect of a
  routine `pip-compile` run.
- **`cryptography>=50.0.0`** — not a direct dependency of this plugin at
  all. It's pulled in transitively via `presidio-image-redactor`'s optional
  Azure Form Recognizer OCR backend (`azure-ai-formrecognizer` →
  `msrest`/`azure-core` → `cryptography`), which this plugin never invokes
  (it uses Tesseract). The version that `presidio-anonymizer` was happy to
  resolve against had known CVEs (PYSEC-2026-3552/3553/3554, certificate
  chain verification issues) — see Vulnerability scanning below. Pinned up
  to a patched version; re-check whether this pin is still necessary next
  time `presidio-anonymizer` is upgraded.

## SBOM

`sbom.cdx.json` / `sbom.cdx.xml` are [CycloneDX](https://cyclonedx.org/)
1.6 Software Bills of Materials listing every package actually installed at
runtime (the resolved `requirements.txt` set, plus the `en_core_web_lg`
spaCy model — both ship in the image; dev-only tooling is deliberately
excluded so the SBOM reflects what's actually shipped, not what's used to
build it).

### Regenerating

Build from a clean venv containing *only* the runtime install (mirroring
what's in the final image), then point
[cyclonedx-py](https://github.com/CycloneDX/cyclonedx-python) at that venv's
interpreter:

```shell
pip install cyclonedx-bom

python -m venv /tmp/sbom-env
/tmp/sbom-env/bin/pip install --require-hashes -r requirements.txt
/tmp/sbom-env/bin/pip install "https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl"
/tmp/sbom-env/bin/pip install --no-deps .

cyclonedx-py environment --output-reproducible --of json -o sbom.cdx.json /tmp/sbom-env/bin/python
cyclonedx-py environment --output-reproducible --of xml  -o sbom.cdx.xml  /tmp/sbom-env/bin/python
```

`environment` mode (rather than `requirements` mode) is used deliberately:
it inspects what's *actually installed*, including licenses and exact
resolved transitive versions, rather than just re-parsing the lockfile
text.

Regenerate whenever `requirements.txt` changes (new lock, dependency
upgrade) or the spaCy model version changes.

## Vulnerability scanning

```shell
pip install pip-audit
pip-audit -r requirements.txt --strict
```

Run this:

- after every lockfile regeneration
- periodically on the existing lock, since new CVEs get published against
  already-pinned versions

If it reports a vulnerable package:

1. Check whether a fixed version is available (`pip-audit` prints
   `fix_versions`).
2. If the vulnerable package is a *direct* dependency in `requirements.in`,
   bump its constraint there.
3. If it's transitive (as with `cryptography` above), add an explicit
   line for it in `requirements.in` with a comment explaining why, pinning
   to the fixed version — pip-compile's resolver will pull it in even
   though nothing in `requirements.in` imports it directly.
4. Regenerate both lockfiles, reinstall clean, rerun `pytest`, rerun
   `pip-audit` to confirm the finding is gone, then regenerate the SBOM.

As of the last scan (see `sbom.cdx.json`'s `metadata.timestamp`),
`pip-audit -r requirements.txt --strict` reports **no known
vulnerabilities**.

## Suggested CI additions

The `ci.yml` workflow currently builds the `:dev` image and runs `pytest`
inside it on every push/PR. Consider adding a scheduled job that runs
`pip-audit -r requirements.txt` against the checked-in lock (catches newly
published CVEs against already-pinned versions, not just ones introduced by
a lockfile change) and opens an issue or fails loudly if something turns
up.