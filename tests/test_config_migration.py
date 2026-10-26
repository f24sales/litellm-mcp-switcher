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
    assert 'LITELLM_MCP_SWITCHER_KEY_NAME_02=bob' in new
    assert 'LITELLM_MCP_SWITCHER_KEY_BEARER_02=sk-456' in new
    assert 'LITELLM_MCP_SWITCHER_DB_NAME=mcp_switcher' in new
    assert 'LITELLM_MCP_SWITCHER_DB_PREFIX=litellm_mcp_switcher' in new
    assert 'LITELLM_MCP_SWITCHER_ADDITIONAL_LINE_02=ReadOnly=true' in new
    assert new.startswith('# private values\n')
    assert migrate_text(new) == new


def test_configuration_migration_rejects_collision():
    with pytest.raises(ValueError):
        migrate_text('USER_NAME=alice\nLITELLM_MCP_SWITCHER_KEY_NAME=bob\n')


def test_custom_table_prefix_is_preserved():
    assert migrate_text('MCP_SWITCHER_DB_PREFIX=custom\n') == 'LITELLM_MCP_SWITCHER_DB_PREFIX=custom\n'
