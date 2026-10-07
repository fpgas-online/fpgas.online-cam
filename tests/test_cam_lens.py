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
    monkeypatch.setenv("FPGAS_CAM_TUNING_DIR", str(tmp_path / "ipa"))
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

    module.lens, module.sys_root, module.run_dir, module.base = lens, sys_root, tmp_path / "run", base
    (tmp_path / "run").mkdir()
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


# --- what the review asked for: the order of the unbind and bind steps, and what is left behind on failure ----

@pytest.fixture
def steps(pi, monkeypatch):
    """Record every sysfs write and tool run of bind_lens instead of doing it; returns the list."""

    class Steps(list):
        pass

    done = Steps()

    def write(path, text):
        path = pathlib.Path(path)
        done.append(f"{path.parent.name}/{path.name} {text}")
        fail = getattr(write, "fail", None)
        if fail and fail in done[-1]:
            raise OSError(5, "Input/output error")
        bound = pathlib.Path(path).parent / text  # mimic the driver core: a bound device appears under its driver
        if path.name == "bind":
            bound.mkdir()
        elif bound.exists():
            bound.rmdir()

    def run(cmd):
        done.append(cmd[0])
        if cmd[0] == getattr(run, "fail", None):
            raise pi.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(pi, "sysfs_write", write)
    monkeypatch.setattr(pi, "run", run)
    monkeypatch.setattr(pi.time, "sleep", lambda seconds: None)
    # real driver directories for the two devices, as sysfs has them
    for bus, driver, name in (("i2c", "ov5647", "10-0036"), ("platform", "rp1-cfe", "1f00110000.csi")):
        d = pi.sys_root / "bus" / bus / "drivers" / driver
        (d / name).mkdir(parents=True, exist_ok=True)
        link = pi.sys_root / "bus" / bus / "devices" / name / "driver"
        link.rmdir()
        link.symlink_to(d)
    done.write, done.run = write, run
    return done


RECEIVER_FIRST = ["rp1-cfe/unbind 1f00110000.csi", "ov5647/unbind 10-0036"]
SENSOR_THEN_RECEIVER = ["ov5647/bind 10-0036", "rp1-cfe/bind 1f00110000.csi"]


def test_bind_unbinds_receiver_first_and_binds_it_last(pi, steps):
    pi.lens()
    (pi.sys_root / "bus/i2c/devices/10-000c/driver").mkdir(parents=True)  # the lens driver binds after the overlay
    pi.bind_lens(pi.find_camera())
    assert steps == RECEIVER_FIRST + ["dtc", "dtoverlay"] + SENSOR_THEN_RECEIVER
    assert not pi.UNBOUND.exists()


def test_failed_overlay_still_binds_both_again(pi, steps):
    pi.lens()
    steps.run.fail = "dtc"
    with pytest.raises(pi.subprocess.CalledProcessError):
        pi.bind_lens(pi.find_camera())
    assert steps == RECEIVER_FIRST + ["dtc"] + SENSOR_THEN_RECEIVER
    assert not pi.UNBOUND.exists()


def test_sensor_that_will_not_bind_does_not_stop_the_receiver_bind(pi, steps):
    pi.lens()
    steps.write.fail = "ov5647/bind"
    with pytest.raises(RuntimeError, match="binding 10-0036"):
        pi.bind_lens(pi.find_camera())
    assert steps[-2:] == SENSOR_THEN_RECEIVER  # the receiver bind was still attempted
    assert pi.UNBOUND.exists()  # and the next run tries the sensor again


def test_failed_sensor_unbind_binds_the_receiver_again(pi, steps):
    pi.lens()
    steps.write.fail = "ov5647/unbind"
    with pytest.raises(OSError):
        pi.bind_lens(pi.find_camera())
    assert steps == RECEIVER_FIRST + ["rp1-cfe/bind 1f00110000.csi"]  # the sensor never left its driver


def test_nothing_is_unbound_behind_a_receiver_that_was_never_tried(pi, steps):
    pi.lens()
    unicam = pi.sys_root / "bus/platform/drivers/unicam"
    (unicam / "1f00110000.csi").mkdir(parents=True)
    link = pi.sys_root / "bus/platform/devices/1f00110000.csi/driver"
    link.unlink()
    link.symlink_to(unicam)
    with pytest.raises(RuntimeError, match="driven by unicam; unbinding it has not been tried"):
        pi.bind_lens(pi.find_camera())
    assert steps == [] and not pi.UNBOUND.exists()


def test_nothing_is_unbound_without_a_receiver_or_a_phandle(pi, steps):
    pi.lens(phandle=None)
    with pytest.raises(RuntimeError, match="no phandle"):
        pi.bind_lens(pi.find_camera())
    (pi.sys_root / "bus/platform/devices/1f00110000.csi/of_node").unlink()
    with pytest.raises(RuntimeError, match="cannot find the bound camera receiver"):
        pi.bind_lens(pi.find_camera())
    assert steps == []


def test_receiver_is_never_a_device_further_up_the_tree(pi):
    # the csi node has no platform device, but its ancestor (the PCIe root complex on a Pi 5) has one:
    # unbinding that would take the Ethernet, and a netbooted Pi's root, with it
    pi.lens()
    (pi.sys_root / "bus/platform/devices/1f00110000.csi/of_node").unlink()
    parent = pi.sys_root / "bus/platform/devices/1000120000.pcie"
    parent.mkdir()
    (parent / "of_node").symlink_to(pi.base / "axi/pcie@1000120000")
    (parent / "driver").mkdir()
    assert pi.find_receiver(pi.find_camera()) is None


def test_interrupted_run_is_finished_by_the_next_one(pi, steps):
    pi.lens()
    camera = pi.find_camera()
    note = {"receiver": "1f00110000.csi", "receiver_driver": str(pi.sys_root / "bus/platform/drivers/rp1-cfe"),
            "sensor": "10-0036", "sensor_driver": str(pi.sys_root / "bus/i2c/drivers/ov5647")}
    pi.UNBOUND.write_text(json.dumps(note))
    (pi.sys_root / "bus/platform/drivers/rp1-cfe/1f00110000.csi").rmdir()  # killed with both unbound
    (pi.sys_root / "bus/i2c/drivers/ov5647/10-0036").rmdir()
    pi.recover()
    assert steps == SENSOR_THEN_RECEIVER and not pi.UNBOUND.exists()
    assert camera.device.name == "10-0036"


def test_camera_that_could_not_be_asked_is_an_error_and_is_asked_again(pi, monkeypatch, capsys):
    pi.lens()

    def cannot(camera):
        raise pi.ProbeError("the probe capture delivered 0 frames (exit 1): camera busy")

    monkeypatch.setattr(pi, "lens_answers", cannot)
    assert pi.main() == 1
    assert "could not ask the camera" in capsys.readouterr().err
    assert not pi.VERDICT.exists()  # nothing is known, so nothing is remembered


def test_fixed_focus_verdict_is_remembered_for_the_boot(pi, monkeypatch, capsys):
    pi.lens()
    monkeypatch.setattr(pi, "lens_answers", lambda camera: False)
    assert pi.main() == 0
    monkeypatch.setattr(pi, "lens_answers", lambda camera: pytest.fail("must not probe a second time"))
    assert pi.main() == 0
    assert "decided earlier in this boot: fixed-focus" in capsys.readouterr().err


def test_failed_bind_is_not_retried_in_the_same_boot(pi, monkeypatch, capsys):
    pi.lens()
    monkeypatch.setattr(pi, "lens_answers", lambda camera: True)

    def fail(camera):
        raise RuntimeError("dtoverlay is not installed")

    monkeypatch.setattr(pi, "bind_lens", fail)
    assert pi.main() == 1
    monkeypatch.setattr(pi, "lens_answers", lambda camera: pytest.fail("must not probe again"))
    monkeypatch.setattr(pi, "bind_lens", lambda camera: pytest.fail("must not unbind the camera again"))
    assert pi.main() == 1
    assert "decided earlier in this boot: failed: dtoverlay is not installed" in capsys.readouterr().err


def test_vc4_tuning_is_not_used_on_a_pisp_board(pi, tmp_path):
    pi.lens()
    (tmp_path / "ipa/pisp/ov5647.json").rename(tmp_path / "ipa/ov5647.json.moved")
    (tmp_path / "ipa/vc4").mkdir()
    (tmp_path / "ipa/vc4/ov5647.json").write_text(json.dumps({"version": 2.0, "target": "bcm2835", "algorithms": []}))
    with pytest.raises(RuntimeError, match="no stock libcamera tuning file"):
        pi.write_tuning(pi.find_camera())


def test_lens_node_without_an_address_is_not_a_lens(pi):
    node = pi.lens()
    (node / "reg").unlink()
    assert pi.find_camera() is None


# --- second review: the probe itself, a note that is not a note, and recovery on its own -------------------------

@pytest.fixture
def probe(pi, monkeypatch):
    """lens_answers against a fake capture and a fake I2C bus; `probe(...)` returns its answer or raises.

    frames: "seq:" lines the capture prints per 0.25 s tick. reads: what each I2C read does, in order (the last
    repeats): True answers, an errno fails with it. opens: errnos the first opens of /dev/i2c-N fail with.
    """
    import errno
    import types

    (pi.sys_root / "module/i2c_dev").mkdir(parents=True)  # already loaded: no modprobe
    pi.lens()

    def ask(frames=2, reads=(errno.EREMOTEIO,), opens=(), exits=None):
        clock, reads_left, opens_left = [0.0], list(reads), list(opens)
        ask.read_count = 0

        class Out:
            def fileno(self):
                return 7

            def read(self):
                return b"seq: 000001 bytesused: 1\n" * frames

        class Capture:
            stdout = Out()

            def poll(self):
                return exits

            def terminate(self):
                ask.terminated = True

            def communicate(self, timeout=None):
                return b"", None

        def os_open(path, flags):
            assert path == "/dev/i2c-10"
            if opens_left:
                raise OSError(opens_left.pop(0), "open failed")
            return 99

        def ioctl(dev, request, arg):
            assert (dev, request) == (99, pi.I2C_RDWR)
            ask.read_count += 1
            result = reads_left.pop(0) if len(reads_left) > 1 else reads_left[0]
            if result is not True:
                raise OSError(result, "read failed")

        def sleep(seconds):
            clock[0] += seconds

        monkeypatch.setattr(pi.subprocess, "Popen", lambda *a, **k: Capture())
        monkeypatch.setattr(pi, "os", types.SimpleNamespace(
            open=os_open, close=lambda dev: None, set_blocking=lambda fd, on: None, O_RDWR=2, environ={}))
        monkeypatch.setattr(pi, "fcntl", types.SimpleNamespace(ioctl=ioctl))
        monkeypatch.setattr(pi, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0]))
        return pi.lens_answers(pi.find_camera())

    ask.errno = errno
    return ask


def test_probe_lens_chip_that_answers(probe):
    assert probe(reads=(probe.errno.EREMOTEIO, probe.errno.EREMOTEIO, True)) is True  # silent until powered
    assert probe.terminated


def test_probe_streaming_camera_that_stays_silent_is_fixed_focus(probe, pi):
    assert probe(frames=2, reads=(probe.errno.EREMOTEIO,)) is False
    # 5 frames arrive on the third tick; from then on four unanswered reads are needed, not one
    assert probe.read_count == 2 + pi.SILENCES_BEFORE_VERDICT
    assert probe(reads=(probe.errno.ENXIO,)) is False


def test_probe_without_frames_says_nothing_about_the_lens(probe, pi):
    with pytest.raises(pi.ProbeError, match="delivered 0 frames"):
        probe(frames=0)
    with pytest.raises(pi.ProbeError, match=r"delivered 0 frames \(exit 1\)"):
        probe(frames=0, exits=1)


@pytest.mark.parametrize("error", ["EIO", "ETIMEDOUT", "EAGAIN", "EBUSY"])
def test_probe_bus_errors_are_not_a_fixed_focus_camera(probe, pi, error):
    with pytest.raises(pi.ProbeError, match="last I2C error"):
        probe(frames=2, reads=(getattr(probe.errno, error),))


def test_probe_bus_that_cannot_be_opened_is_not_a_fixed_focus_camera(probe, pi):
    with pytest.raises(pi.ProbeError, match="open failed"):
        probe(opens=[probe.errno.EACCES] * 1000)


def test_probe_waits_for_the_bus_node_to_appear(probe):
    assert probe(opens=[probe.errno.ENOENT] * 3, reads=(True,)) is True
    assert probe(opens=[probe.errno.ENOENT] * 3, reads=(probe.errno.EREMOTEIO,)) is False


def test_run_kills_a_tool_that_hangs(pi, monkeypatch):
    monkeypatch.setattr(pi, "TOOL_SECONDS", 0.2)
    with pytest.raises(pi.subprocess.TimeoutExpired):
        pi.run(["sleep", "30"])


def test_hung_overlay_tool_still_binds_both_again_and_is_a_failed_bind(pi, steps, monkeypatch, capsys):
    pi.lens()

    def hung(cmd):
        steps.append(cmd[0])
        raise pi.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(pi, "run", hung)
    monkeypatch.setattr(pi, "lens_answers", lambda camera: True)
    assert pi.main() == 1
    assert steps == RECEIVER_FIRST + ["dtc"] + SENSOR_THEN_RECEIVER
    assert pi.VERDICT.read_text().startswith("failed:") and not pi.UNBOUND.exists()


@pytest.mark.parametrize("text", ['{"receiver": "1f0011', "{}", "[]", ""])
def test_note_that_is_not_a_note_is_removed_and_does_not_stop_the_run(pi, steps, capsys, text):
    pi.UNBOUND.write_text(text)
    assert pi.main() == 0  # no lens node on this board: nothing to do, as for any fixed-focus camera
    assert steps == [] and not pi.UNBOUND.exists()
    assert "is not a note of what was unbound" in capsys.readouterr().err


def test_note_and_verdict_are_written_whole(pi, steps, monkeypatch):
    pi.lens()
    seen = []
    real = pi.sysfs_write
    monkeypatch.setattr(pi, "sysfs_write", lambda path, text: (seen.append(json.loads(pi.UNBOUND.read_text())),
                                                               real(path, text)))
    (pi.sys_root / "bus/i2c/devices/10-000c/driver").mkdir(parents=True)
    pi.bind_lens(pi.find_camera())
    assert len(seen) == 4 and all(note["sensor"] == "10-0036" for note in seen)  # complete at every step
    assert list(pi.run_dir.glob("*.part")) == []


def test_recover_alone_binds_and_does_nothing_else(pi, steps, monkeypatch, capsys):
    pi.lens()
    note = {"receiver": "1f00110000.csi", "receiver_driver": str(pi.sys_root / "bus/platform/drivers/rp1-cfe"),
            "sensor": "10-0036", "sensor_driver": str(pi.sys_root / "bus/i2c/drivers/ov5647")}
    pi.UNBOUND.write_text(json.dumps(note))
    (pi.sys_root / "bus/platform/drivers/rp1-cfe/1f00110000.csi").rmdir()
    (pi.sys_root / "bus/i2c/drivers/ov5647/10-0036").rmdir()
    monkeypatch.setattr(pi, "lens_answers", lambda camera: pytest.fail("--recover must not probe"))
    assert pi.main(["--recover"]) == 0
    assert steps == SENSOR_THEN_RECEIVER and not pi.UNBOUND.exists()
    assert capsys.readouterr().out == ""
    assert pi.main(["--bogus"]) == 2


def test_bind_that_fails_in_recovery_keeps_the_note_for_the_next_run(pi, steps, capsys):
    pi.lens()
    note = {"receiver": "1f00110000.csi", "receiver_driver": str(pi.sys_root / "bus/platform/drivers/rp1-cfe"),
            "sensor": "10-0036", "sensor_driver": str(pi.sys_root / "bus/i2c/drivers/ov5647")}
    pi.UNBOUND.write_text(json.dumps(note))
    (pi.sys_root / "bus/platform/drivers/rp1-cfe/1f00110000.csi").rmdir()
    (pi.sys_root / "bus/i2c/drivers/ov5647/10-0036").rmdir()
    steps.write.fail = "ov5647/bind"
    assert pi.main(["--recover"]) == 0
    assert steps == SENSOR_THEN_RECEIVER and pi.UNBOUND.exists()  # receiver bound, sensor not: try again later
    steps.write.fail = None
    del steps[:]
    assert pi.main(["--recover"]) == 0
    assert steps == ["ov5647/bind 10-0036"] and not pi.UNBOUND.exists()


def test_probe_unanswered_reads_must_be_in_a_row(probe, pi):
    e = probe.errno
    with pytest.raises(pi.ProbeError):
        probe(frames=5, reads=(e.EREMOTEIO, e.EREMOTEIO, e.EREMOTEIO, e.EIO))
