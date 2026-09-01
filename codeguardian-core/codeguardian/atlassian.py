from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from codeguardian.config import get_atlassian_mcp_auth, get_atlassian_mcp_url


@asynccontextmanager
async def atlassian_rovo_session():
    async with httpx.AsyncClient(
            auth=get_atlassian_mcp_auth(),
            follow_redirects=True,
    ) as custom_client:
        async with streamable_http_client(
                get_atlassian_mcp_url(),
                http_client=custom_client,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
