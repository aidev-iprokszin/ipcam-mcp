import asyncio
import dataclasses
from types import SimpleNamespace as NS

import pytest
import zeep.exceptions

from ipcam_mcp.adapters import onvif
from ipcam_mcp.adapters.onvif import OnvifCamera, OnvifPtzCamera, make_camera
from ipcam_mcp.capabilities import PTZ, Camera, Preset
from ipcam_mcp.config import DeviceConfig
from ipcam_mcp.errors import ConfigError, DeviceError

PASSWORD = "p@ss:/#"


def make_cfg(type_: str = "tapo_onvif", **settings) -> DeviceConfig:
    return DeviceConfig(
        id="livingroom",
        type=type_,
        host="10.0.0.5",
        user="u",
        password=PASSWORD,
        settings=settings,
    )


class FakePTZService:
    def __init__(self, relative_fails: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.relative_fails = relative_fails

    async def RelativeMove(self, req):
        self.calls.append(("RelativeMove", req))
        if self.relative_fails:
            raise zeep.exceptions.Fault("ter:ActionNotSupported")

    async def ContinuousMove(self, req):
        self.calls.append(("ContinuousMove", req))

    async def Stop(self, req):
        self.calls.append(("Stop", req))

    async def GetPresets(self, req):
        self.calls.append(("GetPresets", req))
        return [NS(Name="Door", token="1"), NS(Name="Window", token="2")]

    async def GotoPreset(self, req):
        self.calls.append(("GotoPreset", req))

    async def SetPreset(self, req):
        self.calls.append(("SetPreset", req))
        return "7"

    async def RemovePreset(self, req):
        self.calls.append(("RemovePreset", req))

    async def GetStatus(self, req):
        return NS(Position=NS(PanTilt=NS(x=0.25, y=-0.5)))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeMedia:
    def __init__(self, tokens=("main", "sub"), uri="rtsp://cam/x", fail=False):
        self.tokens = list(tokens)
        self.uri = uri
        self.fail = fail
        self.stream_requests: list[dict] = []

    async def GetProfiles(self):
        return [NS(token=t) for t in self.tokens]

    async def GetStreamUri(self, req):
        self.stream_requests.append(req)
        if self.fail:
            raise RuntimeError(f"denied rtsp://admin:{PASSWORD}@10.0.0.5/x")
        return NS(Uri=self.uri)


class FakeOnvifCamera:
    instances = 0

    def __init__(self, ptz: FakePTZService, media: FakeMedia | None = None):
        self.ptz = ptz
        self.media = media or FakeMedia()
        FakeOnvifCamera.instances += 1

    async def update_xaddrs(self):
        pass

    async def create_ptz_service(self):
        return self.ptz

    async def create_media_service(self):
        return self.media


@pytest.fixture(autouse=True)
def reset_instances():
    FakeOnvifCamera.instances = 0


@pytest.fixture
def ptz() -> FakePTZService:
    return FakePTZService()


@pytest.fixture
def factory_calls() -> list:
    return []


@pytest.fixture
def cam(tmp_path, ptz, factory_calls, monkeypatch) -> OnvifPtzCamera:
    monkeypatch.setattr(onvif, "CONTINUOUS_DURATIONS", {"small": 0, "medium": 0, "large": 0})
    monkeypatch.setattr(onvif, "SETTLE_POLL", 0)

    def factory(host, port, user, password):
        factory_calls.append((host, port, user))
        return FakeOnvifCamera(ptz)

    return make_camera(make_cfg(), tmp_path, onvif_factory=factory)


@pytest.fixture
def ffmpeg_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_run(args, out, timeout, context, secrets=()):
        calls.append(
            {"args": args, "out": out, "timeout": timeout, "context": context, "secrets": secrets}
        )
        return out

    monkeypatch.setattr(onvif, "run_ffmpeg", fake_run)
    return calls


def test_is_camera_and_ptz(cam):
    assert isinstance(cam, Camera)
    assert isinstance(cam, PTZ)


async def test_rtsp_url_encodes_special_chars(tmp_path):
    cam = make_camera(make_cfg(), tmp_path)
    assert await cam.rtsp_url("high") == "rtsp://u:p%40ss%3A%2F%23@10.0.0.5:554/stream1"
    assert (await cam.rtsp_url("low")).endswith("@10.0.0.5:554/stream2")
    assert (await make_camera(make_cfg(rtsp_port=8554), tmp_path).rtsp_url("high")).endswith(
        "@10.0.0.5:8554/stream1"
    )


async def test_snapshot_calls_ffmpeg(cam, ffmpeg_calls, tmp_path):
    path = await cam.snapshot("high")
    (call,) = ffmpeg_calls
    assert path.parent == tmp_path
    assert path.name.startswith("livingroom_") and path.suffix == ".jpg"
    assert call["out"] == path
    assert call["timeout"] == 20
    assert PASSWORD in call["secrets"]
    assert "p@ss" not in call["context"]
    assert await cam.rtsp_url("high") in call["args"]


async def test_snapshot_low_quality_uses_stream2(cam, ffmpeg_calls):
    await cam.snapshot("low")
    assert await cam.rtsp_url("low") in ffmpeg_calls[0]["args"]


async def test_record_validates_seconds(cam, ffmpeg_calls):
    for bad in (0, 61):
        with pytest.raises(ValueError, match="1.*60"):
            await cam.record(bad, "high")
    path = await cam.record(10, "high")
    assert path.suffix == ".mp4"
    assert ffmpeg_calls[0]["timeout"] == 25
    assert "10" in ffmpeg_calls[0]["args"]


async def test_move_relative(cam, ptz):
    pos = await cam.move("left", "small")
    assert ptz.names() == ["RelativeMove"]
    req = ptz.calls[0][1]
    assert req["ProfileToken"] == "main"
    assert req["Translation"]["PanTilt"] == {"x": 0.05, "y": 0.0}  # Tapo alias inverts pan
    assert pos == (0.25, -0.5)


async def test_move_directions_and_amounts(cam, ptz):
    await cam.move("up", "large")
    await cam.move("right", "medium")
    await cam.move("down", "small")
    pts = [c[1]["Translation"]["PanTilt"] for c in ptz.calls]
    assert pts == [{"x": 0.0, "y": 0.3}, {"x": -0.15, "y": 0.0}, {"x": 0.0, "y": -0.05}]


async def test_move_falls_back_to_continuous(cam, ptz):
    ptz.relative_fails = True
    await cam.move("left", "small")
    assert ptz.names() == ["RelativeMove", "ContinuousMove", "Stop"]
    assert ptz.calls[1][1]["Velocity"]["PanTilt"] == {"x": 0.5, "y": 0.0}
    await cam.move("right", "small")
    assert ptz.names()[3:] == ["ContinuousMove", "Stop"]


async def test_onvif_client_created_once(cam, factory_calls):
    await cam.move("left", "small")
    await cam.list_presets()
    assert factory_calls == [("10.0.0.5", 2020, "u")]


async def test_onvif_port_setting(tmp_path, ptz):
    seen = []
    cam = OnvifPtzCamera(
        make_cfg(onvif_port=8000),
        tmp_path,
        onvif_factory=lambda h, p, u, pw: seen.append(p) or FakeOnvifCamera(ptz),
    )
    await cam.list_presets()
    assert seen == [8000]


async def test_list_presets(cam):
    assert await cam.list_presets() == [Preset("Door", "1"), Preset("Window", "2")]


async def test_goto_preset_by_name_or_token(cam, ptz):
    await cam.goto_preset("Door")
    await cam.goto_preset("1")
    gotos = [c[1] for c in ptz.calls if c[0] == "GotoPreset"]
    assert [g["PresetToken"] for g in gotos] == ["1", "1"]
    assert all(g["ProfileToken"] == "main" for g in gotos)


async def test_goto_unknown_preset(cam):
    with pytest.raises(DeviceError) as e:
        await cam.goto_preset("Garage")
    assert str(e.value) == "preset 'Garage' not found; available: Door, Window"


async def test_save_new_preset(cam, ptz):
    assert await cam.save_preset("desk") == Preset("desk", "7")
    req = next(c[1] for c in ptz.calls if c[0] == "SetPreset")
    assert req["PresetName"] == "desk"
    assert "PresetToken" not in req


async def test_save_existing_refused(cam):
    with pytest.raises(DeviceError) as e:
        await cam.save_preset("Door")
    assert "Door" in str(e.value) and "overwrite" in str(e.value)


async def test_save_existing_overwrite(cam, ptz):
    await cam.save_preset("Door", overwrite=True)
    req = next(c[1] for c in ptz.calls if c[0] == "SetPreset")
    assert req["PresetToken"] == "1"


@pytest.mark.parametrize("name", ["", "x" * 33, "a\nb"])
async def test_save_invalid_names(cam, name):
    with pytest.raises(ValueError):
        await cam.save_preset(name)


async def test_remove_by_name_and_token(cam, ptz):
    assert await cam.remove_preset("Door") == Preset("Door", "1")
    assert await cam.remove_preset("2") == Preset("Window", "2")
    tokens = [c[1]["PresetToken"] for c in ptz.calls if c[0] == "RemovePreset"]
    assert tokens == ["1", "2"]


async def test_remove_unknown_lists_available(cam):
    with pytest.raises(DeviceError) as e:
        await cam.remove_preset("Garage")
    assert str(e.value) == "preset 'Garage' not found; available: Door, Window"


async def test_position(cam):
    assert await cam.position() == (0.25, -0.5)


async def test_onvif_error_is_scrubbed(tmp_path):
    def factory(*a):
        raise RuntimeError(f"auth failed for {PASSWORD}")

    cam = make_camera(make_cfg(), tmp_path, onvif_factory=factory)
    with pytest.raises(DeviceError) as e:
        await cam.list_presets()
    assert PASSWORD not in str(e.value)
    assert "livingroom" in str(e.value)


async def test_onvif_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(onvif, "ONVIF_TIMEOUT", 0.2)

    class Hanging(FakeOnvifCamera):
        async def update_xaddrs(self):
            await asyncio.sleep(5)

    cam = make_camera(make_cfg(), tmp_path, onvif_factory=lambda *a: Hanging(FakePTZService()))
    with pytest.raises(DeviceError, match="timed out"):
        await cam.list_presets()


async def test_failed_connect_is_retried_next_call(tmp_path, ptz):
    attempts = []

    def factory(*a):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("network down")
        return FakeOnvifCamera(ptz)

    cam = make_camera(make_cfg(), tmp_path, onvif_factory=factory)
    with pytest.raises(DeviceError):
        await cam.list_presets()
    assert await cam.list_presets() == [Preset("Door", "1"), Preset("Window", "2")]


async def test_move_waits_until_position_settles(cam, ptz, monkeypatch):
    # TC70 ignores new moves while still moving and reports MoveStatus UNKNOWN,
    # so move() must poll until the position stops changing.
    monkeypatch.setattr(onvif, "SETTLE_POLL", 0)
    positions = iter([(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (0.2, 0.0), (9.9, 9.9)])

    async def get_status(req):
        x, y = next(positions)
        return NS(Position=NS(PanTilt=NS(x=x, y=y)))

    ptz.GetStatus = get_status
    assert await cam.move("left", "medium") == (0.2, 0.0)


async def test_move_settle_gives_up_without_status(cam, ptz, monkeypatch):
    monkeypatch.setattr(onvif, "SETTLE_POLL", 0)

    async def get_status(req):
        raise RuntimeError("not supported")

    ptz.GetStatus = get_status
    assert await cam.move("left", "small") is None


async def test_slow_settle_is_not_a_move_timeout(cam, ptz, monkeypatch):
    """The move itself succeeded; slow position polling must not turn it into an
    'ONVIF timed out' error (the model might retry and move the camera twice)."""
    monkeypatch.setattr(onvif, "ONVIF_TIMEOUT", 0.2)
    monkeypatch.setattr(onvif, "SETTLE_TIMEOUT", 0.4)
    monkeypatch.setattr(onvif, "SETTLE_POLL", 0.05)
    counter = iter(range(10_000))

    async def get_status(req):  # never settles: the position changes on every read
        await asyncio.sleep(0.02)
        return NS(Position=NS(PanTilt=NS(x=next(counter) / 1000, y=0.0)))

    ptz.GetStatus = get_status
    assert await cam.move("left", "large") is None  # moved; position not confirmed
    assert ptz.names() == ["RelativeMove"]


async def test_move_transport_error_does_not_switch_to_continuous(cam, ptz):
    async def flaky(req):
        ptz.calls.append(("RelativeMove", req))
        raise ConnectionResetError("network blip")

    real_relative = ptz.RelativeMove
    ptz.RelativeMove = flaky
    with pytest.raises(DeviceError, match="ConnectionResetError"):
        await cam.move("left", "small")
    ptz.RelativeMove = real_relative
    await cam.move("left", "small")
    assert "ContinuousMove" not in ptz.names()


async def test_cancelled_continuous_move_still_stops(cam, ptz, monkeypatch):
    monkeypatch.setattr(onvif, "CONTINUOUS_DURATIONS", {"small": 5, "medium": 5, "large": 5})
    ptz.relative_fails = True
    task = asyncio.create_task(cam.move("left", "small"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "ContinuousMove" in ptz.names():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ptz.names()[-1] == "Stop"


# --- generic ONVIF ---------------------------------------------------------------


def stream_cam(tmp_path, media, type_="onvif", **settings):
    cam = make_camera(
        make_cfg(type_, **settings),
        tmp_path,
        onvif_factory=lambda *a: FakeOnvifCamera(FakePTZService(), media),
    )
    return cam


def test_tapo_defaults(tmp_path):
    seen = []
    cam = make_camera(
        make_cfg(),
        tmp_path,
        onvif_factory=lambda h, p, u, pw: seen.append(p) or FakeOnvifCamera(None),
    )
    assert type(cam) is OnvifPtzCamera
    assert seen == [] and cam._onvif_port == 2020


async def test_tapo_url_and_port(tmp_path):
    cam = make_camera(make_cfg(), tmp_path)
    assert (await cam.rtsp_url("high")).endswith("@10.0.0.5:554/stream1")


async def test_tapo_snapshot_does_not_contact_onvif(tmp_path, ffmpeg_calls):
    cam = make_camera(make_cfg(), tmp_path, onvif_factory=lambda *a: FakeOnvifCamera(None))
    await cam.snapshot("high")
    await cam.snapshot("low")
    await cam.record(5, "high")
    assert FakeOnvifCamera.instances == 0


def test_onvif_defaults(tmp_path):
    seen = []
    cam = make_camera(
        make_cfg("onvif"),
        tmp_path,
        onvif_factory=lambda h, p, u, pw: seen.append(p) or FakeOnvifCamera(FakePTZService()),
    )
    assert type(cam) is OnvifCamera
    assert not isinstance(cam, PTZ)
    assert isinstance(cam, Camera)
    assert cam._onvif_port == 80


def test_onvif_ptz_true(tmp_path):
    cam = make_camera(make_cfg("onvif", ptz=True), tmp_path)
    assert type(cam) is OnvifPtzCamera
    assert isinstance(cam, PTZ)


def test_tapo_ptz_false_override(tmp_path):
    assert not isinstance(make_camera(make_cfg(ptz=False), tmp_path), PTZ)


async def test_stream_uri_lookup(tmp_path, ffmpeg_calls):
    media = FakeMedia(uri="rtsp://admin:x@192.0.2.19:8554/live/ch0?a=1")
    cam = stream_cam(tmp_path, media)
    await cam.snapshot("high")
    assert await cam.rtsp_url("high") == "rtsp://u:p%40ss%3A%2F%23@10.0.0.5:8554/live/ch0?a=1"
    assert ffmpeg_calls[0]["args"].count("rtsp://u:p%40ss%3A%2F%23@10.0.0.5:8554/live/ch0?a=1") == 1
    (req,) = media.stream_requests
    assert req["ProfileToken"] == "main"
    assert req["StreamSetup"] == {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    await cam.rtsp_url("low")
    assert media.stream_requests[1]["ProfileToken"] == "sub"


async def test_stream_uri_without_port_uses_rtsp_port(tmp_path):
    media = FakeMedia(uri="rtsp://cam/x")
    assert (await stream_cam(tmp_path, media).rtsp_url("high")).endswith("@10.0.0.5:554/x")
    assert (await stream_cam(tmp_path, media, rtsp_port=10554).rtsp_url("high")).endswith(
        "@10.0.0.5:10554/x"
    )


async def test_single_profile_low_uses_profile_0(tmp_path):
    media = FakeMedia(tokens=["only"])
    await stream_cam(tmp_path, media).rtsp_url("low")
    assert media.stream_requests[0]["ProfileToken"] == "only"


async def test_stream_lookup_cached(tmp_path, ffmpeg_calls):
    media = FakeMedia()
    cam = stream_cam(tmp_path, media)
    await cam.snapshot("high")
    await cam.snapshot("high")
    await cam.snapshot("low")
    assert len(media.stream_requests) == 2


async def test_stream_lookup_failure_message(tmp_path, ffmpeg_calls):
    cam = stream_cam(tmp_path, FakeMedia(fail=True))
    with pytest.raises(DeviceError) as e:
        await cam.snapshot("high")
    msg = str(e.value)
    assert "stream_high" in msg
    assert PASSWORD not in msg and "p%40ss" not in msg and "admin:" not in msg


async def test_configured_stream_paths_skip_onvif(tmp_path, ffmpeg_calls):
    cam = make_camera(
        make_cfg("onvif", stream_high="/h264", stream_low="/sub"),
        tmp_path,
        onvif_factory=lambda *a: FakeOnvifCamera(None),
    )
    assert (await cam.rtsp_url("high")).endswith("@10.0.0.5:554/h264")
    await cam.snapshot("low")
    assert FakeOnvifCamera.instances == 0


async def test_invert_pan(tmp_path, ptz):
    def mk(**kw):
        return make_camera(
            make_cfg("onvif", ptz=True, **kw),
            tmp_path,
            onvif_factory=lambda *a: FakeOnvifCamera(ptz),
        )

    await mk().move("left", "small")
    await mk(invert_pan=True).move("left", "small")
    xs = [c[1]["Translation"]["PanTilt"]["x"] for c in ptz.calls if c[0] == "RelativeMove"]
    assert xs[0] < 0 < xs[1]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("onvif_port", "80"),
        ("onvif_port", 0),
        ("onvif_port", True),
        ("rtsp_port", 70000),
        ("invert_pan", "yes"),
        ("ptz", 1),
        ("stream_high", "h264"),
        ("stream_low", 5),
    ],
)
def test_bad_setting_types(tmp_path, key, value):
    with pytest.raises(ConfigError) as e:
        make_camera(make_cfg("onvif", **{key: value}), tmp_path)
    assert "livingroom" in str(e.value) and key in str(e.value)


# --- probe -----------------------------------------------------------------------


class FakeProbeCamera:
    def __init__(self, manufacturer="TP-Link", model="C210"):
        self._info = NS(Manufacturer=manufacturer, Model=model)

    async def update_xaddrs(self):
        pass

    async def create_devicemgmt_service(self):
        info = self._info

        class Mgmt:
            async def GetDeviceInformation(self):
                return info

        return Mgmt()


async def test_probe_returns_device_info():
    calls = []

    def factory(*a):
        calls.append(a)
        return FakeProbeCamera()

    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=factory)
    assert info == onvif.DeviceInfo("TP-Link", "C210", False, 80)
    assert calls == [("10.0.0.5", 80, "admin", "secret")]


async def test_probe_scrubs_credentials():
    class Failing(FakeProbeCamera):
        async def update_xaddrs(self):
            raise Exception("401 for admin:p@ss")  # noqa: TRY002

    with pytest.raises(DeviceError) as e:
        await onvif.probe("10.0.0.5", "admin", "p@ss", onvif_factory=lambda *a: Failing())
    assert "p@ss" not in str(e.value)
    assert "admin" not in str(e.value)
    assert "10.0.0.5" in str(e.value)


class ClosingProbeCamera(FakeProbeCamera):
    def __init__(self, fail_update=False, fail_close=False):
        super().__init__()
        self.closed = 0
        self.fail_update = fail_update
        self.fail_close = fail_close

    async def update_xaddrs(self):
        if self.fail_update:
            raise Exception("401 unauthorized")  # noqa: TRY002

    async def close(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("close failed")


async def test_probe_closes_camera_on_success():
    cam = ClosingProbeCamera()
    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=lambda *a: cam)
    assert info.model == "C210"
    assert cam.closed == 1


async def test_probe_closes_camera_on_failure():
    cam = ClosingProbeCamera(fail_update=True)
    with pytest.raises(DeviceError, match="login failed"):
        await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=lambda *a: cam)
    assert cam.closed == 1


async def test_probe_close_error_is_suppressed():
    cam = ClosingProbeCamera(fail_close=True)
    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=lambda *a: cam)
    assert info.model == "C210"
    assert cam.closed == 1


async def test_probe_timeout(monkeypatch):
    monkeypatch.setattr(onvif, "ONVIF_TIMEOUT", 0.01)

    class Hanging(FakeProbeCamera):
        async def update_xaddrs(self):
            await asyncio.sleep(5)

    with pytest.raises(DeviceError, match="timed out"):
        await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=lambda *a: Hanging())


def _by_port(cams: dict[int, object], seen: list[int]):
    def factory(host, port, user, password):
        seen.append(port)
        cam = cams[port]
        if isinstance(cam, Exception):
            raise cam
        return cam

    return factory


class UnreachableCamera(FakeProbeCamera):
    async def update_xaddrs(self):
        raise OSError("connection refused")


async def test_probe_tries_80_then_2020():
    seen: list[int] = []
    f = _by_port({80: UnreachableCamera(), 2020: FakeProbeCamera()}, seen)
    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=f)
    assert info.onvif_port == 2020 and seen == [80, 2020]


async def test_probe_explicit_port_only():
    seen: list[int] = []
    f = _by_port({8000: FakeProbeCamera()}, seen)
    info = await onvif.probe("10.0.0.5", "admin", "secret", port=8000, onvif_factory=f)
    assert info.onvif_port == 8000 and seen == [8000]


@pytest.mark.parametrize(
    "exc",
    [
        zeep.exceptions.XMLSyntaxError("root element found is html"),
        zeep.exceptions.XMLParseError("not xml"),
    ],
)
async def test_probe_non_onvif_reply_keeps_searching(exc):
    """A plain web server on port 80 (camera web UI) is not ONVIF: try the next port."""

    class WebPage(FakeProbeCamera):
        async def update_xaddrs(self):
            raise exc

    seen: list[int] = []
    f = _by_port({80: WebPage(), 2020: FakeProbeCamera()}, seen)
    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=f)
    assert info.onvif_port == 2020 and seen == [80, 2020]


async def test_probe_non_onvif_reply_everywhere_says_not_onvif():
    class WebPage(FakeProbeCamera):
        async def update_xaddrs(self):
            raise zeep.exceptions.XMLSyntaxError("root element found is html")

    f = _by_port({80: WebPage(), 2020: WebPage()}, [])
    with pytest.raises(DeviceError) as e:
        await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=f)
    assert "no ONVIF service found" in str(e.value) and "login failed" not in str(e.value)


async def test_probe_soap_fault_stops_search():
    class Unauthorized(FakeProbeCamera):
        async def update_xaddrs(self):
            raise zeep.exceptions.Fault("NotAuthorized")

    seen: list[int] = []
    f = _by_port({80: Unauthorized(), 2020: FakeProbeCamera()}, seen)
    with pytest.raises(DeviceError, match="NotAuthorized"):
        await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=f)
    assert seen == [80]


async def test_probe_both_fail_lists_ports():
    f = _by_port({80: UnreachableCamera(), 2020: UnreachableCamera()}, [])
    with pytest.raises(DeviceError) as e:
        await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=f)
    msg = str(e.value)
    assert "80" in msg and "2020" in msg
    assert "secret" not in msg and "admin" not in msg


class PtzProbeCamera(FakeProbeCamera):
    def __init__(self, caps):
        super().__init__()
        self._caps = caps

    async def create_devicemgmt_service(self):
        info, caps = self._info, self._caps

        class Mgmt:
            async def GetDeviceInformation(self):
                return info

            async def GetCapabilities(self, req):
                assert req == {"Category": "All"}
                if isinstance(caps, Exception):
                    raise caps
                return caps

        return Mgmt()


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (NS(PTZ=NS(XAddr="x")), True),
        (NS(PTZ=None), False),
        (RuntimeError("nope"), False),
    ],
)
async def test_probe_detects_ptz(caps, expected):
    cam = PtzProbeCamera(caps)
    info = await onvif.probe("10.0.0.5", "admin", "secret", onvif_factory=lambda *a: cam)
    assert info.ptz is expected


def test_known_settings():
    assert onvif.known_settings("TP-LINK", "C210") == {"invert_pan": True}
    assert onvif.known_settings("Hikvision", "X") == {}


async def test_stream_lookup_timeout_and_no_profiles_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(onvif, "ONVIF_TIMEOUT", 0.1)

    class HangingMedia(FakeMedia):
        async def GetStreamUri(self, req):
            await asyncio.sleep(5)

    with pytest.raises(DeviceError) as e:
        await stream_cam(tmp_path, HangingMedia()).rtsp_url("high")
    assert "timed out" in str(e.value) and "stream_high" in str(e.value)

    with pytest.raises(DeviceError) as e:
        await stream_cam(tmp_path, FakeMedia(tokens=[])).rtsp_url("low")
    assert "no media profiles" in str(e.value) and "stream_low" in str(e.value)


async def test_explicit_rtsp_port_beats_stream_uri_port(tmp_path):
    media = FakeMedia(uri="rtsp://cam:8554/x")
    cam = stream_cam(tmp_path, media, rtsp_port=10554)
    assert (await cam.rtsp_url("high")).endswith("@10.0.0.5:10554/x")


async def test_stream_uri_port_used_without_rtsp_port_setting(tmp_path):
    media = FakeMedia(uri="rtsp://cam:8554/x")
    cam = stream_cam(tmp_path, media)
    assert (await cam.rtsp_url("high")).endswith("@10.0.0.5:8554/x")


async def test_aclose_closes_onvif_client(cam, ptz):
    await cam.list_presets()  # connects
    client = cam._cam
    closed = []

    async def close():
        closed.append(1)

    client.close = close
    await cam.aclose()
    assert closed == [1] and cam._cam is None
    await cam.aclose()  # idempotent
    assert closed == [1]


async def test_rtsp_url_brackets_ipv6_host(tmp_path):
    cfg = dataclasses.replace(make_cfg(), host="fe80::1")
    cam = make_camera(cfg, tmp_path)
    assert (await cam.rtsp_url("high")).endswith("@[fe80::1]:554/stream1")


@pytest.mark.parametrize("kind", ["snapshot", "record"])
async def test_failed_url_lookup_leaves_no_empty_file(tmp_path, kind):
    """An offline camera fails before ffmpeg runs: the reserved file must not stay behind."""
    cam = make_camera(make_cfg(), tmp_path)

    async def broken(quality):
        raise DeviceError("livingroom: ONVIF timed out")

    cam.rtsp_url = broken
    with pytest.raises(DeviceError):
        await (cam.snapshot("high") if kind == "snapshot" else cam.record(3, "high"))
    assert list(tmp_path.iterdir()) == []
