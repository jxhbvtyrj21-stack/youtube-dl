"""A stand-in for ffmpeg used to test supervision. Behaviour by argv[1]:

  normal <frames>      emit -progress blocks for N frames, write output, exit 0
  hang                 emit a few blocks then sleep (0 % CPU)
  livelock             emit a few blocks then spin at 100 % CPU, no output
  crash                emit a few blocks then exit 1
  child                spawn a sleeping grandchild, print its pid to stderr, then hang
  stubborn             ignore 'q' and SIGTERM, hang
  flood                write 50 MB to stderr, then normal 30
  slowgrow             no progress blocks, but output file keeps growing for 3 s, exit 0

argv[2] (optional) = output path.
"""

import os
import signal
import subprocess
import sys
import threading
import time

mode = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].isdigit() else None
if mode == "normal" and len(sys.argv) > 2 and sys.argv[2].isdigit():
    frames_total = int(sys.argv[2])
    out = sys.argv[3] if len(sys.argv) > 3 else None
else:
    frames_total = 30


def block(frame, end=False):
    sys.stdout.write(f"frame={frame}\nfps=30.0\nout_time_us={frame * 33333}\nspeed=1.0x\n"
                     f"total_size={frame * 1000}\nprogress={'end' if end else 'continue'}\n")
    sys.stdout.flush()


def write_out(n=1000):
    if out:
        with open(out, "ab") as fh:
            fh.write(b"\x00" * n)


def stdin_q_listener():
    """Like ffmpeg: 'q' on stdin makes it finish and exit."""
    for line in sys.stdin:
        if line.strip() == "q":
            os._exit(255)


if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # ignores both 'q' and SIGTERM
else:
    threading.Thread(target=stdin_q_listener, daemon=True).start()

if mode == "flood":
    for _ in range(420000):
        sys.stderr.write("x" * 120 + "\n")
    mode = "normal"

if mode == "normal":
    for f in range(1, frames_total + 1):
        write_out()
        if f % 10 == 0 or f == frames_total:
            block(f, end=f == frames_total)
        time.sleep(0.005)
    sys.exit(0)

for f in range(1, 4):
    block(f * 10)
    write_out()
    time.sleep(0.05)

if mode == "hang" or mode == "stubborn":
    time.sleep(3600)
elif mode == "livelock":
    end = time.time() + 3600
    x = 0
    while time.time() < end:
        x += 1
elif mode == "crash":
    sys.stderr.write("Error while decoding stream #0:0\n")
    sys.exit(1)
elif mode == "child":
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    sys.stderr.write(f"CHILD={c.pid}\n")
    sys.stderr.flush()
    time.sleep(3600)
elif mode == "slowgrow":
    for _ in range(30):
        write_out()
        time.sleep(0.1)
    sys.exit(0)
