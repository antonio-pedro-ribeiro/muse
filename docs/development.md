# Development, CI and releases

## Checks

Run the same checks as CI before you push:

```
uv sync                                        # dev tools come with the default dependency group
uv run ruff check src tests scripts            # lint (rules in pyproject.toml)
uv run pyright                                 # type check of kernel/ and sdk/
uv run --with scikit-learn pytest              # tests; hardware tests (marker `device`) are skipped
```

`uv run --with scikit-learn` adds scikit-learn only for that run, so the `train-probe` end-to-end test runs
without installing PyTorch. The CBraMod test is skipped unless the `[models]` extra is installed and the weights
are pulled.

To check a build the way CI does, install the wheel into a clean environment and run the smoke test from
outside the source tree:

```
uv build
uv venv ../smoke && uv pip install --python ../smoke dist/*.whl
cd .. && smoke/Scripts/python muse/scripts/smoke_test.py 0.1.0      # smoke/bin/python on Linux/macOS
```

The smoke test checks the version, the `muse` and `muse-check` commands, the web UI files, that every
built-in plugin registers, and an offline session with a report.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests, on pushes to `main`, on demand (*Run workflow*), and as the
first stage of a release.

| Job | Runs on | What it does |
|---|---|---|
| Lint and type check | Ubuntu | `uv sync --locked` (fails if `uv.lock` is stale), ruff, pyright |
| Tests | Ubuntu, Windows | pytest without hardware, with scikit-learn; the JUnit report is uploaded on failure |
| Build distributions | Ubuntu | `uv build` (sdist, then the wheel from the sdist), uploaded as the `dist` artifact |
| Install wheel | Ubuntu, Windows | installs that wheel into a clean environment and runs `scripts/smoke_test.py` |

macOS isn't in the matrix yet because it has never been tested there. The repository is public, so its runners
are free: add `macos-latest` to the two matrices once the app has been tried on a Mac.

Dependabot (`.github/dependabot.yml`) proposes weekly updates to the GitHub Actions and the locked Python
dependencies. It can't update OpenMuse, which is pinned by commit.

**Recommended repository setting:** protect `main` (Settings → Branches) and require the *Lint and type check*,
*Tests* and *Install wheel* checks to pass before merging.

## Releasing

1. Bump the version and commit it on `main`:

   ```
   uv version --bump minor        # or patch / major; or `uv version 0.2.0`
   git commit -am "Release 0.2.0"
   ```

2. Tag and push:

   ```
   git tag v0.2.0
   git push origin main v0.2.0
   ```

`.github/workflows/release.yml` then:

1. checks that the tag matches `version` in `pyproject.toml`;
2. runs the full CI workflow on the tagged commit;
3. creates a GitHub Release named after the tag, with release notes generated from the merged pull requests,
   and attaches the wheel, the sdist and `SHA256SUMS`. These are exactly the files CI built and installed.

A pre-release version makes a pre-release: `uv version 0.2.0rc1`, then tag `v0.2.0rc1`. The same goes for
`a`, `b` and `dev` versions.

Install a release with uv (the OpenMuse dependency is fetched from GitHub):

```
uv tool install https://github.com/antonio-pedro-ribeiro/muse/releases/download/v0.2.0/muse_monitor-0.2.0-py3-none-any.whl
```

The latest release is always at https://github.com/antonio-pedro-ribeiro/muse/releases/latest.

**Why not PyPI:** OpenMuse is a git dependency pinned by commit, and PyPI rejects packages with direct URL
dependencies. The `openmuse` package on PyPI is a different project, so depending on it by name would install
the wrong code.
