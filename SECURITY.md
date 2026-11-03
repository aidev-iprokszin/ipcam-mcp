# Security

## Reporting a vulnerability

Please report security problems **privately** through GitHub:
**Security → Report a vulnerability** on this repository. Do not open a public issue for
them. You will get an answer as soon as possible; this is a one-person project, so allow a few
days.

## What this server can do

ipcam-mcp gives an AI agent access to your cameras. Treat it like handing someone the camera
app:

- It can **see** (snapshots), **record** video, and **move** pan/tilt cameras.
- With `allow_management = true` (the default) it can **add, rename and remove** cameras in
  your config. Set `allow_management = false` in `cameras.toml` to turn those tools off.
- Snapshots, recordings and baselines are stored locally and never deleted by the server.

Your MCP client asks before each tool call unless you allowlist tools. If you allowlist,
consider keeping `camera_record`, `ptz_remove_preset` and `remove_camera` off the list.

## Design assumptions

- **Local network only.** The server talks to cameras on your LAN over ONVIF and RTSP. It is
  not meant to be exposed to the internet.
- **stdio by default.** The optional HTTP transport has **no authentication**. It binds to
  `127.0.0.1` by default, with DNS-rebinding protection (also for `localhost` and `::1`); any
  other `--host` is refused unless you pass `--allow-remote`, which exposes full camera control
  to the network.
- **RTSP is usually unencrypted**, and many cameras only support HTTP for ONVIF. Anyone on
  the same network segment may be able to see the stream or the camera login. Keep cameras
  on a trusted network or VLAN.

## Credentials

- Camera credentials live in plain-text env files outside the repository (default
  `~/.config/ipcam-mcp/`). Files written by `add_camera` are created with mode `0600` in a
  `0700` folder; the server warns at startup if a secrets file can be read by other users.
- Credentials never pass through the model: `add_camera` collects them with a client-side
  form or from a file you fill in yourself, passwords are scrubbed from every error and log
  message, and RTSP URLs are masked (`rtsp://***@host…`).
- Use a dedicated local camera account where your camera supports it, not your cloud login.

## Supported versions

Only the latest release on `main` receives fixes.
