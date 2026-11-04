"""Validate operator backup guards without touching Docker or a production volume."""
import copy
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('omv_snapshot',Path(__file__).parents[1]/'scripts/omv_snapshot.py')
snapshot=importlib.util.module_from_spec(spec);spec.loader.exec_module(snapshot)

def config():
    return {'HostConfig':{'PortBindings':{'8000/tcp':[{'HostIp':'172.31.1.1','HostPort':'3010'}]}},
            'Config':{'Env':['DATA_DIR=/data'],'WorkingDir':'/app'},
            'Mounts':[{'Type':'volume','Name':'xata_xata_data','Destination':'/data','RW':True}]}

def test_current_port_and_persistent_volume_required():
    assert snapshot.validate(config())=='/app'
    for mutate in [
        lambda x:x['HostConfig']['PortBindings']['8000/tcp'][0].update(HostIp='0.0.0.0'),
        lambda x:x['Mounts'][0].update(Name='fresh_empty_volume'),
        lambda x:x['Mounts'][0].update(RW=False),
        lambda x:x['Config'].update(Env=['DATA_DIR=/tmp']),
        lambda x:x['Config'].update(WorkingDir='/')]:
        info=config();mutate(info)
        with pytest.raises(ValueError):snapshot.validate(info)


def test_private_snapshot_does_not_deploy_or_print_secrets(tmp_path,monkeypatch,capsys):
    import json
    calls=[]
    info=config();info.update(Id='test-container-id',Name='/xata-app-1',Image='sha256:testimage')
    info['Config']['Env']+=['APP_PASSWORD=private-value']
    info['Config']['Labels']={'com.docker.compose.project':'xata','com.docker.compose.service':'app',
                              'com.docker.compose.project.config_files':'/missing/compose.yml'}
    info['NetworkSettings']={'Networks':{'xata_default':{}}}
    def command(*args):
        calls.append(args)
        if args[:2]==('docker','inspect'):return json.dumps([info]).encode()
        if args[:3]==('docker','image','inspect'):return b'[{}]'
        if args[:3]==('docker','volume','inspect'):return b'[{}]'
        if "print(int(Path('/data/images').exists()))" in args:return b'0'
        return b''
    monkeypatch.setattr(snapshot,'command',command)
    destination=tmp_path/'private'
    snapshot.snapshot('test-container-id',destination)
    assert 'private-value' not in capsys.readouterr().out
    assert 'private-value' in (destination/'container.inspect.json').read_text()
    assert (destination.stat().st_mode&0o777)==0o700
    assert (destination/'container.inspect.json').stat().st_mode&0o777==0o600
    assert not any(c[:2] in [('docker','stop'),('docker','rm'),('docker','compose')] for c in calls)
    assert any('backup(destination,pages=128,sleep=.05)' in value for c in calls for value in c)
