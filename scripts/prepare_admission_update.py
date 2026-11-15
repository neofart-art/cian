"""Prepare/build images over ACTUAL OMV images; never stop/recreate/import.

Keep the rolled-back service.py and its one-page behavior. Private output includes
runtime inspect/environment and must not be posted publicly.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
APP_BASE_ADAPTER = 'bb732ebae97b32bb8196602391848d6db99459d8b67d9b1251bae632def9905b'
OLD_EXTRACT = 'dc8774430cf9ace6c8e9a8ecda8e7069ca816397bfcee937f4fcc2788fd437e9'


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def backport_scope(old, new):
    start, end = old.index('EXTRACT_CARD = '), old.index('# evaluate_all')
    if digest(old[start:end]) != OLD_EXTRACT:
        raise ValueError('Unexpected/local modified card extractor; no overwrite performed')
    if old.count('cards.push(extract(node));') != 1:
        raise ValueError('Unexpected card snapshot; no overwrite performed')
    a, b = new.index('EXTRACT_CARD = '), new.index('# evaluate_all')
    result = old[:start] + new[a:b] + old[end:]
    result = result.replace('cards.push(extract(node));', '''const card = extract(node);
      if (card.search_section === 'recommendation') {
        skipped += 1;
        continue;
      }
      cards.push(card);''')
    compile(result, 'collector/page.py', 'exec')
    return result


def run(*args):
    subprocess.run(args, check=True)


def read_json(*args):
    return json.loads(subprocess.check_output(args))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(mode=0o700)  # never overwrite a recovery directory
    doc = json.loads(args.compose.read_text())
    resolved = read_json('docker', 'compose', '-p', 'xata', '-f', str(args.compose), 'config', '--format', 'json')
    policy_raw = (ROOT / 'config/cian_district_policy_20261115.json').read_text()
    policy = json.loads(policy_raw)
    query = parse_qs(urlsplit(resolved['services']['app']['environment']['CIAN_SEARCH_URL']).query)
    ids = {v for k, values in query.items() if k.startswith('district[') for v in values}
    if ids != set(policy['ids']):
        raise ValueError('Search district IDs changed; verify the screenshot policy first')
    if len(policy['names']) != len(set(policy['names'])):
        raise ValueError('Duplicate policy names')
    tag = 'admission-' + os.urandom(6).hex()
    (output / 'previous-compose.json').write_text(args.compose.read_text())
    before = {}
    for name in ('app', 'browser'):
        info = read_json('docker', 'inspect', f'xata-{name}-1')[0]
        before[name] = info['Image']
        (output / f'{name}.inspect.private.json').write_text(json.dumps(info))
        if info['Config']['User'] != '10001:10001' or info['Config']['WorkingDir'] != '/app':
            raise ValueError('Unexpected user/workdir; inspect before preparing this update')
        env = dict(v.split('=', 1) for v in info['Config']['Env'])
        candidate_service = resolved['services'][name]
        if candidate_service['environment'] != env:
            raise ValueError(f'{name}: runtime environment differs from supplied Compose')
        if candidate_service.get('command') != info['Config']['Cmd']:
            raise ValueError(f'{name}: runtime command differs from supplied Compose')
        actual_mounts = {(m['Type'], m.get('Name'), m['Destination'], m['RW']) for m in info['Mounts']}
        config_mounts = set()
        for m in candidate_service.get('volumes', []):
            if m['type'] != 'volume':
                raise ValueError('Unexpected bind mount; inspect before preparing this update')
            volume = resolved['volumes'][m['source']]
            if not volume.get('external'):
                raise ValueError('Expected existing external volume')
            config_mounts.add(('volume', volume['name'], m['target'], not m.get('read_only', False)))
        if actual_mounts != config_mounts:
            raise ValueError(f'{name}: runtime volumes differ from supplied Compose')
        networks = {resolved['networks'][k]['name'] for k in candidate_service['networks']}
        if networks != set(info['NetworkSettings']['Networks']):
            raise ValueError(f'{name}: runtime networks differ from supplied Compose')
        ports = {(str(p['target']), p.get('protocol', 'tcp'), p.get('host_ip', '0.0.0.0'), str(p['published']))
                 for p in candidate_service.get('ports', [])}
        actual_ports = set()
        for target, bindings in (info['HostConfig']['PortBindings'] or {}).items():
            port, protocol = target.split('/')
            actual_ports.update((port, protocol, b['HostIp'] or '0.0.0.0', b['HostPort']) for b in bindings or [])
        if ports != actual_ports:
            raise ValueError(f'{name}: runtime ports differ from supplied Compose')
        for key, host_key in (('mem_limit', 'Memory'), ('memswap_limit', 'MemorySwap'),
                              ('pids_limit', 'PidsLimit'), ('shm_size', 'ShmSize')):
            if int(candidate_service.get(key, 0)) != info['HostConfig'][host_key]:
                raise ValueError(f'{name}: runtime {key} differs from supplied Compose')
        if float(candidate_service.get('cpus', 0)) * 1e9 != info['HostConfig']['NanoCpus']:
            raise ValueError(f'{name}: runtime CPU limit differs from supplied Compose')
        if not info['HostConfig']['ReadonlyRootfs'] or not candidate_service.get('read_only'):
            raise ValueError('Expected read-only runtime')
        changes = subprocess.check_output(['docker', 'diff', f'xata-{name}-1'], text=True)
        if any(line.split(' ', 1)[-1] == '/app' or line.split(' ', 1)[-1].startswith('/app/')
               for line in changes.splitlines()):
            raise ValueError(f'{name}: local runtime /app changes; preserve/merge before building')
        if name == 'browser' and info['HostConfig']['PortBindings']:
            raise ValueError('Manual browser access still active; reconcile Compose first')
        folder = output / name
        folder.mkdir()
        base_tag = f'xata-{name}:before-{tag}'
        run('docker', 'image', 'tag', info['Image'], base_tag)
        run('docker', 'image', 'save', '-o', str(output / f'{name}-image.tar'), base_tag)
        if name == 'app':
            old = folder / 'browser_source.before.py'
            run('docker', 'cp', f'xata-{name}-1:/app/app/browser_source.py', str(old))
            if digest(old.read_text()) != APP_BASE_ADAPTER:
                raise ValueError('Local app adapter modifications detected; merge rather than overwrite')
            for filename in ('browser_source.py', 'browser_admission.py'):
                (folder / filename).write_text((ROOT / 'app' / filename).read_text())
            copy = 'COPY --chown=10001:10001 browser_source.py browser_admission.py /app/app/\n'
        else:
            old = folder / 'page.before.py'
            run('docker', 'cp', f'xata-{name}-1:/app/collector/page.py', str(old))
            patched = backport_scope(old.read_text(), (ROOT / 'collector/page.py').read_text())
            (folder / 'page.py').write_text(patched)
            copy = 'COPY --chown=10001:10001 page.py /app/collector/page.py\n'
        (folder / 'Dockerfile').write_text(f'FROM {base_tag}\n' + copy)
        service = doc['services'][name]
        service['image'] = f'xata-{name}:{tag}'
        service['build'] = {'context': str(folder), 'dockerfile': 'Dockerfile'}
    doc['services']['app']['environment']['CIAN_DISTRICT_POLICY'] = json.dumps(policy, ensure_ascii=False)
    (output / 'image-ids.before.json').write_text(json.dumps(before))
    candidate = output / 'compose.json'
    candidate.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    run('docker', 'compose', '-p', 'xata', '-f', str(candidate), 'config', '--quiet')
    if args.build:
        run('docker', 'compose', '-p', 'xata', '-f', str(candidate), 'build', 'app', 'browser')
    print('Prepared:', candidate)
    print('Deployment performed: false; browser service.py unchanged; profile/data untouched')


if __name__ == '__main__':
    main()
