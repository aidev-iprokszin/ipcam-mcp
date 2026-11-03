# ipcam-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude Code see through and control
the IP cameras on your local network (ONVIF + RTSP): snapshots, video clips, pan/tilt and
change detection, all handled locally.

Any ONVIF camera should work. See [known cameras](docs/cameras/) for what has been tested.

## Tools

| Tool | What it does |
|---|---|
| `list_cameras(include_removed=False)` | Configured cameras with their uid, scene and capabilities (`camera`, `ptz`); `include_removed=true` also lists removed cameras whose files are still on disk |
| `camera_snapshot(camera=None, quality="high", crop=None, region=None, from_path=None)` | Returns the picture (≤1280 px wide) and saves the full-size JPEG. `crop=[x, y, w, h]` (fractions 0–1) or `region` (`top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`, `bottom`, `bottom-right`) returns a detail at full resolution. `from_path` reuses an earlier snapshot (for example one from `scene_timeline`) without contacting a camera; `camera` is then optional |
| `camera_save_baseline(camera, name, quality="high")` | Takes a snapshot and keeps it as a named reference (e.g. `door-closed`) |
| `camera_compare(camera, baseline=None, quality="high")` | Takes a snapshot and reports what changed against the camera's previous snapshot in its current scene, or against a named baseline saved in that scene |
| `camera_record(camera, seconds=10, quality="high")` | Records 1–60 s to an mp4, returns its path |
| `ptz_move(camera, direction, amount="small")` | Pan/tilt `left/right/up/down` by `small/medium/large`; waits for the move to finish and returns the final position if the camera reports one |
| `ptz_list_presets(camera)` | Saved positions |
| `ptz_goto_preset(camera, preset)` | Move to a preset by name or token |
| `ptz_save_preset(camera, name, overwrite=False)` | Saves the current position as a preset; refuses an existing name unless `overwrite=true` |
| `ptz_remove_preset(camera, preset)` | Deletes a preset by name or token (cannot be undone) |
| `list_scenes()` | Every scene with its cameras, removed cameras that watched it, the first and last capture and counts |
| `scene_snapshot(scene, quality="high")` | A snapshot from every camera in the scene, each labelled with the camera's name; a camera that fails reports its error without failing the others |
| `scene_compare(scene, baseline=None, quality="high")` | `camera_compare` for every camera in the scene, labelled the same way |
| `scene_timeline(scene, start=None, end=None, limit=50)` | The scene's snapshots, recordings and baselines, newest first, from every camera that watched it (removed ones too). `start`/`end`: `YYYY-MM-DD` (whole day) or an ISO datetime; `limit` 1–500 |

`camera` is a camera's name: its section name in `cameras.toml`. `quality="high"` uses the
camera's main stream (1080p), `"low"` the sub stream (smaller, quicker). Media is saved to the
camera's own folder, `<media_dir>/<uid>/<camera>_<YYYYmmdd-HHMMSS>.jpg|.mp4` (`media_dir`
defaults to `~/.local/share/ipcam-mcp/media`).

**Compare and crop.** `camera_compare` lists the grid cells that changed (the same names as
`region`) and a box around them. Pass a changed cell as `region` to `camera_snapshot`, ideally
with `from_path` set to the snapshot you just looked at, to see it closer. Baselines live in
`<media_dir>/<uid>/baselines/` as a JPEG plus a small JSON file.

**Camera uids.** Every camera has a permanent `uid` (8 characters, `a-z0-9`) in its
`cameras.toml` section. The server fills it in when it is missing, so you never write one
yourself; don't change or copy it. The uid names the camera's media folder, which also holds a
`camera.json` with the camera's current name. Because of it, a renamed camera keeps its
snapshots and baselines, and a new camera that reuses an old name starts with an empty history.
When you upgrade from 0.2, the first start moves existing snapshots, recordings and baselines
from the old flat layout into these folders; files of cameras that are no longer configured get
a folder of their own and are kept.

**Scenes.** A scene is the place a camera watches, such as `front-door` or `garage`. By default
it is the camera's name; set `scene = "..."` in the camera's section (or use `set_scene`) when
they differ, for example when several cameras watch one place. Renaming a camera keeps its
scene. Moving a camera to another scene starts a new period in its `camera.json`: snapshots and
baselines from before the move stay with the old scene, and `camera_compare` only compares within
the current one, so it never reports a different place as a change.

**Replacing a camera.** When a camera enters a scene where a removed camera was last (with
`add_camera`, or `set_scene`), you are asked in a form whether it replaces one of them, is an
additional camera, or to cancel. A replacement is its *successor*: until it has a snapshot of
its own there, `camera_compare` compares with the old camera's newest snapshot in the scene, and
baselines are looked up along the predecessors (up to 10). The result then says which camera
took the reference, since angle, lens or resolution may differ. Without form support the camera
is added as an additional one; call `set_scene` with its current scene later, from a client that
shows forms, to declare it a successor. `delete_camera` mentions when a camera is another's
predecessor.

## Prerequisites

- Linux or WSL2, on the same network as the camera
- [uv](https://docs.astral.sh/uv/) on your `PATH` (it installs Python 3.13+ and the dependencies
  for you)
- ffmpeg: `sudo apt install ffmpeg`
- An ONVIF-capable IP camera with a local camera account (username and password), with ONVIF
  and RTSP enabled
- [Claude Code](https://claude.com/claude-code) or another MCP client that supports stdio
  servers

## Setup

1. **Clone and install**:
   ```
   git clone https://github.com/aidev-iprokszin/ipcam-mcp.git && cd ipcam-mcp && uv sync
   ```
2. **Camera account**: create or find the camera's local ONVIF/RTSP account (username and
   password). This is **not** your cloud login. See [known cameras](docs/cameras/) for where
   your brand hides it.
3. **Secrets file** (outside the repo), e.g. `~/.config/ipcam-mcp/secrets.env`:
   ```
   CAMERA_IP=192.168.x.y
   CAMERA_USER=...
   CAMERA_PASSWORD=...
   ```
   Make it readable only by you: `chmod 600 <file>`. The server warns at startup if other
   users can read it.
4. **Camera config**: copy `cameras.example.toml` to `~/.config/ipcam-mcp/cameras.toml` (or
   point `IPCAM_MCP_CONFIG` at another path) and adjust the camera name / variable names. Only
   variable *names* go in this file, never the secrets themselves. Use `type = "onvif"`.

   Steps 3 and 4 are optional if you let Claude add cameras: without a config file the server
   starts with no cameras, and `add_camera` (see [Managing cameras](#managing-cameras))
   creates `cameras.toml` and the camera's secrets file for your first camera.
5. **Use it in Claude Code**: the repo ships a project-scoped `.mcp.json`, so running `claude`
   anywhere inside this repo offers the `ipcam` server. Approve it when prompted. If you
   rejected it by mistake, run `claude mcp reset-project-choices` and start `claude` again. Then
   ask Claude e.g. "list my cameras" or "take a snapshot of livingroom".

   To use it from **every** folder, also register it in your user scope (inside the repo the
   project entry takes precedence, so there are no duplicates):
   ```
   claude mcp add ipcam -s user -- uv --directory /absolute/path/to/ipcam-mcp run ipcam-mcp
   ```

### Camera settings

All optional, set in the camera's section of `cameras.toml`. A wrong type is reported when the
config loads.

| Setting | Type | Default |
|---|---|---|
| `onvif_port` | integer 1–65535 | 80 |
| `rtsp_port` | integer 1–65535 | the port in the camera's stream URI, else 554 (a value you set always wins) |
| `invert_pan` | boolean | false |
| `ptz` | boolean | false |
| `stream_high` | path starting with `/` | looked up over ONVIF |
| `stream_low` | path starting with `/` | looked up over ONVIF |

`ptz = false` means the camera has no pan/tilt tools. Set `stream_high`/`stream_low` if the
ONVIF lookup fails or picks the wrong stream. Set `invert_pan = true` if `ptz_move left` turns
the view to the right. Some brands need other values; see [known cameras](docs/cameras/).

### Other MCP clients

Any client that supports stdio servers (Cursor, VS Code, Codex CLI, …) can run the same
command: `uv --directory /absolute/path/to/ipcam-mcp run ipcam-mcp`.

**Claude Desktop on Windows with the repo in WSL** (untested): add to
`%APPDATA%\Claude\claude_desktop_config.json` and restart the app:
```json
{
  "mcpServers": {
    "ipcam": {
      "command": "wsl.exe",
      "args": ["-e", "/home/<you>/.local/bin/uv", "--directory",
               "/home/<you>/path/to/ipcam-mcp", "run", "ipcam-mcp"]
    }
  }
}
```

### WSL2 networking

If the camera can't be reached from WSL (`nc -zv <ip> 554` fails), enable mirrored networking
in `%UserProfile%\.wslconfig`:
```
[wsl2]
networkingMode=mirrored
```
then run `wsl --shutdown` from Windows.

## Managing cameras

Instead of editing `cameras.toml` by hand you can let Claude add, rename, remove and delete cameras.
This is on by default. To turn it off, set `allow_management = false` in `cameras.toml` (see
`cameras.example.toml`), then run `/mcp reconnect ipcam`; the flag is only read at startup.
Claude Code still asks before each tool call unless you allowlist it; if you allowlist the
camera tools, consider leaving `remove_camera`, `delete_camera` and `ptz_remove_preset` off the list.

`add_camera` logs into whatever host it is given. Before you type a password into the login form,
check that the IP it shows is your camera's (from the camera's app or your router), not one the
agent picked up elsewhere.

| Tool | What it does |
|---|---|
| `add_camera(name, host, credentials_via="form", onvif_port=None, scene=None)` | Verifies the login over ONVIF (tries port 80, then 2020, unless you give `onvif_port`), detects pan/tilt, then adds the camera with a new uid. `name` and `scene` are lowercase letters, digits, `-`, `_` (max 32); `scene` defaults to the name |
| `rename_camera(old, new)` | Changes a camera's name; its uid, scene, baselines, snapshots and recordings stay with it (no files move), and its secrets file is renamed too if the tool created it |
| `set_scene(camera, scene)` | Moves a camera to another scene (the place it watches). Earlier snapshots and baselines stay with the earlier scene. If removed cameras watched the scene, a form asks whether it replaces one of them (see *Replacing a camera*); with the current scene it asks that again if no predecessor is declared yet |
| `remove_camera(name)` | Removes the camera from the config; keeps its snapshots, recordings and baselines (it becomes a *removed camera*) |
| `delete_camera(name)` | Deletes the camera (config entry, its managed secrets file) and its folder with all snapshots, recordings and baselines, or the leftover folder of a removed camera with that name. **You** approve it in a form listing what will be deleted; with several matches you pick one or all. Without form support nothing is deleted and the result says what to delete by hand |
| `reload_cameras()` | Always available. Re-reads `cameras.toml` after a manual edit and lists the cameras |

Changes apply immediately, without restarting the server. If loading the new config fails, the
previous `cameras.toml` is restored.

**IP address.** Network discovery is not supported (WSL2 NAT blocks it), so tell Claude the
camera's IP from the camera's app or your router.

**Credentials never pass through the model.** Two routes:

- **Form** (default): your client shows a login form. MCP forms have no masked field, so the
  password is visible while you type it, but it is not stored in the conversation. Clients on
  MCP protocol 2026-07-28 cannot show the form mid-call, and neither can some phone (Remote
  Control) views; there `add_camera` always uses the secrets-file route and says so in its
  result.
- **File** (`credentials_via="file"`): `add_camera` creates `<secrets_dir>/<name>.env` (mode
  `0600`, default `secrets_dir` is `~/.config/ipcam-mcp/secrets`) with `HOST=`, empty `USER=` and
  `PASSWORD=`. Fill in USER and PASSWORD yourself, then ask Claude to call `add_camera` again
  with the same name and host, **keeping `credentials_via="file"`**. Calling it with the default
  `form` would ask for the credentials again instead of reading the file.

Each camera added this way has its own `<name>.env`, and files at `<secrets_dir>/<name>.env` are
treated as managed: renamed and deleted along with the camera. Keep hand-made files elsewhere;
those, such as a shared `secrets.env`, are never renamed or deleted. Secrets files are read literally: `${VAR}` in a
value is not expanded.

## Transports

`ipcam-mcp` uses stdio by default. `ipcam-mcp --transport http [--host 127.0.0.1] [--port 8765]`
serves streamable HTTP, but **without authentication**, so only use it on a trusted machine
and keep it bound to localhost. On `127.0.0.1`, `localhost` or `::1` the server rejects
requests addressed to other hosts, such as from web pages (DNS-rebinding protection). Any other
`--host` is refused unless you also pass `--allow-remote`; then anyone who can reach the port
controls your cameras.

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

- Credentials are read only from your secrets file, never from the repo, and are not put into
  environment variables.
- They are kept in memory by the camera's config and adapter, and are excluded from `repr`.
- The password is scrubbed (raw and URL-encoded) from every error message the server returns
  or logs.
- RTSP URLs are masked (`rtsp://***@host…`), which also hides the username. The username is
  not scrubbed anywhere else.
- Logs go to stderr; stdout carries only the MCP protocol.

## Troubleshooting

- **Auth errors / `401` / ONVIF error on every PTZ call**: use the camera's local account, not a
  cloud login. See [known cameras](docs/cameras/).
- **Snapshots fail but ONVIF works**: set `stream_high`/`stream_low` (see
  [Camera settings](#camera-settings)).
- **`timed out … (camera unreachable?)`**: the camera is offline or not reachable from this
  machine. See [WSL2 networking](#wsl2-networking).
- **`ffmpeg not installed`**: `sudo apt install ffmpeg`.

## Known limitations

- While the camera is offline, failed ONVIF connection attempts aren't cleaned up, and you may
  see `Unclosed client session` warnings on stderr. They're harmless.
- Filenames have one-second resolution; a second capture in the same second gets a `-2`, `-3`, …
  suffix.
- Media files are never deleted. Clean up `~/.local/share/ipcam-mcp/media/` yourself.
- HTTP transport has no authentication (see above).

## Upgrading from 0.2

- On the first start, every camera in `cameras.toml` gets a permanent `uid = "..."` line, and
  existing snapshots, recordings and baselines move from the flat layout into per-camera folders,
  `<media_dir>/<uid>/`. Files of cameras that are no longer configured are kept in folders of
  their own (*removed cameras*: `list_cameras(include_removed=true)`, `delete_camera`).
- Every camera is in a scene, by default its name, so existing snapshots and baselines stay in
  scenes named after their cameras. `camera_compare` now compares only within the camera's
  current scene.
- `add_camera` and `remove_camera` take `name` instead of `id`.
- New tools: `delete_camera`, `set_scene`, `list_scenes`, `scene_snapshot`, `scene_compare`,
  `scene_timeline`. `camera_snapshot` accepts `from_path` without `camera`.

## Development

```
uv run pytest            # unit tests (no network; the preview test is skipped without ffmpeg)
uv run pytest -m live    # against the real camera from your config (IPCAM_MCP_CAMERA, default livingroom)
IPCAM_MCP_CAMERA=office uv run pytest -m live    # e.g. against another configured camera
uv run ruff check . && uv run ruff format .
```

> ⚠️ The live tests **physically move the camera** (a small pan left and back) and save
> snapshots and a 3 s clip to the media folder. They also create and delete a preset and a
> baseline, both named `cmcp_test`.

### Adding a camera type

1. Add an adapter in `src/ipcam_mcp/adapters/` taking `(DeviceConfig, media_dir)` and
   implementing the `Camera` protocol (and `PTZ` if it can pan/tilt) from
   `src/ipcam_mcp/capabilities.py`.
2. Map its config `type` to the adapter in `ADAPTERS` in `src/ipcam_mcp/registry.py`.

A new camera feature (e.g. privacy mode) gets a `Protocol` in `capabilities.py`, registered in
`CAPABILITIES`, plus its tools in `src/ipcam_mcp/server.py`.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

This project was built with [Claude Code](https://claude.com/claude-code).

## License

[MIT](LICENSE)
