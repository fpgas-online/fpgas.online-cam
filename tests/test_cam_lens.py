"""cam-lens.py: what it reads from sysfs and the device tree, and what it writes.

Runs the real module against a fake /sys with the layout of a Pi 5 carrying an OV5647 (firmware-loaded `ov5647`
overlay: sensor at 10-0036, a disabled `ad5398@c` beside it, CSI-2 link to the rp1-cfe receiver):

    uv run --no-project --with pytest pytest tests/test_cam_lens.py

What this cannot check, and was checked on hardware instead (see README, "Lens motor"): that a lens chip
answers on I2C while the camera is powered, that the overlay binds the driver, and that libcamera focuses.
"""

import importlib.util
import json
import pathlib
import struct

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "cam-lens.py"
AF_DIR = SOURCE.parent / "af"
I2C = "axi/pcie@1000120000/rp1/i2c@88000"
CSI = "axi/pcie@1000120000/rp1/csi@110000"


def u32(value):
    return struct.pack(">I", value)


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A fake /sys; returns the loaded module. `pi.lens(status)` adds the lens node the overlay describes."""
    sys_root = tmp_path / "sys"
    base = sys_root / "firmware/devicetree/base"
    sensor = base / I2C / "ov5647@36"
    (sensor / "port/endpoint").mkdir(parents=True)
    (sensor / "name").write_bytes(b"ov5647\0")
    (sensor / "compatible").write_bytes(b"ovti,ov5647\0")
    (sensor / "port/endpoint/remote-endpoint").write_bytes(u32(0x52))
    receiver = base / CSI
    (receiver / "port/endpoint").mkdir(parents=True)
    (receiver / "port/endpoint/phandle").write_bytes(u32(0x52))

    devices = sys_root / "bus/i2c/devices"
    (devices / "10-0036").mkdir(parents=True)
    (devices / "10-0036/of_node").symlink_to(sensor)
    (devices / "10-0036/driver").mkdir()
    platform = sys_root / "bus/platform/devices"
    (platform / "1f00110000.csi").mkdir(parents=True)
    (platform / "1f00110000.csi/of_node").symlink_to(receiver)
    (platform / "1f00110000.csi/driver").mkdir()
    (platform / "1f00088000.i2c").mkdir()
    (platform / "1f00088000.i2c/of_node").symlink_to(base / I2C)
    (sys_root / "bus/platform/drivers/rp1-cfe/1f00110000.csi").mkdir(parents=True)

    stock = tmp_path / "ipa/pisp"
    stock.mkdir(parents=True)
    (stock / "ov5647.json").write_text(json.dumps(
        {"version": 2.0, "target": "pisp", "algorithms": [{"rpi.black_level": {"black_level": 1024}}, {"rpi.agc": {}}]}))

    monkeypatch.setenv("FPGAS_CAM_SYSFS", str(sys_root))
    monkeypatch.setenv("FPGAS_CAM_AF_DIR", str(AF_DIR))
    monkeypatch.setenv("FPGAS_CAM_TUNING_DIRS", f"{stock}:{tmp_path / 'ipa/vc4'}")
    monkeypatch.setenv("FPGAS_CAM_RUN_DIR", str(tmp_path / "run"))
    spec = importlib.util.spec_from_file_location("cam_lens", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def lens(status="disabled", phandle=0x10D):
        node = base / I2C / "ad5398@c"
        node.mkdir()
        (node / "compatible").write_bytes(b"adi,ad5398\0")
        (node / "reg").write_bytes(u32(0x0C))
        (node / "status").write_bytes(status.encode() + b"\0")
        if phandle is not None:
            (node / "phandle").write_bytes(u32(phandle))
        return node

    module.lens, module.sys_root, module.run_dir = lens, sys_root, tmp_path / "run"
    return module


def test_sensor_without_a_lens_node_is_left_alone(pi, capsys):
    # e.g. an imx219: its overlay describes no lens motor at all
    assert pi.find_camera() is None
    assert pi.main() == 0
    assert capsys.readouterr().out == ""


def test_finds_the_sensor_its_lens_node_and_the_receiver(pi):
    pi.lens()
    camera = pi.find_camera()
    assert camera.device.name == "10-0036"
    assert (camera.bus, camera.lens_addr, camera.sensor_name) == (10, 0x0C, "ov5647")
    assert not camera.lens_enabled and not camera.lens_linked and not camera.lens_bound
    assert camera.lens_device.name == "10-000c"
    assert pi.find_receiver(camera).name == "1f00110000.csi"


def test_overlay_makes_the_two_changes_of_the_vcm_parameter(pi):
    pi.lens(phandle=0x10D)
    text = pi.overlay_text(pi.find_camera())
    assert f'target-path = "/{I2C}/ad5398@c";' in text and 'status = "okay";' in text
    assert f'target-path = "/{I2C}/ov5647@36";' in text and "lens-focus = <0x10d>;" in text


def test_lens_node_without_a_phandle_cannot_be_linked(pi):
    pi.lens(phandle=None)
    with pytest.raises(RuntimeError, match="no phandle"):
        pi.overlay_text(pi.find_camera())


def test_fixed_focus_camera_prints_nothing(pi, monkeypatch, capsys):
    pi.lens()
    monkeypatch.setattr(pi, "lens_answers", lambda camera: False)
    monkeypatch.setattr(pi, "bind_lens", lambda camera: pytest.fail("must not touch the drivers"))
    assert pi.main() == 0
    out = capsys.readouterr()
    assert out.out == "" and "fixed-focus" in out.err


def test_answering_lens_is_bound_and_gets_an_autofocus_tuning(pi, monkeypatch, capsys):
    pi.lens()
    bound = []
    monkeypatch.setattr(pi, "lens_answers", lambda camera: True)
    monkeypatch.setattr(pi, "bind_lens", lambda camera: bound.append(camera.device.name))
    assert pi.main() == 0
    assert bound == ["10-0036"]
    path = pathlib.Path(capsys.readouterr().out.strip())
    assert path == pi.run_dir / "ov5647_af.json"
    tuning = json.loads(path.read_text())
    names = [next(iter(a)) for a in tuning["algorithms"]]
    assert names == ["rpi.black_level", "rpi.agc", "rpi.af"]  # the stock algorithms, untouched, plus ours
    assert tuning["algorithms"][-1]["rpi.af"]["map"] == [0.0, 150, 15.0, 500]


def test_lens_already_bound_only_writes_the_tuning(pi, monkeypatch, capsys):
    # a second start of the stream service in the same boot, or `dtoverlay=ov5647,vcm` in config.txt
    lens = pi.lens(status="okay")
    (lens.parent / "ov5647@36/lens-focus").write_bytes(u32(0x10D))
    (pi.sys_root / "bus/i2c/devices/10-000c/driver").mkdir(parents=True)
    monkeypatch.setattr(pi, "lens_answers", lambda camera: pytest.fail("must not probe"))
    monkeypatch.setattr(pi, "bind_lens", lambda camera: pytest.fail("must not rebind"))
    assert pi.main() == 0
    assert capsys.readouterr().out.strip().endswith("ov5647_af.json")


def test_failed_bind_is_an_error_not_a_fixed_focus_camera(pi, monkeypatch, capsys):
    pi.lens()
    monkeypatch.setattr(pi, "lens_answers", lambda camera: True)

    def fail(camera):
        raise RuntimeError("no driver bound to the lens motor at 10-000c after the overlay")

    monkeypatch.setattr(pi, "bind_lens", fail)
    assert pi.main() == 1
    out = capsys.readouterr()
    assert out.out == "" and "FAILED to bind" in out.err


def test_half_set_up_lens_is_an_error(pi, capsys):
    pi.lens(status="okay")  # enabled, but the sensor has no lens-focus link and no driver is bound
    assert pi.main() == 1
    assert "half set up" in capsys.readouterr().err


def test_shipped_autofocus_section_is_what_libcamera_reads():
    section = json.loads((AF_DIR / "ov5647.json").read_text())["rpi.af"]
    assert set(section["ranges"]) >= {"normal"} and set(section["speeds"]) >= {"normal"}
    low_dioptres, low_code, high_dioptres, high_code = section["map"]
    assert low_dioptres < high_dioptres and 0 <= low_code < high_code <= 1023
