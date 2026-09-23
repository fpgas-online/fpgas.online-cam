"""gst-libcam.sh picks its capture source from what the board really has.

Runs the real script against a fake sysfs / by-id tree, with GST_LAUNCH=echo so
the pipeline it would launch is printed instead of run:

    uv run --no-project --with pytest pytest tests/test_source_select.py
"""

import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "gst-libcam.sh"
NO_CAMERA = 78  # cam.service: RestartPreventExitStatus=78


@pytest.fixture
def board(tmp_path):
    sysfs, byid = tmp_path / "video4linux", tmp_path / "by-id"
    sysfs.mkdir()
    byid.mkdir()

    def node(name, label):
        (sysfs / name).mkdir()
        (sysfs / name / "name").write_text(label + "\n")

    def run(**env):
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "GST_LAUNCH": "echo", "RTMP_DEST": "rtmp://127.0.0.1/pib/test",
                 "V4L_SYSFS": str(sysfs), "V4L_BYID": str(byid), "CAM_WAIT_SECS": "0", **env})

    # Every Pi has the codec/ISP nodes whether or not a camera is attached.
    node("video10", "bcm2835-codec-decode")
    node("video12", "bcm2835-codec-isp")
    run.node, run.byid = node, byid
    return run


def test_csi_sensor_uses_libcamerasrc(board):
    board.node("v4l-subdev0", "ov5647 10-0036")  # Pi 4 (unicam) and Pi 5 (rp1-cfe) name it alike
    board.node("video0", "unicam-image")
    r = board()
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[0] == "libcamerasrc"


def test_usb_grabber_used_when_no_csi_sensor(board):
    board.node("video0", "UVC Camera (345f:2109): USB Vid")
    grabber = board.byid / "usb-MACROSILICON_2109-video-index0"
    grabber.touch()
    (board.byid / "usb-MACROSILICON_2109-video-index1").touch()  # metadata node, not a capture node
    r = board()
    assert r.returncode == 0, r.stderr
    words = r.stdout.split()
    assert words[:2] == ["v4l2src", f"device={grabber}"]
    # the grabber's raw YUY2, rate-limited and converted to what the encoder is fed
    assert "video/x-raw,format=YUY2,width=1280,height=720" in words
    assert words.index("videorate") < words.index("videoconvert") < words.index("clockoverlay")


def test_csi_sensor_wins_over_usb_grabber(board):
    board.node("v4l-subdev2", "imx708 10-001a")
    (board.byid / "usb-MACROSILICON_2109-video-index0").touch()
    r = board()
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[0] == "libcamerasrc"


def test_no_camera_gives_up_after_the_tries(board):
    board.node("v4l-subdev0", "csi2")  # Pi 5 receiver without a sensor is not a camera
    r = board(CAM_WAIT_TRIES="3")
    assert r.returncode == NO_CAMERA
    assert r.stdout == ""  # never launched a pipeline
    assert r.stderr.count("no camera found (try") == 3
    assert "giving up" in r.stderr


def test_cam_src_override_skips_detection(board):
    r = board(CAM_SRC="videotestsrc is-live=true")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[:2] == ["videotestsrc", "is-live=true"]
