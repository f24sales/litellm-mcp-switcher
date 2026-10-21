"""Runs inside the candidate on GitHub only; tests both listeners and role separation."""
import json
import os
import time
import urllib.request
import urllib.error


def request(url, bearer=None, payload=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


web = os.environ.get("WEBUI_ENABLED", "1") == "1"
mcp = os.environ.get("MCP_ENABLED", "1") == "1"
for port, enabled in [(8080, web), (8000, mcp)]:
    for attempt in range(40):
        try:
            status, _ = request(f"http://127.0.0.1:{port}/" if port == 8080 else f"http://127.0.0.1:{port}/mcp")
            if not enabled:
                raise AssertionError("Disabled listener is still exposed")
            break
        except (urllib.error.URLError, TimeoutError):
            if not enabled:
                break
            time.sleep(1)
    else:
        raise AssertionError("Enabled listener did not start")
if web:
    assert request("http://127.0.0.1:8080/healthz")[0] == 200
    assert request("http://127.0.0.1:8080/api/me")[0] == 401
    assert request("http://127.0.0.1:8080/api/me", os.environ["USER_SWITCHER_BEARER"])[0] == 200
if mcp:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert request("http://127.0.0.1:8000/mcp", "unknown", payload)[0] == 401
    status, content = request("http://127.0.0.1:8000/mcp", os.environ["USER_SWITCHER_BEARER"], payload)
    assert status == 200 and 'current_state' in content and 'admin_set_access' not in content
    status, content = request("http://127.0.0.1:8000/mcp", os.environ["LITELLM_ADMIN_VIRTUAL_KEY"], payload)
    assert status == 200 and 'admin_set_access' in content and 'apply_selection' not in content
print("Candidate listeners and MCP role separation passed")
