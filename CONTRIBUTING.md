# Contributing

Thanks for your interest! Bug reports, camera reports and pull requests are welcome.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to
uphold it.

## Reporting

- **Bugs and questions:** open an issue. Include your camera brand/model, the config `type`,
  and the error message. **Never paste IP addresses, usernames or passwords.**
- **Security problems:** see [SECURITY.md](SECURITY.md); please report them privately.
- **A camera that works (or doesn't):** an issue or a PR adding a page under
  [`docs/cameras/`](docs/cameras/) helps others with the same model.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) and ffmpeg.

```
uv sync
uv run pytest                                   # unit tests, no network
uv run ruff check && uv run ruff format --check
```

CI runs exactly these on every pull request; please make sure they pass.

### Live tests

`uv run pytest -m live` runs against a real camera from your own config. These tests
**physically move the camera**, save snapshots and a short clip, and create and delete a
preset and a baseline named `cmcp_test`. They never run in CI. Mention in your PR whether you
ran them, and against which camera.

## Workflow: issue first

Every change starts with an issue, also for the maintainer:

1. **Open an issue** (or comment on an existing one) describing the problem or idea. For larger
   changes, wait for a short go-ahead so no work is wasted.
2. **Branch** from `main`, e.g. `12-short-description`.
3. **Open a pull request** whose description contains `Closes #12`. A check named
   `linked-issue` fails pull requests that don't close an issue.
4. **CI** (`test`) must pass. Pull requests are **squash-merged**, and the issue closes
   automatically.
5. Merged pull requests appear in the next **release notes**, with your name.

### Pull request guidelines

- Keep changes focused; one topic per PR.
- Add or update tests for behaviour changes.
- Follow the existing style (ruff, line length 100).
- Credentials must never reach the model, logs or error messages; keep the existing tests for
  this passing and add one if you touch those paths.
- Labels decide the release-notes section: `enhancement`, `bug`, `documentation`,
  `dependencies`; `skip-changelog` leaves a PR out.

## Releases (maintainer)

Versions follow [Semantic Versioning](https://semver.org). To release:

1. In a pull request (with its issue), bump `version` in `pyproject.toml` and run `uv lock`.
2. After merging: `git switch main && git pull && git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The `Release` workflow checks that the tag matches the version, runs the tests, builds the
   wheel and sdist, and publishes a GitHub Release with generated notes.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
