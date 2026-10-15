"""Supervise display, optional local VNC, and API; forward shutdown cleanly."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

children = []
stopping = False


def stop(*_):
    global stopping
    stopping = True


def start(args):
    child = subprocess.Popen(args)
    children.append(child)
    return child


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    profile = Path(os.getenv('BROWSER_PROFILE_DIR', '/profile'))
    if os.getenv('BROWSER_MANUAL_ACCESS', '0') == '1' and not (profile / 'vnc.pass').is_file():
        sys.exit('Создайте VNC-пароль по инструкции README; доступ без пароля запрещён.')
    try:
        display = start(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'])
        for _ in range(50):
            if Path('/tmp/.X11-unix/X99').exists():
                break
            if display.poll() is not None:
                raise RuntimeError('Xvfb stopped')
            time.sleep(.1)
        else:
            raise RuntimeError('Xvfb not ready')
        if os.getenv('BROWSER_MANUAL_ACCESS', '0') == '1':
            start(['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900',
                   '-rfbauth', str(profile / 'vnc.pass'), '-forever', '-shared', '-quiet'])
            start(['websockify', '--web=/usr/share/novnc', '0.0.0.0:6080', '127.0.0.1:5900'])
        start([sys.executable, '-m', 'uvicorn', 'collector.service:app', '--host', '0.0.0.0',
               '--port', '8010', '--workers', '1', '--no-access-log', '--no-proxy-headers'])
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('Browser service child stopped')
            time.sleep(.5)
    finally:
        # Stop API first so Chromium flushes the persistent profile before Xvfb exits.
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    main()
