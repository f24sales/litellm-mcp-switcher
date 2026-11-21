from fastmcp import Client

from mcp_switcher.dummy_mcp import create_dummy_mcp


async def test_only_hello_is_exposed():
    async with Client(create_dummy_mcp()) as client:
        assert [tool.name for tool in await client.list_tools()] == ["hello"]
        response = await client.call_tool("hello", {})
        assert not response.is_error and response.data == "Hello World"
