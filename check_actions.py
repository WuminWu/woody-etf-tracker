import urllib.request, json
url = 'https://api.github.com/repos/WuminWu/woody-etf-tracker/actions/runs'
try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        for run in data.get('workflow_runs', [])[:5]:
            print(f"{run['name']} - {run['status']} - {run['conclusion']} - {run['created_at']}")
except Exception as e:
    print(e)
