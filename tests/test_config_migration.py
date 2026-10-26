import pytest

from mcp_switcher.migrate_config import migrate_text


def test_configuration_migration_preserves_secrets_and_existing_database():
    old = '''# private values
USER_NAME=alice
USER_SWITCHER_BEARER='a$token#not-a-comment'
USER_LITELLM_BEARER=sk-123
USER_NAME_02=bob
USER_LITELLM_BEARER_02=sk-456
MCP_SWITCHER_DB_NAME=mcp_switcher
MCP_SWITCHER_DB_PREFIX=mcp_switcher
ADDITIONAL_LINE_02=ReadOnly=true
'''
    new = migrate_text(old)
    assert "LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER='a$token#not-a-comment'" in new
    assert 'LITELLM_MCP_SWITCHER_BEARER_ALIAS_02=bob' in new
    assert 'LITELLM_MCP_SWITCHER_KEY_BEARER_02=sk-456' in new
    assert 'LITELLM_MCP_SWITCHER_DB_NAME=mcp_switcher' in new
    assert 'LITELLM_MCP_SWITCHER_DB_PREFIX=litellm_mcp_switcher' in new
    assert 'LITELLM_MCP_SWITCHER_ADDITIONAL_LINE_02=ReadOnly=true' in new
    assert new.startswith('# private values\n')
    assert migrate_text(new) == new


def test_configuration_migration_rejects_collision():
    with pytest.raises(ValueError):
        migrate_text('USER_NAME=alice\nLITELLM_MCP_SWITCHER_BEARER_ALIAS=bob\n')


def test_custom_table_prefix_is_preserved():
    assert migrate_text('MCP_SWITCHER_DB_PREFIX=custom\n') == 'LITELLM_MCP_SWITCHER_DB_PREFIX=custom\n'


def test_bearer_alias_rename_preserves_pairs_and_orchestrator_order():
    old = '''LITELLM_MCP_SWITCHER_KEY_NAME=main
LITELLM_MCP_SWITCHER_KEY_BEARER=sk-main
LITELLM_MCP_SWITCHER_KEY_ALWAYS_ON_MCPS=
LITELLM_MCP_SWITCHER_KEY_NAME_02=research
LITELLM_MCP_SWITCHER_KEY_BEARER_02=sk-research
LITELLM_MCP_SWITCHER_ORCHESTRATOR_KEY_NAMES=research,main
LITELLM_MCP_SWITCHER_ORCHESTRATOR_KEY_NAMES_02=main
'''
    expected = old.replace('LITELLM_MCP_SWITCHER_KEY_NAME', 'LITELLM_MCP_SWITCHER_BEARER_ALIAS').replace(
        'ORCHESTRATOR_KEY_NAMES', 'ORCHESTRATOR_BEARER_ALIASES')
    assert migrate_text(old) == expected
    assert migrate_text(expected) == expected


def test_old_and_new_bearer_alias_cannot_disagree():
    with pytest.raises(ValueError, match='conflicting'):
        migrate_text('LITELLM_MCP_SWITCHER_KEY_NAME=alice\nLITELLM_MCP_SWITCHER_BEARER_ALIAS=bob\n')
