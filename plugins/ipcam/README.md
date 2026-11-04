# ipcam plugin

Lets Claude see through and control the IP cameras on your local network (ONVIF + RTSP):
snapshots, video clips, pan/tilt, presets and change detection. Full documentation, setup
and camera notes: <https://github.com/aidev-iprokszin/ipcam-mcp>.

## Install

```
/plugin install ipcam --marketplace aidev-iprokszin/ipcam-mcp
```

Requires [uv](https://docs.astral.sh/uv/) and ffmpeg on your `PATH`, and Linux or WSL2 on the
same network as the cameras. Then ask Claude to add a camera, or see the setup guide linked
above.

## What it runs

The plugin starts one local MCP server over stdio: `uvx ipcam-mcp==<version>`, the
[ipcam-mcp](https://pypi.org/project/ipcam-mcp/) package from PyPI, pinned to the version in
this folder's `.mcp.json`. uv downloads the package and its dependencies from PyPI on first
use. The server runs `ffmpeg` locally to capture snapshots and clips.

## What it sends and stores

- **Network:** the server connects only to the cameras you configure, on your local network
  (ONVIF over HTTP and RTSP). It does not contact any other service and has no telemetry.
- **Stored on your machine:** camera settings in `~/.config/ipcam-mcp/cameras.toml`, camera
  usernames and passwords in local secrets files under `~/.config/ipcam-mcp/secrets/`
  (the server creates them readable only by you and warns about others), and snapshots, recordings and baselines in
  `~/.local/share/ipcam-mcp/media`. Nothing is deleted automatically; `delete_camera` removes
  a camera's files after you approve it in a form.
- **Returned to Claude:** tool results, including snapshot images, go to Claude as part of
  your conversation, like any other tool output. Recordings stay on disk; only their path is
  returned.

## Tools

Camera: `list_cameras`, `camera_snapshot`, `camera_save_baseline`, `camera_compare`,
`camera_record`. Pan/tilt: `ptz_move`, `ptz_list_presets`, `ptz_goto_preset`,
`ptz_save_preset`, `ptz_remove_preset`. Scenes: `list_scenes`, `scene_snapshot`,
`scene_compare`, `scene_timeline`. Management: `reload_cameras`, `add_camera`,
`rename_camera`, `set_scene`, `remove_camera`, `delete_camera`. Every tool declares MCP
annotations (read-only, destructive, idempotent).

## License

MIT, see <https://github.com/aidev-iprokszin/ipcam-mcp/blob/main/LICENSE>.
