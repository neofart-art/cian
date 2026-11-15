"""Create/build an isolated, no-import Firefox pilot. Never deploy production."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[1]
    info = json.loads(subprocess.check_output(['docker','inspect','xata-app-1']))[0]
    environment = dict(v.split('=',1) for v in info['Config']['Env'])
    search = environment.get('CIAN_SEARCH_URL','')
    parsed = urlsplit(search)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443)
        or not (parsed.hostname == 'cian.ru' or (parsed.hostname or '').endswith('.cian.ru'))
        or parsed.path.rstrip('/') != '/cat.php' or len(search)>4096):
        raise ValueError('Valid production Cian search URL required')
    # No app credentials, production network, data volume or browser profile copied.
    doc = {
      'services': {'browser': {
        'image':'xata-firefox-pilot:preview',
        'build':{'context':str(root),'dockerfile':'Dockerfile.firefox'},
        'container_name':'xata-firefox-pilot',
        'environment':{
          'BROWSER_ENGINE':'firefox','BROWSER_REVIEW_ONLY':'1','BROWSER_MANUAL_ACCESS':'1',
          'BROWSER_MAX_PAGES':'1',
          'BROWSER_REQUIRE_RESULT_SCOPE':'1','BROWSER_RESULTS_SELECTOR':'',
          'BROWSER_PROFILE_DIR':'/profile','HOME':'/home/browser',
          'BROWSER_API_TOKEN':secrets.token_hex(32),'CIAN_SEARCH_URL':search.replace('$','$$'),
        },
        'volumes':['firefox_profile:/profile'], 'ports':['127.0.0.1:16081:6080'],
        'init':True,'read_only':True,'restart':'unless-stopped',
        'cpus':3,'mem_limit':'4g','memswap_limit':'6g','shm_size':'512m','pids_limit':512,
        'tmpfs':['/tmp:size=256m,mode=1777','/home/browser:size=16m,uid=10001,gid=10001,mode=700'],
        'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
        'healthcheck':{
          'test':['CMD','python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz',timeout=3)"],
          'interval':'30s','timeout':'5s','start_period':'60s','retries':3},
        'logging':{'driver':'json-file','options':{'max-size':'5m','max-file':'3'}},
      }},
      'volumes':{'firefox_profile':{'name':'xata_firefox_pilot_profile'}},
    }
    path = args.output.resolve()/'compose.json'
    path.write_text(json.dumps(doc,ensure_ascii=False,indent=2))
    subprocess.run(['docker','compose','-p','xata-firefox-pilot','-f',str(path),'config','--quiet'],check=True)
    if args.build:
        subprocess.run(['docker','compose','-p','xata-firefox-pilot','-f',str(path),'build','browser'],check=True)
    print('Prepared:',path)
    print('Review-only Firefox; no production deployment or import performed')


if __name__ == '__main__':
    main()
