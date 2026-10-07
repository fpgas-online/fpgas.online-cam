#!/usr/bin/python3
"""Find out whether this board's CSI camera has a lens motor, and if it does, hand the lens to libcamera.

Run as root by fpgas-gst-libcam.sh before it starts the pipeline. Every Pi runs the same image and the same
config.txt, and a camera can be swapped between boots, so what the camera is has to be found out here, on the
Pi, in every boot (fpgas-online/fpgas.online-infra issue #177).

The firmware's camera auto-detect loads the plain sensor overlay. For the OV5647 that overlay already describes a
lens motor beside the sensor, as a *disabled* `ad5398@c` node: the overlay's `vcm` parameter does nothing but
set that node to "okay" and add `lens-focus = <&vcm>` to the sensor node. config.txt cannot ask for it, because
fixed-focus OV5647 modules are in the same fleet. So:

  1. Find the bound sensor and a disabled lens node next to it in the device tree. None: nothing to do.
  2. Ask the lens chip itself. It only answers while the camera is powered, so run a short capture and read
     its I2C address meanwhile. No answer from a camera that is streaming: a fixed-focus module, nothing to do.
     The answer holds for this boot (a CSI camera cannot be changed with the power on) and is remembered in
     /run, so later starts of the stream do not probe again.
  3. Unbind the camera receiver and the sensor, apply a runtime overlay that makes the two changes the `vcm`
     parameter would have made, and bind them again. The receiver has to be unbound too: re-probing the sensor
     under a live rp1-cfe makes it register its video devices twice, the kernel oopses, and the Pi then hangs
     in shutdown (measured on a Pi 5, 6.12.109+rpt-rpi-v8). Termination signals are held off for the length of
     this step, what was unbound is written down first, and a run that finds such a note left behind binds the
     two again before anything else. A killed run leaves no sensor for the caller to find, so the caller runs
     `fpgas-cam-lens --recover` (that step alone) before it looks for a camera.
  4. Write a libcamera tuning file with an autofocus section (the stock ov5647 tuning has none) and print its
     path. The caller then runs libcamerasrc with that file and af-mode=continuous: libcamera scans for the
     sharpest lens position from the picture when the stream starts.

Output: the tuning file's path on stdout when the lens is ready for autofocus, nothing otherwise. Everything
else, the output of the tools it runs included, goes to stderr. Exit 0 for "ready" and for "fixed-focus"; exit 1
when the camera could not be asked, or a lens chip answered and could not be set up: a camera that should
focus and cannot is a fault, not a fixed-focus camera. A failed set-up is remembered for the boot too, so a
restarting stream does not unbind and bind the camera drivers over and over.

All of this is volatile: nothing is written outside /run, and a reboot starts from the firmware's device tree.
"""

import ctypes
import errno
import fcntl
import glob
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

SYSFS = pathlib.Path(os.environ.get("FPGAS_CAM_SYSFS", "/sys"))
DT_BASE = SYSFS / "firmware/devicetree/base"
AF_DIR = pathlib.Path(os.environ.get("FPGAS_CAM_AF_DIR", "/usr/share/fpgas-online/cam/af"))
TUNING_DIR = pathlib.Path(os.environ.get("FPGAS_CAM_TUNING_DIR", "/usr/share/libcamera/ipa/rpi"))
RUN_DIR = pathlib.Path(os.environ.get("FPGAS_CAM_RUN_DIR", "/run/fpgas-cam"))
VERDICT = RUN_DIR / "lens-verdict"  # "fixed-focus" or "failed: ...", for this boot
UNBOUND = RUN_DIR / "lens-unbound.json"  # what bind_lens has unbound and not yet bound again

# Camera receiver drivers the unbind and bind below have been run on (Pi 5, 5 Oct 2026). Unbinding any other
# (unicam on a Pi 3 or 4) has never been tried, and a kernel fault there would cost the board its camera: a
# lens behind another receiver is an error, the stream starts unfocused, and nothing is unbound.
TESTED_RECEIVERS = ("rp1-cfe",)

# Lens motor drivers a sensor overlay may describe, by device-tree compatible.
LENS_COMPATIBLES = ("adi,ad5398",)
I2C_RDWR, I2C_M_RD = 0x0707, 0x0001
PROBE_SECONDS = 12  # how long to wait for the probe capture to deliver frames
FRAMES_BEFORE_VERDICT = 5  # frames the capture must have delivered before silence means "no lens chip"
SILENCES_BEFORE_VERDICT = 4  # unanswered reads, 0.25 s apart, a streaming camera gets before that verdict
# What an I2C read of an address nobody acknowledges fails with. Any other error says nothing about a chip.
NOBODY_THERE = (errno.ENXIO, errno.EREMOTEIO)
TOOL_SECONDS = 30  # no tool run here takes longer; one that does is hung

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


class ProbeError(Exception):
    """The camera could not be powered and asked, so nothing is known about a lens."""


def log(*args):
    print("fpgas-cam-lens:", *args, file=sys.stderr, flush=True)


def run(cmd):
    """Run a tool with its stdout on our stderr: our stdout carries the tuning file's path and nothing else.

    A tool that hangs is killed (SIGKILL, so it works even while bind_lens holds the termination signals off).
    """
    subprocess.run(cmd, check=True, stdout=sys.stderr, timeout=TOOL_SECONDS)


def write_atomic(path, text):
    """Never a half-written file where the next run, or libcamera, will read it."""
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    part.replace(path)


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
    """The first bound I2C device whose device-tree node has a lens motor node as a sibling, or None.

    libcamera's first camera is the one streamed (`cam -c1`, libcamerasrc); on a board with two CSI cameras
    that need not be this one. No fleet board has two.
    """
    for device in sorted((SYSFS / "bus/i2c/devices").glob("*-00*")):
        if not (device / "driver").exists() or not (device / "of_node").exists():
            continue
        sensor = (device / "of_node").resolve()
        if not (sensor / "port").is_dir():  # a camera sensor has a CSI-2 endpoint; the lens node has none
            continue
        for sibling in sorted(sensor.parent.iterdir()):
            compatible = dt_string(sibling, "compatible") if sibling.is_dir() else None
            if (compatible and any(c in compatible.split("\0") for c in LENS_COMPATIBLES)
                    and dt_u32(sibling, "reg") is not None):
                return Camera(device, sensor, sibling)
    return None


def find_receiver(camera):
    """The platform device that owns the far end of the sensor's CSI-2 link, e.g. .../devices/1f00110000.csi.

    Only the endpoint's own device: the node above its `port` (or `ports`). If that node has no platform device
    the answer is None, never a device further up the tree: on a Pi 5 the next one up is the PCIe root complex
    the Ethernet hangs off, and this is the device bind_lens unbinds.
    """
    remote = dt_u32(camera.sensor / "port" / "endpoint", "remote-endpoint")
    if remote is None:
        return None
    for phandle in DT_BASE.rglob("phandle"):
        if int.from_bytes(phandle.read_bytes()[:4], "big") != remote:
            continue
        owner = phandle.parent.parent  # endpoint -> port
        if not owner.name.startswith("port"):
            return None
        owner = owner.parent  # port -> device, or port -> ports
        if owner.name == "ports":
            owner = owner.parent
        for dev in (SYSFS / "bus/platform/devices").iterdir():
            if (dev / "of_node").exists() and (dev / "of_node").resolve() == owner:
                return dev
        return None
    return None


def lens_answers(camera):
    """Does a chip answer at the lens motor's I2C address while the camera is powered?

    True as soon as it answers. False only for a camera that is streaming and whose address stays unacknowledged
    over several reads. ProbeError for everything else (no frames, the bus could not be opened, reads failing
    some other way): then nothing is known about the lens, and "fixed-focus" must not be remembered.
    """
    loaded_here = not (SYSFS / "module/i2c_dev").is_dir()
    if loaded_here:
        run(["modprobe", "i2c-dev"])

    class Msg(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                    ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]

    class Rdwr(ctypes.Structure):
        _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]

    # The camera's supply is only on while it streams, and the chip is silent without it. `cam` prints one line
    # per frame; frames arriving is how we know the camera is powered.
    capture = dev = None
    answered, frames, silences, bus_error, tail = False, 0, 0, None, b""
    try:
        try:
            capture = subprocess.Popen(["cam", "-c1", f"--capture={PROBE_SECONDS * 30}"],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            raise ProbeError(f"cannot run the probe capture: {e}") from e
        os.set_blocking(capture.stdout.fileno(), False)
        deadline = time.monotonic() + PROBE_SECONDS
        while time.monotonic() < deadline and not answered:
            time.sleep(0.25)
            chunk = capture.stdout.read() or b""
            frames += chunk.count(b"seq:")
            tail = (tail + chunk)[-600:]
            try:
                # udev may not have made the node yet just after the modprobe: try again next time round
                dev = dev if dev is not None else os.open(f"/dev/i2c-{camera.bus}", os.O_RDWR)
                buf = (ctypes.c_uint8 * 2)()
                fcntl.ioctl(dev, I2C_RDWR, Rdwr((Msg * 1)(Msg(camera.lens_addr, I2C_M_RD, 2, buf)), 1))
                answered = True
            except OSError as e:
                if e.errno not in NOBODY_THERE:
                    bus_error, silences = e, 0  # the unanswered reads have to be in a row
                elif frames >= FRAMES_BEFORE_VERDICT:  # before that: not powered yet, or nothing there
                    silences += 1
            if silences >= SILENCES_BEFORE_VERDICT or capture.poll() is not None:
                break
        if answered:
            return True
        if silences >= SILENCES_BEFORE_VERDICT:
            return False
        last = tail.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ProbeError(f"the probe capture delivered {frames} frames (exit {capture.poll()}), {silences} "
                         f"unanswered reads, last I2C error {bus_error}: {last[0]}")
    finally:
        if dev is not None:
            os.close(dev)
        if capture is not None:
            capture.terminate()
            try:
                capture.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                capture.kill()  # it must not keep the camera: the stream is about to want it
                try:
                    capture.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    log("the probe capture did not die when killed")
        if loaded_here:
            subprocess.run(["modprobe", "-r", "i2c-dev"], check=False, stdout=sys.stderr)


def sysfs_write(path, text):
    log(f"echo {text} > {path}")
    pathlib.Path(path).write_text(text)


def overlay_text(camera):
    phandle = dt_u32(camera.lens, "phandle")
    if phandle is None:
        raise RuntimeError(f"{dt_path(camera.lens)} has no phandle in the live device tree: cannot link the sensor to it")
    return OVERLAY.format(lens=dt_path(camera.lens), sensor=dt_path(camera.sensor), phandle=phandle)


def rebind(note):
    """Bind the sensor, then the receiver, from a note of what was unbound. Each is tried whatever the other did.

    The receiver is bound even if the sensor would not bind: that is the order of an ordinary boot, and a board
    with its receiver bound and no sensor is no worse off than one with neither.
    """
    errors = []
    for driver, name in ((note["sensor_driver"], note["sensor"]), (note["receiver_driver"], note["receiver"])):
        if (pathlib.Path(driver) / name).exists():
            continue  # already bound
        try:
            sysfs_write(pathlib.Path(driver) / "bind", name)
        except OSError as e:
            errors.append(f"binding {name}: {e}")
    return errors


def recover():
    """Finish what an interrupted run left: bind the sensor and the receiver again."""
    if not UNBOUND.exists():
        return
    try:
        note = json.loads(UNBOUND.read_text())
        log(f"an earlier run was interrupted with {note['receiver']} and {note['sensor']} unbound: "
            "binding them again")
        errors = rebind(note)
    except (ValueError, KeyError, TypeError) as e:
        # The note is written whole or not at all, so this is not ours; it must not stop every later run.
        log(f"{UNBOUND} is not a note of what was unbound ({e!r}): removed, nothing bound")
        errors = []
    for e in errors:
        log(e)
    if errors:
        log(f"keeping {UNBOUND}: the next run tries again")
    else:
        UNBOUND.unlink(missing_ok=True)
    time.sleep(1)  # let the sensor register before find_camera looks for it


def bind_lens(camera):
    """Make the two device-tree changes of the overlay's `vcm` parameter and re-probe sensor and receiver."""
    receiver = find_receiver(camera)
    if receiver is None or not (receiver / "driver").exists():
        raise RuntimeError("cannot find the bound camera receiver at the far end of the sensor's CSI-2 link")
    text = overlay_text(camera)
    note = {"receiver": receiver.name, "receiver_driver": str((receiver / "driver").resolve()),
            "sensor": camera.device.name, "sensor_driver": str((camera.device / "driver").resolve())}
    receiver_driver = pathlib.Path(note["receiver_driver"]).name
    if receiver_driver not in TESTED_RECEIVERS:
        raise RuntimeError(f"the camera receiver {receiver.name} is driven by {receiver_driver}; unbinding it has "
                           f"not been tried (only {', '.join(TESTED_RECEIVERS)}), so the lens is left unbound")
    # From the first unbind to the last bind nothing may stop us: systemd's SIGTERM on a restart of the stream
    # service would otherwise leave the camera unbound for the rest of the boot.
    held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
    try:
        write_atomic(UNBOUND, json.dumps(note))  # failing here, nothing is unbound yet
        try:
            sysfs_write(pathlib.Path(note["receiver_driver"]) / "unbind", note["receiver"])
            sysfs_write(pathlib.Path(note["sensor_driver"]) / "unbind", note["sensor"])
            with tempfile.TemporaryDirectory() as tmp:
                dts, dtbo = f"{tmp}/fpgas-cam-lens.dts", f"{tmp}/fpgas-cam-lens.dtbo"
                pathlib.Path(dts).write_text(text)
                run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", dtbo, dts])
                run(["dtoverlay", dtbo])
        finally:
            # Bind again whatever happened: a board with the stock tree and a camera beats one with no camera.
            errors = rebind(note)
            if not errors:  # a bind that failed is tried again by the next run's recover()
                UNBOUND.unlink(missing_ok=True)
        if errors:
            raise RuntimeError("; ".join(errors))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
    for _ in range(20):
        if camera.lens_bound:
            return
        time.sleep(0.25)
    raise RuntimeError(f"no driver bound to the lens motor at {camera.lens_device.name} after the overlay")


def is_pisp():
    """Pi 5 family: the camera goes through the PiSP (rp1-cfe); older Pis use the VC4 ISP (unicam)."""
    return bool(glob.glob(str(SYSFS / "bus/platform/drivers/rp1-cfe/*.csi")))


def write_tuning(camera):
    """The stock tuning of this sensor for this Pi's ISP plus our autofocus section, in /run. Returns its path."""
    section = AF_DIR / f"{camera.sensor_name}.json"
    if not section.exists():
        raise RuntimeError(f"no autofocus section for sensor {camera.sensor_name} ({section})")
    stock = TUNING_DIR / ("pisp" if is_pisp() else "vc4") / f"{camera.sensor_name}.json"
    if not stock.exists():
        raise RuntimeError(f"no stock libcamera tuning file {stock}")
    tuning = json.loads(stock.read_text())
    tuning["algorithms"] = [a for a in tuning["algorithms"] if "rpi.af" not in a]
    tuning["algorithms"].append({"rpi.af": json.loads(section.read_text())["rpi.af"]})
    out = RUN_DIR / f"{camera.sensor_name}_af.json"
    write_atomic(out, json.dumps(tuning, indent=1))
    log(f"tuning: {stock} + {section} -> {out}")
    return out


def decide(camera):
    """0 with the lens ready, 0 with nothing to do, or 1; and the verdict to remember for this boot, if any."""
    name = f"{camera.sensor_name} at {camera.device.name}, lens node {dt_path(camera.lens)}"
    if camera.lens_enabled and camera.lens_linked and camera.lens_bound:
        log(f"{name}: lens driver already bound")
        return True, None
    if camera.lens_enabled or camera.lens_linked:
        log(f"{name}: lens node half set up (enabled={camera.lens_enabled} linked={camera.lens_linked} "
            f"bound={camera.lens_bound})")
        return False, None
    if VERDICT.exists():
        verdict = VERDICT.read_text().strip()
        log(f"{name}: decided earlier in this boot: {verdict}")
        return (None if verdict == "fixed-focus" else False), None
    try:
        if not lens_answers(camera):
            log(f"{name}: no chip answers at {camera.lens_addr:#04x} while the camera is streaming: fixed-focus camera")
            return None, "fixed-focus"
    except (ProbeError, OSError, subprocess.SubprocessError) as e:
        log(f"{name}: could not ask the camera about a lens: {e}")
        # Not remembered: the next start of the stream asks again (up to PROBE_SECONDS each time it fails).
        return False, None
    log(f"{name}: a lens chip answers at {camera.lens_addr:#04x}: binding its driver")
    try:
        bind_lens(camera)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log(f"FAILED to bind the lens driver: {e}")
        return False, f"failed: {e}"
    return True, None


def main(argv=()):
    if list(argv) not in ([], ["--recover"]):
        log("usage: fpgas-cam-lens [--recover]")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR / "lens-lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another fpgas-cam-lens is running")
            return 1
        recover()
        if argv:
            return 0
        camera = find_camera()
        if camera is None:
            log("no bound sensor with a lens motor node in the device tree: nothing to do")
            return 0
        ready, verdict = decide(camera)
        if verdict:
            write_atomic(VERDICT, verdict + "\n")
        if ready is None:
            return 0
        if not ready:
            return 1
        try:
            print(write_tuning(camera))
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            log(f"FAILED to write the autofocus tuning: {e}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
