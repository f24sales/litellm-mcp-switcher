"""Orchestrator target selection remains separate across sessions and identities."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_switcher.config import Identity, authenticate, load_configuration
from mcp_switcher.db import Database
from mcp_switcher.mcp import create_mcp
from mcp_switcher.models import ProjectInput, Selection
from mcp_switcher.service import Service
from mcp_switcher.web import create_web
from test_configuration import configure
from test_web_mcp import rpc

P = "LITELLM_MCP_SWITCHER_"


def with_orchestrators(setup):
    original, fake, users = setup
    identities = users + [Identity("operator", "operator-" + "o" * 40, key_names=("alice", "bob")),
                           Identity("single", "single-" + "s" * 40, key_names=("alice",))]
    return Service(original.db, original.upstream, identities, original.settings), fake, identities


def call(client, bearer, name, **arguments):
    status, data = rpc(client, bearer, "tools/call", {"name": name, "arguments": arguments})
    assert status == 200, data
    result = data.get("result", {})
    assert not result.get("isError"), result
    assert "error" not in data, data
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_orchestrator_two_sessions_projects_and_key_scoped_writes(setup):
    service, fake, identities = with_orchestrators(setup)
    mcp = create_mcp(service, identities)
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True, json_response=True)) as client:
        bearer = identities[-2].bearer
        for who, count in [(identities[1], 6), (identities[-2], 7), (identities[-1], 7)]:
            _, data = rpc(client, who.bearer, "tools/list")
            names = {t['name'] for t in data['result']['tools']}
            assert len(names) == count
            assert ('choose_bearer' in names) == who.orchestrator
        first = call(client, bearer, 'choose_bearer')
        second = call(client, bearer, 'choose_bearer', key_name='bob')
        a, b = first['session_id'], second['session_id']
        assert a != b and first['target_key'] == 'alice' and second['target_key'] == 'bob'
        assert first['key_names'] == ['alice', 'bob']
        for ident in identities:
            assert ident.bearer not in json.dumps(first)
        alice = call(client, bearer, 'current_state', session_id=a)
        bob = call(client, bearer, 'current_state', session_id=b)
        assert alice['target_key'] == 'alice' and bob['target_key'] == 'bob'
        assert alice['revision'] != bob['revision']
        _, stale = rpc(client, bearer, 'tools/call', {'name': 'apply_selection', 'arguments': {
            'session_id': b, 'selection': {'servers': {}}, 'revision': alice['revision']}})
        assert stale['result']['isError']
        assert not fake.writes
        result = call(client, bearer, 'apply_selection', session_id=b,
                      selection={'servers': {'tasks': None}}, revision=bob['revision'])
        assert result['target_key'] == 'bob'
        assert set(fake.keys[identities[1].litellm_key]['object_permission']['mcp_servers']) == {'tasks', 'docs'}
        assert fake.keys[identities[2].litellm_key]['object_permission']['mcp_servers'] == ['tasks']
        project = call(client, bearer, 'save_project', session_id=b,
                       name='Bob project', selection={'servers': {'tasks': None}})
        assert project['target_key'] == 'bob'
        assert call(client, identities[2].bearer, 'current_state')['projects'][0]['id'] == project['id']
        assert call(client, bearer, 'current_state', session_id=a)['projects'] == []
        assert call(client, bearer, 'choose_bearer', session_id=a)['target_key'] == 'alice'
        call(client, bearer, 'choose_bearer', session_id=a, key_name='bob')
        assert call(client, bearer, 'current_state', session_id=a)['target_key'] == 'bob'
        for who, name, args in [
            (identities[1], 'choose_bearer', {}),
            (identities[-1], 'current_state', {'session_id': b}),
            (identities[-1], 'choose_bearer', {'key_name': 'bob'}),
            (identities[-2], 'current_state', {}),
            (identities[-2], 'choose_bearer', {'session_id': 'not-created'}),
            (identities[-2], 'admin_list_users', {}),
        ]:
            _, result = rpc(client, who.bearer, 'tools/call', {'name': name, 'arguments': args})
            assert 'error' in result or result.get('result', {}).get('isError'), result


async def test_web_target_is_per_request_and_never_global(setup):
    service, fake, identities = with_orchestrators(setup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_web(service, identities)),
                                 base_url='http://test') as client:
        headers = {'Authorization': 'Bearer ' + identities[-2].bearer}
        me = (await client.get('/api/me', headers=headers)).json()
        assert me['orchestrator'] and me['key_names'] == ['alice', 'bob']
        for name in ['alice', 'bob', 'alice']:
            response = await client.get('/api/state', headers=headers, params={'key_name': name})
            assert response.status_code == 200 and response.json()['target_key'] == name
        assert (await client.get('/api/state', headers=headers)).json()['target_key'] == 'alice'
        for token, name in [(identities[1].bearer, 'bob'), (identities[-1].bearer, 'bob')]:
            response = await client.get('/api/state', headers={'Authorization': 'Bearer ' + token},
                                        params={'key_name': name})
            assert response.status_code == 403


def test_configuration_allows_managed_key_without_personal_login(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.delenv(P + 'KEY_SWITCHER_BEARER')
    monkeypatch.setenv(P + 'ORCHESTRATOR_NAME', 'operator')
    monkeypatch.setenv(P + 'ORCHESTRATOR_BEARER', 'operator-' + 'o' * 40)
    monkeypatch.setenv(P + 'ORCHESTRATOR_KEY_NAMES', 'alice')
    settings, identities = load_configuration()
    assert identities[1].bearer == ''
    assert identities[2].orchestrator and identities[2].key_names == ('alice',)
    assert authenticate(identities[1].litellm_key, identities) is None
    assert authenticate(identities[2].bearer, identities).name == 'operator'
    monkeypatch.setenv(P + 'ORCHESTRATOR_KEY_NAMES', 'unknown')
    with pytest.raises(ValueError):
        load_configuration()


def test_default_sql_prefix_migration_preserves_projects_and_operation(db):
    old = Database(db.engine.url, 'mcp_switcher')
    new = Database(db.engine.url)
    try:
        old.migrate()
        old.initialize_owners(['alice'])
        project = old.save_project('alice', ProjectInput(name='Kept', selection=Selection(servers={'tasks': None})))
        with old.lease('alice', 'active', {'servers': {'tasks': None}}) as operation:
            pass
        new.migrate()
        assert new.list_projects('alice')[0]['id'] == project['id']
        assert new.operation('alice')['id'] == operation
        assert new.operation('alice')['status'] == 'pending'
        new.migrate()
        assert len(new.list_projects('alice')) == 1
    finally:
        new.metadata.drop_all(new.engine)
        with new.engine.begin() as conn:
            conn.exec_driver_sql('DROP TABLE IF EXISTS litellm_mcp_switcher_alembic_version')
        old.close()
        new.close()
