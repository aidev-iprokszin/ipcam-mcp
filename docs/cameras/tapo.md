# TP-Link Tapo

Status: TC70 verified 2026-11-02.

## Camera account

In the Tapo app, open the camera, then Camera Settings, Advanced Settings, *Camera Account*,
and create a username and password. RTSP and ONVIF use this account, not your TP-Link cloud
login.

## IP address

In the Tapo app, open the camera, then Settings, Device Info.

## Ports and streams

- ONVIF: 2020
- RTSP: 554
- `/stream1`: main stream, at the resolution chosen in the Tapo app's video quality setting
  (a 2K model may still be set to 720p or 1080p)
- `/stream2`: sub stream

Snapshots, recordings and compares use whatever `/stream1` sends, so pick the resolution in
the app. Right after changing it, or while the app shows live view, the camera may refuse
`/stream1` for a while ("Operation not permitted"); `/stream2` keeps working.

## Night vision

The night vision mode in the Tapo app decides what snapshots, recordings and compares see in
the dark:

- **Infrared**: black and white. The IR lights brighten anything close to the camera (walls,
  roofs), and the sensor shows many fixed, star-like dots that are not in the scene.
- **Full colour**: uses the camera's white spotlight. To watch a sky or a distant scene,
  cover the spotlight (a C320WS has no setting to turn IR off). In a night-sky test this
  recorded about twice as many stars as infrared, and nearby surfaces stayed dark.

A light near the frame, such as a lit window, can show up as a soft bright patch in the sky
(a reflection in the lens or dome), and switching it on or off changes `camera_compare`
results. A baseline only fits the mode it was taken in: save one per mode (and one for
daytime), for example `night-colour` and `day`.

## Config

```toml
[cameras.livingroom]
type = "tapo_onvif"
secrets_file = "~/.config/ipcam-mcp/secrets.env"
host_var = "CAMERA_IP"
user_var = "CAMERA_USER"
password_var = "CAMERA_PASSWORD"
```

`tapo_onvif` is the same as `type = "onvif"` with these settings:

```toml
onvif_port = 2020
ptz = true
invert_pan = true
stream_high = "/stream1"
stream_low = "/stream2"
```

Defaults of `tapo_onvif` compared with `onvif`:

| Setting | `onvif` | `tapo_onvif` |
|---|---|---|
| `onvif_port` | 80 | 2020 |
| `rtsp_port` | from stream URI, else 554 | 554 |
| `invert_pan` | false | true |
| `ptz` | false | true |
| `stream_high` | from ONVIF | `/stream1` |
| `stream_low` | from ONVIF | `/stream2` |

With `tapo_onvif`, snapshots and recordings do not contact the camera over ONVIF.

## TC70 quirks

- The ONVIF pan axis is inverted (`+x` turns the view left), so `invert_pan = true`.
- `MoveStatus` is always `UNKNOWN`, and new moves are ignored while the camera is moving, so
  `ptz_move` polls the position until it settles before returning.
- `RelativeMove` works. `ContinuousMove` + `Stop` is a fallback, used only when a camera rejects
  `RelativeMove` with a SOAP fault.
- The audio is G.711, which mp4 can't hold, so recordings copy the video and transcode the
  audio to AAC.

## Troubleshooting

- **`401` or an ONVIF error on every call**: you used the wrong kind of account. Use the Camera
  Account from the app, not the TP-Link cloud login.
