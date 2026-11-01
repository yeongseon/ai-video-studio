"""Sparse samples and asynchronous cleanup interruption regressions."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from creator_service.media_integrity import MediaExpectation, MediaIntegrityError, validate_media
from creator_service.render_profile import RenderProfile


@pytest.mark.parametrize("segments", [1, 5, 30])
def test_sparse_frames_rejected_independent_of_segments(tmp_path: Path, segments: int) -> None:
    # Given: timestamps reach five seconds despite losing every fifth frame.
    path = tmp_path / "sparse.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=128x128:rate=30:duration=5", "-vf", "select=not(eq(mod(n\\,5)\\,0))",
        "-vsync", "0", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", str(path)],
        check=True, capture_output=True, timeout=30)
    # When / Then
    with pytest.raises(MediaIntegrityError):
        validate_media(path, MediaExpectation(RenderProfile(width=128, height=128), 5, False, segments))


@pytest.mark.parametrize("fps", [15, 24, 25, 30, 60])
def test_exact_decimal_ceiling_accepts(tmp_path: Path, fps: int) -> None:
    # Given / When / Then: decimal representation must not move the documented boundary.
    path = tmp_path / "good.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=size=128x128:rate={fps}:duration=5", "-c:v", "libx264", "-threads", "1",
        "-preset", "ultrafast", str(path)], check=True, capture_output=True, timeout=30)
    validate_media(path, MediaExpectation(RenderProfile(width=128, height=128, fps=fps),
        5 + max(0.1, 2 / fps) + 1e-6, False))


@pytest.mark.parametrize("mode", ["timeout", "soft", "keyboard", "exited"])
def test_resistant_group_and_grace_interruption(tmp_path: Path, mode: str) -> None:
    # Given: an isolated supervisor reaps orphans and externally bounds cleanup hangs.
    code = r'''
import ctypes, os, signal, subprocess, sys, time
from creator_service.media_validation_process import run_validation
from celery.exceptions import SoftTimeLimitExceeded
ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
mode, ready = sys.argv[1:]
child = """import os,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pid=os.fork()
if pid==0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    while True: time.sleep(1)
open(sys.argv[1],'w').write(str(os.getpid())+' '+str(pid))
print('ready',flush=True)
if sys.argv[2]=='exited': sys.exit(0)
while True: time.sleep(1)
"""
original_killpg = os.killpg
def killpg(pgid, sig):
    original_killpg(pgid, sig)
    if sig == signal.SIGTERM and mode in ('soft','keyboard'):
        signal.setitimer(signal.ITIMER_REAL, 0.03)
def alarm(sig, frame):
    signal.setitimer(signal.ITIMER_REAL, 0)
    if mode=='soft': raise SoftTimeLimitExceeded()
    raise KeyboardInterrupt()
os.killpg=killpg
signal.signal(signal.SIGALRM,alarm)
start=time.monotonic()
caught=None
try:
    run_validation([sys.executable,'-c',child,ready,mode],0.3)
except (RuntimeError,SoftTimeLimitExceeded,KeyboardInterrupt) as exc:
    caught=type(exc).__name__
finally:
    signal.setitimer(signal.ITIMER_REAL,0)
assert time.monotonic()-start < 1.5
assert caught == {'soft':'SoftTimeLimitExceeded','keyboard':'KeyboardInterrupt'}.get(mode,'ValidationProcessError'), caught
pids=list(map(int,open(ready).read().split()))
deadline=time.monotonic()+1
while time.monotonic()<deadline:
    try:
        pid,_=os.waitpid(-1,os.WNOHANG)
        if pid==0: time.sleep(0.005)
    except ChildProcessError: break
for pid in pids:
    assert not os.path.exists('/proc/'+str(pid)), ('survivor',pid)
'''
    # When / Then: external subprocess timeout turns a hang into a test failure.
    result = subprocess.run([sys.executable, "-c", code, mode, str(tmp_path / "pids")],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
