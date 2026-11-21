from fastmcp import Client

from mcp_switcher.dummy_mcp import create_dummy_mcp


async def test_only_hello_world_is_exposed():
    async with Client(create_dummy_mcp()) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["hello_world"]
        assert not tools[0].description
        response = await client.call_tool("hello_world", {})
        assert not response.is_error and response.data == "hello world"
