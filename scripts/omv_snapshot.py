"""Operator-run discovery and consistent backups; never stops/recreates containers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def command(*args):
    return subprocess.run(args,check=True,capture_output=True).stdout


def validate(info):
    ports=info.get('HostConfig',{}).get('PortBindings',{}).get('8000/tcp',[])
    if ports!=[{'HostIp':'172.31.1.1','HostPort':'3010'}]:
        raise ValueError('Expected only 172.31.1.1:3010 -> 8000; inspect actual configuration first')
    mounts=info.get('Mounts',[])
    if not any(m.get('Type')=='volume' and m.get('Name')=='xata_xata_data' and m.get('Destination')=='/data' and m.get('RW') for m in mounts):
        raise ValueError('Expected existing writable named volume xata_xata_data at /data')
    env=dict(value.split('=',1) for value in info['Config'].get('Env',[]) if '=' in value)
    if env.get('DATA_DIR')!='/data':
        raise ValueError('Unexpected DATA_DIR; inspect before backup')
    work=info['Config'].get('WorkingDir','')
    if not work.startswith('/') or work=='/' or '..' in Path(work).parts:
        raise ValueError('Unexpected application work directory')
    return work


def private_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    path.chmod(0o600)


def snapshot(container,output):
    os.umask(0o077)
    info=json.loads(command('docker','inspect',container))[0]
    work=validate(info)
    output=Path(output).resolve()
    output.mkdir(parents=True,mode=0o700,exist_ok=False)
    private_json(output/'container.inspect.json',[info])
    image=json.loads(command('docker','image','inspect',info['Image']))[0]
    private_json(output/'image.inspect.json',[image])
    private_json(output/'volume.inspect.json',json.loads(command('docker','volume','inspect','xata_xata_data')))
    (output/'runtime').mkdir()
    command('docker','cp',info['Id']+':'+work+'/.',str(output/'runtime'))
    (output/'container.diff.txt').write_bytes(command('docker','diff',info['Id']))
    labels=info['Config'].get('Labels',{}) or {}
    original=[]
    for index,raw in enumerate(labels.get('com.docker.compose.project.config_files','').split(',')):
        if not raw:continue
        path=Path(raw)
        found=path.is_file()
        original.append({'path':str(path),'exists':found})
        if found:shutil.copy2(path,output/('original-compose-'+str(index)+path.suffix))
    workdir=Path(labels.get('com.docker.compose.project.working_dir','/nonexistent'))
    if (workdir/'.env').is_file():shutil.copy2(workdir/'.env',output/'original.env')
    # sqlite backup() reads a consistent snapshot even with a WAL or a live writer.
    # Importing app.main here would run migrations: intentionally use sqlite3 only.
    database_backup='/data/backups/pre-update-'+uuid.uuid4().hex+'.sqlite3'
    script="""import sqlite3,sys
from pathlib import Path
target=Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect('file:/data/xata.sqlite3?mode=ro',uri=True,timeout=30) as source,sqlite3.connect(target) as destination:
    source.backup(destination,pages=128,sleep=.05)
    assert destination.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert destination.execute('PRAGMA foreign_key_check').fetchone() is None
"""
    command('docker','exec',info['Id'],'python','-c',script,database_backup)
    command('docker','cp',info['Id']+':'+database_backup,str(output/'xata.sqlite3'))
    exists=command('docker','exec',info['Id'],'python','-c',"from pathlib import Path; print(int(Path('/data/images').exists()))").strip()==b'1'
    if exists:command('docker','cp',info['Id']+':/data/images',str(output/'images'))
    command('docker','image','save','--output',str(output/'app-image.tar'),info['Image'])
    report={'container':info['Name'].lstrip('/'),'container_id':info['Id'],'image_id':info['Image'],
            'compose_project':labels.get('com.docker.compose.project'),'compose_service':labels.get('com.docker.compose.service'),
            'source_labels_are_not_proof_of_existing_files':True,'original_compose':original,
            'runtime_saved':True,'database_backup_inside_volume':database_backup,
            'ports':info['HostConfig'].get('PortBindings'),'mounts':[{'type':m['Type'],'name':m.get('Name'),'destination':m['Destination']} for m in info['Mounts']],
            'networks':list(info.get('NetworkSettings',{}).get('Networks',{})),
            'deployment_performed':False}
    private_json(output/'report.json',report)
    # Only redacted structural report reaches stdout. Full inspect/env remains private.
    print(json.dumps(report,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container',required=True)
    parser.add_argument('--output',required=True,help='New private backup directory on a data disk')
    args=parser.parse_args()
    try:snapshot(args.container,args.output)
    except subprocess.CalledProcessError as error:
        raise SystemExit('Docker operation failed; partial private backup retained. Exit code '+str(error.returncode)) from None

if __name__=='__main__':main()
