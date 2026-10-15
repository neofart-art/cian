"""Interactive local VNC password setup; never accepts Cian credentials."""
import getpass
import os
import subprocess
from pathlib import Path

if __name__ == '__main__':
    os.umask(0o077)
    first = getpass.getpass('Отдельный VNC-пароль (ровно 8 ASCII символов, не пароль ЦИАН): ')
    if len(first) != 8 or not first.isascii() or not first.isprintable():
        raise SystemExit('Нужно ровно 8 печатных ASCII символов.')
    if first != getpass.getpass('Повторите VNC-пароль: '):
        raise SystemExit('Пароли не совпали.')
    target = Path(os.getenv('BROWSER_PROFILE_DIR', '/profile')) / 'vnc.pass'
    subprocess.run(['x11vnc', '-storepasswd', first, str(target)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target.chmod(0o600)
    print('VNC-пароль сохранён в volume профиля.')
