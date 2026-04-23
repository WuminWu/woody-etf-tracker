import urllib.request, json
url = 'https://api.github.com/repos/WuminWu/woody-etf-tracker/actions/workflows'
try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        for wf in data.get('workflows', []):
            print(f"{wf['name']} - state: {wf['state']}")
except Exception as e:
    print(e)
