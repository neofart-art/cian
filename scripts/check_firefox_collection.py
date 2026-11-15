#!/usr/bin/env python3
"""Collect into memory, compare visible primary IDs, run admission without upsert."""
import argparse
import json
import subprocess


COLLECT = r'''
import json, os, urllib.request
headers = {"Authorization":"Bearer "+os.environ["BROWSER_API_TOKEN"],
           "Content-Type":"application/json"}
base = "http://127.0.0.1:8010"
request = urllib.request.Request(base+"/collect", method="POST", headers=headers,
    data=json.dumps({"url":os.environ["CIAN_SEARCH_URL"]}).encode())
report = json.load(urllib.request.urlopen(request, timeout=105))
request = urllib.request.Request(base+"/preview/results", headers=headers)
preview = json.load(urllib.request.urlopen(request, timeout=15))
print(json.dumps({"collection":report,"preview":preview}))
'''

ADMISSION = r'''
import json, os, sys
from app.browser_admission import Admission
from app.criteria import browser_criteria
items = json.load(sys.stdin)
policy = Admission(os.environ["CIAN_SEARCH_URL"], browser_criteria(),
                   os.getenv("CIAN_DISTRICT_POLICY", "{}"))
admitted, summary = policy.apply(items)
print("Проверка условий ХАТА:")
print(json.dumps(summary, ensure_ascii=False, indent=2))
for item in items:
    reasons = policy.reasons(item)
    print(item["id"], "OK" if not reasons else ", ".join(reasons),
          "|", item.get("metro"), "|", item.get("metro_time"))
print("Запись в базу не выполнялась.")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', default='xata-firefox-pilot')
    parser.add_argument('--app', default='xata-app-1')
    args = parser.parse_args()
    raw = subprocess.check_output(
        ['docker','exec',args.browser,'python','-c',COLLECT], timeout=130)
    payload = json.loads(raw)
    report, preview = payload['collection'], payload['preview']
    items = report.get('items', [])
    print('Статус сбора:', report.get('status'), flush=True)
    print('Получено:', len(items), flush=True)
    print('ID:', ', '.join(str(item['id']) for item in items), flush=True)
    if report.get('status') != 'authorized':
        print(report.get('error') or report.get('message'))
        raise SystemExit(1)
    selected = {str(item['id']) for item in preview.get('offers', [])
                if item.get('in_selected_container')}
    matches = {str(item['id']) for item in items} == selected
    print('URL совпадает:', preview.get('query_matches_configured_search'), flush=True)
    print('Основных контейнеров:', preview.get('containers'), flush=True)
    print('Собранные ID совпадают с основным блоком:', matches, flush=True)
    if (preview.get('status') != 'authorized' or preview.get('containers') != 1
            or preview.get('query_matches_configured_search') is not True or not matches):
        raise SystemExit('Не подтверждено совпадение с основной выдачей; импорт не выполнялся.')
    subprocess.run(['docker','exec','-i',args.app,'python','-c',ADMISSION],
                   input=json.dumps(items).encode(), check=True, timeout=30)


if __name__ == '__main__':
    main()
