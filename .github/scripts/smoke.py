# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Runs inside the candidate on GitHub only; tests both listeners and role separation."""
import json
import os
import time
import urllib.request
import urllib.error
import sys


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


web = os.environ.get("LITELLM_MCP_SWITCHER_WEBUI_ENABLED", "1") == "1"
mcp = os.environ.get("LITELLM_MCP_SWITCHER_MCP_ENABLED", "1") == "1"
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
for attempt in range(40):
    try:
        if request("http://127.0.0.1:4000/v1/mcp/server")[0] == 200:
            break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(1)
else:
    raise AssertionError("Runner fixture did not become ready")
if web:
    assert request("http://127.0.0.1:8080/healthz")[0] == 200
    assert request("http://127.0.0.1:8080/api/me")[0] == 401
    assert request("http://127.0.0.1:8080/api/me", os.environ["LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER"])[0] == 200
    key = os.environ["LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER"]
    status, content = request("http://127.0.0.1:8080/api/state", key)
    assert status == 200, content
    state = json.loads(content)
    if len(sys.argv) > 1 and sys.argv[1] == "restart":
        assert any(p["name"] == "CI smoke project" for p in state["projects"]), state
    else:
        status, content = request("http://127.0.0.1:8080/api/projects", key,
            {"name": "CI smoke project", "selection": {"servers": {"fixture": None}}})
        assert status == 201, content
    status, content = request("http://127.0.0.1:8080/api/apply", key,
        {"selection": {"servers": {}}, "revision": state["revision"]})
    assert status == 200 and json.loads(content)["active"]["servers"] == {}, content
if mcp:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert request("http://127.0.0.1:8000/mcp", "unknown", payload)[0] == 401
    status, content = request("http://127.0.0.1:8000/mcp", os.environ["LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER"], payload)
    assert status == 200 and 'current_state' in content and 'admin_set_access' not in content
    status, content = request("http://127.0.0.1:8000/mcp", os.environ["LITELLM_MCP_SWITCHER_ADMIN_VIRTUAL_KEY"], payload)
    assert status == 200 and 'admin_set_access' in content and 'apply_selection' not in content
    status, content = request("http://127.0.0.1:8000/mcp", os.environ["LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER"],
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "current_state", "arguments": {}}})
    assert status == 200 and 'ci-user' in content and '"isError":true' not in content.replace(' ', '')
    orchestrator = os.environ["LITELLM_MCP_SWITCHER_ORCHESTRATOR_BEARER"]
    status, content = request("http://127.0.0.1:8000/mcp", orchestrator, payload)
    assert status == 200 and 'choose_bearer' in content and 'admin_set_access' not in content
    status, content = request("http://127.0.0.1:8000/mcp", orchestrator,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "choose_bearer", "arguments": {}}})
    assert status == 200 and '"isError":true' not in content.replace(' ', ''), content
    if content.startswith('event:'):
        content = next(line[6:] for line in content.splitlines() if line.startswith('data: '))
    result = json.loads(content)['result']
    chosen = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert chosen['target_key'] == 'ci-user'
    status, content = request("http://127.0.0.1:8000/mcp", orchestrator,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "current_state", "arguments": {"session_id": chosen['session_id']}}})
    assert status == 200 and 'ci-user' in content and '"isError":true' not in content.replace(' ', ''), content
print("Candidate listeners and MCP role separation passed")
