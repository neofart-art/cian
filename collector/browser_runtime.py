"""Explicit browser choice; profiles never cross engine boundaries."""
import os


def browser_engine():
    engine = os.getenv('BROWSER_ENGINE', 'chromium')
    if engine not in ('chromium', 'firefox'):
        raise ValueError('BROWSER_ENGINE must be chromium or firefox')
    return engine


async def launch_context(playwright, profile):
    engine = browser_engine()
    options = dict(headless=False, viewport={'width': 1280, 'height': 900})
    if engine == 'firefox':
        # Read/screenshot the actual window viewport seen over VNC, not a larger
        # emulated viewport extending past the Xvfb screen/browser toolbar.
        options.pop('viewport')
        options.update(no_viewport=True, locale='ru-RU', timezone_id='Europe/Moscow')
    return await getattr(playwright, engine).launch_persistent_context(str(profile / engine), **options)
