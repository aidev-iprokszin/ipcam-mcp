# Contributing

Thanks for your interest! Bug reports, camera reports and pull requests are welcome.

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

## Pull requests

- Keep changes focused; one topic per PR.
- Add or update tests for behaviour changes.
- Follow the existing style (ruff, line length 100).
- Credentials must never reach the model, logs or error messages; keep the existing tests for
  this passing and add one if you touch those paths.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
