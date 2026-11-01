"""MCP (Model Context Protocol) layer for Ghost."""

from ghost.mcp.client import MCPClient
from ghost.mcp.protocol import MCPError, MCPNotification, MCPRequest, MCPResponse
from ghost.mcp.server import MCPServer
from ghost.mcp.transport import (
    InProcessTransport,
    MCPTransport,
    SSETransport,
    StdioTransport,
    StreamableHTTPTransport,
)

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPNotification",
    "MCPRequest",
    "MCPResponse",
    "MCPServer",
    "MCPTransport",
    "InProcessTransport",
    "SSETransport",
    "StdioTransport",
    "StreamableHTTPTransport",
]
