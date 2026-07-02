import requests

url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-06-17"

headers_test1 = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.mlb.com",
    "Referer": "https://www.mlb.com/"
}

headers_test2 = {
    "User-Agent": "python-requests/2.31.0",
    "Accept": "*/*"
}

headers_test3 = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

for i, h in enumerate([headers_test1, headers_test2, headers_test3]):
    try:
        r = requests.get(url, headers=h)
        print(f"Test {i+1}: {r.status_code}")
    except Exception as e:
        print(f"Test {i+1} failed: {e}")
