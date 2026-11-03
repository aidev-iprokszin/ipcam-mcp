# Known cameras

| Brand | Models | Config | Status |
|---|---|---|---|
| TP-Link Tapo | TC70 | `type = "tapo_onvif"` | verified 2026-11-02 ([page](tapo.md)) |

Other ONVIF cameras should work with `type = "onvif"`. Reports are welcome.

## Trying a new camera

1. Enable ONVIF in the camera's app or web page and create a local account. Cloud logins do
   not work.
2. Find the ONVIF port. It is usually 80, 2020 or 8000.
3. Ask Claude to `add_camera` with the camera's IP. It tries ports 80 and 2020. Give
   `onvif_port` for any other port.
4. If snapshots fail, set `stream_high` and `stream_low` in the camera's section of
   `cameras.toml` (the RTSP path of the main and sub stream, e.g. `/stream1`).
5. If the camera pans, run `ptz_move left`. If the view turns right, set `invert_pan = true`.
6. Report what you found by adding a page (below).

## Adding a page

Copy [tapo.md](tapo.md) to `<brand>.md`, replace the contents and keep the same headings:
account, IP, ports, streams, config, quirks, troubleshooting. Add a row to the table above.
Use `reported` as the status if you did not verify it with the live tests.
