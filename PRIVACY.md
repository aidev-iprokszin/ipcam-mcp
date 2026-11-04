# Privacy

ipcam-mcp (the PyPI package, the MCP server and the Claude Code plugin `ipcam`) is
open-source software that runs entirely on your own computer. The project and its
maintainer do not run any service and do not collect, receive or store any of your data.
There is no telemetry, analytics or crash reporting.

## What the software handles

The server works with personal data on your machine:

- **Camera images and video.** Snapshots, recordings and baselines can show people and
  private places.
- **Camera credentials.** The username and password of each camera's local account.
- **Camera settings.** Names, network addresses and scenes of your cameras.

## Where it is stored

Only on your computer, by default:

| What | Where |
|---|---|
| Camera settings | `~/.config/ipcam-mcp/cameras.toml` |
| Camera usernames and passwords | secrets files under `~/.config/ipcam-mcp/secrets/` (created readable only by you) |
| Snapshots, recordings, baselines | `~/.local/share/ipcam-mcp/media` |

The server never deletes snapshots or recordings on its own. `delete_camera` deletes a
camera's files only after you approve it in a form; you can also delete the files yourself
at any time.

## Where it goes

- **Your cameras.** The server connects only to the cameras you configure, on your local
  network (ONVIF and RTSP). It contacts no other server.
- **Your AI client.** Tool results, including snapshot images, are returned to the MCP client
  you use (for example Claude Code) as part of your conversation. From there, the client's
  provider handles them under its own privacy policy, for Claude the
  [Anthropic Privacy Policy](https://www.anthropic.com/legal/privacy). Recordings are not
  sent; only their file path is returned.
- **Installing.** Installing or updating downloads the package and its dependencies from
  PyPI (via uv), which sees a normal download request.

## People in view

Cameras can record people who did not agree to it. You are responsible for using cameras
lawfully where you live, for example informing people who may be recorded.

## Contact

Questions or concerns: open an issue at
<https://github.com/aidev-iprokszin/ipcam-mcp/issues>. Report security problems privately as
described in [SECURITY.md](SECURITY.md).
