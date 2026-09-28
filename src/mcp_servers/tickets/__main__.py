from mcp_servers.tickets.server import mcp

if __name__ == "__main__":
    mcp.run()  # stdio transport: the gateway talks to this process over stdin/stdout
