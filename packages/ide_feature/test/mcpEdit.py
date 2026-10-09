"""External official MCP SDK client used by real GUI parity acceptance."""
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    request=json.loads(sys.argv[1])
    async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','modelbuilder.mcp_server'],env=dict(os.environ))) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            read=await session.call_tool('recipe_get',{'project_id':request['project_id'],'recipe_id':request['recipe_id']})
            if read.isError: raise RuntimeError('MCP recipe_get failed')
            recipe=read.structuredContent
            if recipe is None: recipe=json.loads(read.content[0].text)
            if 'name' in request:
                recipe['name']=request['name']
                changed=await session.call_tool('recipe_update',{'project_id':recipe['project_id'],'recipe_id':recipe['recipe_id'],'recipe':recipe,'expected_revision':recipe['revision'],'idempotency_key':request['idempotency_key']})
                if changed.isError: raise RuntimeError('MCP recipe_update failed')
                recipe=changed.structuredContent or json.loads(changed.content[0].text)
            print(json.dumps(recipe))

asyncio.run(main())
