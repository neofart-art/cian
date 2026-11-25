#!/usr/bin/env python3
"""Prepare a narrow derivative of the running Chromium image; never deploy."""
import argparse
import ast
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True)


def patch_page(source):
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name == 'normalize_card']
    if len(nodes) != 1 or 'normalize_browser_card' in source:
        raise ValueError('Unexpected normalize_card; no changes made')
    node = nodes[0]
    old = ast.get_source_segment(source, node)
    if "parse_card_text(text)" not in old or "title or text" not in old:
        raise ValueError('Unexpected deployed normalizer; no changes made')
    replacement = '''def normalize_card(card):
    from collector.card_fields import normalize_browser_card
    return normalize_browser_card(card)'''
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [replacement + '\n']
    patched = ''.join(lines)
    # Existing Chromium service, browser launch, navigation and page scope stay intact.
    ast.parse(patched)
    return patched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    compose = args.compose.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit('Output directory already exists')
    app = json.loads(run('docker', 'inspect', 'xata-app-1'))[0]
    browser = json.loads(run('docker', 'inspect', 'xata-browser-1'))[0]
    active = app['Config']['Labels']['com.docker.compose.project.config_files']
    if ',' in active or Path(active).resolve() != compose:
        raise SystemExit('Provided compose is not the active single-file app configuration')
    doc = json.loads(run('docker', 'compose', '-p', 'xata', '-f', str(compose),
                         'config', '--format', 'json'))
    env = dict(e.split('=', 1) for e in app['Config']['Env'])
    browser_env = dict(e.split('=', 1) for e in browser['Config']['Env'])
    configured = doc['services']['app']['environment']
    for key in ('CIAN_SEARCH_URL', 'CIAN_DISTRICT_POLICY', 'BROWSER_SERVICE_URL',
                'BROWSER_SCHEDULE_ENABLED', 'BROWSER_SEARCH_INTERVAL_SECONDS'):
        if str(configured.get(key, '')) != env.get(key, ''):
            raise SystemExit('Runtime/compose mismatch: ' + key)
    if env.get('BROWSER_SERVICE_URL', '').rstrip('/') != 'http://browser:8010':
        raise SystemExit('App is not using the Chromium browser service')
    if browser_env.get('BROWSER_MANUAL_ACCESS', '0') != '0':
        raise SystemExit('Browser is in manual mode')
    if not browser['State']['Running']:
        raise SystemExit('Browser is not running')
    source_root = Path(__file__).resolve().parents[1]
    output.mkdir(mode=0o700)
    shutil.copy2(compose, output / 'compose-before.json')
    subprocess.run(['docker', 'cp', 'xata-browser-1:/app/collector/page.py',
                    str(output / 'page-before.py')], check=True)
    source = (output / 'page-before.py').read_text()
    patched = patch_page(source)
    (output / 'page.py').write_text(patched)
    shutil.copy2(source_root / 'collector/card_fields.py', output / 'card_fields.py')
    shutil.copy2(source_root / 'tests/test_visible_card_fields.py', output / 'test_fields.py')
    fingerprint = hashlib.sha256((browser['Image'] + patched).encode()).hexdigest()[:12]
    base_tag = 'xata-browser:before-fields-' + fingerprint
    image_tag = 'xata-browser:visible-fields-' + fingerprint
    subprocess.run(['docker', 'image', 'tag', browser['Image'], base_tag], check=True)
    (output / 'Dockerfile').write_text(
        'FROM ' + base_tag + '\n'
        'COPY --chown=10001:10001 page.py card_fields.py /app/collector/\n')
    subprocess.run(['docker', 'build', '-t', image_tag, str(output)], check=True)
    # No server profile, data volume, network or API token is passed to this test.
    subprocess.run([
        'docker', 'run', '--rm', '--network=none', '--read-only', '--user', '0',
        '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/app',
        '--mount', 'type=bind,src=' + str(output / 'test_fields.py') + ',dst=/tmp/test_fields.py,readonly',
        '--entrypoint', 'python', image_tag, '/tmp/test_fields.py',
    ], check=True)
    doc['services']['browser']['image'] = image_tag
    doc['services']['browser'].pop('build', None)
    # Use resolved configuration so relative host paths retain their actual meaning.
    target = output / 'compose.json'
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    target.chmod(0o600)
    subprocess.run(['docker', 'compose', '-p', 'xata', '-f', str(target),
                    'config', '--quiet'], check=True)
    manifest = dict(base_image=browser['Image'], image=image_tag,
                    original_page_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    patched_page_sha256=hashlib.sha256(patched.encode()).hexdigest(),
                    app_image=app['Image'], deployment_performed=False)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print('Prepared:', target)
    print('Tests passed; deployment performed: false; app, data and profile untouched')


if __name__ == '__main__':
    main()
