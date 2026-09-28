from mcp_servers.usage.server import mcp

if __name__ == "__main__":
    mcp.run()  # stdio transport: the gateway talks to this process over stdin/stdout
