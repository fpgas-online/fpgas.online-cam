#!/usr/bin/python3
"""Find out whether this board's CSI camera has a lens motor, and if it does, hand the lens to libcamera.

Run as root by fpgas-gst-libcam.sh before it starts the pipeline. Every Pi runs the same image and the same
config.txt, and a camera can be swapped between boots, so what the camera is has to be found out here, on the
Pi, every time (fpgas-online/fpgas.online-infra issue #177).

The firmware's camera auto-detect loads the plain sensor overlay. For the OV5647 that overlay already describes a
lens motor beside the sensor, as a *disabled* `ad5398@c` node: the overlay's `vcm` parameter does nothing but
set that node to "okay" and add `lens-focus = <&vcm>` to the sensor node. config.txt cannot ask for it, because
fixed-focus OV5647 modules are in the same fleet. So:

  1. Find the bound sensor and a disabled lens node next to it in the device tree. None: nothing to do.
  2. Ask the lens chip itself. It only answers while the camera is powered, so run a short capture and read
     its I2C address meanwhile. No answer: a fixed-focus module, nothing to do.
  3. Unbind the camera receiver and the sensor, apply a runtime overlay that makes the two changes the `vcm`
     parameter would have made, and bind them again. The receiver has to be unbound too: re-probing the sensor
     under a live rp1-cfe makes it register its video devices twice and the kernel oopses (measured on a Pi 5,
     6.12.109+rpt-rpi-v8).
  4. Write a libcamera tuning file with an autofocus section (the stock ov5647 tuning has none) and print its
     path. The caller then runs libcamerasrc with that file and af-mode=continuous: libcamera scans for the
     sharpest lens position from the picture when the stream starts.

Output: the tuning file's path on stdout when the lens is ready for autofocus, nothing otherwise. Everything
else goes to stderr. Exit 0 unless a lens chip answered and could not be set up (exit 1): a camera that should
focus and cannot is a fault, not a fixed-focus camera.

All of this is volatile: nothing is written outside /run, and a reboot starts from the firmware's device tree.
"""

import ctypes
import fcntl
import glob
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

SYSFS = pathlib.Path(os.environ.get("FPGAS_CAM_SYSFS", "/sys"))
DT_BASE = SYSFS / "firmware/devicetree/base"
AF_DIR = pathlib.Path(os.environ.get("FPGAS_CAM_AF_DIR", "/usr/share/fpgas-online/cam/af"))
TUNING_DIRS = os.environ.get("FPGAS_CAM_TUNING_DIRS", "/usr/share/libcamera/ipa/rpi/pisp:/usr/share/libcamera/ipa/rpi/vc4")
RUN_DIR = pathlib.Path(os.environ.get("FPGAS_CAM_RUN_DIR", "/run/fpgas-cam"))

# Lens motor drivers a sensor overlay may describe, by device-tree compatible.
LENS_COMPATIBLES = ("adi,ad5398",)
I2C_RDWR, I2C_M_RD = 0x0707, 0x0001
PROBE_SECONDS = 10

OVERLAY = """/dts-v1/;
/plugin/;
/ {{
	fragment@0 {{
		target-path = "{lens}";
		__overlay__ {{
			status = "okay";
		}};
	}};
	fragment@1 {{
		target-path = "{sensor}";
		__overlay__ {{
			lens-focus = <{phandle:#x}>;
		}};
	}};
}};
"""


def log(*args):
    print("fpgas-cam-lens:", *args, file=sys.stderr, flush=True)


def dt_string(node, prop):
    try:
        return (node / prop).read_bytes().rstrip(b"\0").decode()
    except OSError:
        return None


def dt_u32(node, prop):
    try:
        data = (node / prop).read_bytes()
    except OSError:
        return None
    return int.from_bytes(data[:4], "big") if len(data) >= 4 else None


def dt_path(node):
    """A device-tree node's path as an overlay's target-path wants it."""
    return "/" + str(node.relative_to(DT_BASE))


class Camera:
    """A bound CSI sensor and the lens node its overlay put beside it."""

    def __init__(self, device, sensor, lens):
        self.device = device  # the sensor's I2C device, e.g. .../bus/i2c/devices/10-0036
        self.sensor = sensor  # its device-tree node
        self.lens = lens  # the lens motor's device-tree node
        self.bus = int(device.name.split("-")[0])
        self.lens_addr = dt_u32(lens, "reg")
        self.sensor_name = dt_string(sensor, "name") or sensor.name.split("@")[0]

    @property
    def lens_enabled(self):
        return dt_string(self.lens, "status") in (None, "okay", "ok")

    @property
    def lens_linked(self):
        return (self.sensor / "lens-focus").exists()

    @property
    def lens_device(self):
        return self.device.parent / f"{self.bus}-{self.lens_addr:04x}"

    @property
    def lens_bound(self):
        return (self.lens_device / "driver").exists()


def find_camera():
    """The first bound I2C device whose device-tree node has a lens motor node as a sibling, or None."""
    for device in sorted((SYSFS / "bus/i2c/devices").glob("*-00*")):
        if not (device / "driver").exists() or not (device / "of_node").exists():
            continue
        sensor = (device / "of_node").resolve()
        if not (sensor / "port").is_dir():  # a camera sensor has a CSI-2 endpoint; the lens node has none
            continue
        for sibling in sorted(sensor.parent.iterdir()):
            compatible = dt_string(sibling, "compatible") if sibling.is_dir() else None
            if compatible and any(c in compatible.split("\0") for c in LENS_COMPATIBLES):
                return Camera(device, sensor, sibling)
    return None


def find_receiver(camera):
    """The platform device at the other end of the sensor's CSI-2 link, e.g. .../devices/1f00110000.csi."""
    remote = dt_u32(camera.sensor / "port" / "endpoint", "remote-endpoint")
    if remote is None:
        return None
    by_node = {}
    for dev in (SYSFS / "bus/platform/devices").iterdir():
        if (dev / "of_node").exists():
            by_node[(dev / "of_node").resolve()] = dev
    for phandle in DT_BASE.rglob("phandle"):
        if int.from_bytes(phandle.read_bytes()[:4], "big") == remote:
            node = phandle.parent
            while node != DT_BASE:
                if node in by_node:
                    return by_node[node]
                node = node.parent
    return None


def lens_answers(camera):
    """Does a chip answer at the lens motor's I2C address while the camera is powered?"""
    loaded_here = not (SYSFS / "module/i2c_dev").is_dir()
    if loaded_here:
        subprocess.run(["modprobe", "i2c-dev"], check=True)

    class Msg(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                    ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]

    class Rdwr(ctypes.Structure):
        _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]

    # The camera's supply is only on while it streams, and the chip is silent without it.
    capture = subprocess.Popen(["cam", "-c1", f"--capture={PROBE_SECONDS * 30}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    answered = False
    try:
        deadline = time.monotonic() + PROBE_SECONDS
        dev = None
        while time.monotonic() < deadline and capture.poll() is None and not answered:
            time.sleep(0.5)
            try:
                dev = dev if dev is not None else os.open(f"/dev/i2c-{camera.bus}", os.O_RDWR)
                buf = (ctypes.c_uint8 * 2)()
                fcntl.ioctl(dev, I2C_RDWR, Rdwr((Msg * 1)(Msg(camera.lens_addr, I2C_M_RD, 2, buf)), 1))
                answered = True
            except OSError:
                pass  # not powered yet, or nothing there: keep asking until the deadline
        if dev is not None:
            os.close(dev)
    finally:
        capture.terminate()
        _, err = capture.communicate(timeout=15)
        if loaded_here:
            subprocess.run(["modprobe", "-r", "i2c-dev"], check=False)
    if capture.returncode not in (0, -15) and not answered:
        log("the probe capture failed:", err.decode(errors="replace").strip().splitlines()[-1:] or "no message")
    return answered


def sysfs_write(path, text):
    log(f"echo {text} > {path}")
    pathlib.Path(path).write_text(text)


def overlay_text(camera):
    phandle = dt_u32(camera.lens, "phandle")
    if phandle is None:
        raise RuntimeError(f"{dt_path(camera.lens)} has no phandle in the live device tree: cannot link the sensor to it")
    return OVERLAY.format(lens=dt_path(camera.lens), sensor=dt_path(camera.sensor), phandle=phandle)


def bind_lens(camera):
    """Make the two device-tree changes of the overlay's `vcm` parameter and re-probe sensor and receiver."""
    receiver = find_receiver(camera)
    if receiver is None or not (receiver / "driver").exists():
        raise RuntimeError("cannot find the bound camera receiver at the far end of the sensor's CSI-2 link")
    text = overlay_text(camera)
    receiver_driver = (receiver / "driver").resolve()
    sensor_driver = (camera.device / "driver").resolve()
    sysfs_write(receiver_driver / "unbind", receiver.name)
    sysfs_write(sensor_driver / "unbind", camera.device.name)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dts, dtbo = f"{tmp}/fpgas-cam-lens.dts", f"{tmp}/fpgas-cam-lens.dtbo"
            pathlib.Path(dts).write_text(text)
            subprocess.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", dtbo, dts], check=True)
            subprocess.run(["dtoverlay", dtbo], check=True)
    finally:
        # Bind again whatever happened: a board with the stock tree and a camera beats one with no camera.
        sysfs_write(sensor_driver / "bind", camera.device.name)
        sysfs_write(receiver_driver / "bind", receiver.name)
    for _ in range(20):
        if camera.lens_bound:
            return
        time.sleep(0.25)
    raise RuntimeError(f"no driver bound to the lens motor at {camera.lens_device.name} after the overlay")


def write_tuning(camera):
    """The stock tuning of this sensor plus our autofocus section, in /run. Returns its path."""
    section = AF_DIR / f"{camera.sensor_name}.json"
    if not section.exists():
        raise RuntimeError(f"no autofocus section for sensor {camera.sensor_name} ({section})")
    for directory in TUNING_DIRS.split(":"):
        stock = pathlib.Path(directory) / f"{camera.sensor_name}.json"
        if stock.exists() and (pathlib.Path(directory).name != "pisp" or is_pisp()):
            break
    else:
        raise RuntimeError(f"no stock libcamera tuning file for sensor {camera.sensor_name} in {TUNING_DIRS}")
    tuning = json.loads(stock.read_text())
    tuning["algorithms"] = [a for a in tuning["algorithms"] if "rpi.af" not in a]
    tuning["algorithms"].append({"rpi.af": json.loads(section.read_text())["rpi.af"]})
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    out = RUN_DIR / f"{camera.sensor_name}_af.json"
    out.write_text(json.dumps(tuning, indent=1))
    log(f"tuning: {stock} + {section} -> {out}")
    return out


def is_pisp():
    """Pi 5 family: the camera goes through the PiSP (rp1-cfe); older Pis use the VC4 ISP (unicam)."""
    return bool(glob.glob(str(SYSFS / "bus/platform/drivers/rp1-cfe/*.csi")))


def main():
    camera = find_camera()
    if camera is None:
        log("no bound sensor with a lens motor node in the device tree: nothing to do")
        return 0
    name = f"{camera.sensor_name} at {camera.device.name}, lens node {dt_path(camera.lens)}"
    if camera.lens_enabled and camera.lens_linked and camera.lens_bound:
        log(f"{name}: lens driver already bound")
    elif camera.lens_enabled or camera.lens_linked:
        log(f"{name}: lens node half set up (enabled={camera.lens_enabled} linked={camera.lens_linked} "
            f"bound={camera.lens_bound})")
        return 1
    elif not lens_answers(camera):
        log(f"{name}: no chip answers at {camera.lens_addr:#04x} while the camera is powered: fixed-focus camera")
        return 0
    else:
        log(f"{name}: a lens chip answers at {camera.lens_addr:#04x}: binding its driver")
        try:
            bind_lens(camera)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
            log(f"FAILED to bind the lens driver: {e}")
            return 1
    try:
        print(write_tuning(camera))
    except (OSError, RuntimeError, ValueError) as e:
        log(f"FAILED to write the autofocus tuning: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
