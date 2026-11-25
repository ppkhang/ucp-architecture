"""Run it:
    python -m mcp_server register     make an agent at UCP and print its API key once
    python -m mcp_server              stdio, for Claude Code or Claude Desktop on this machine
    python -m mcp_server http         Streamable HTTP with OAuth, for a custom connector (through a tunnel)
"""
import os
import sys
from urllib.parse import urlparse

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette

from mcp_server.auth import SCOPE, OwnerAuth
from mcp_server.server import build_server
from mcp_server.ucp_client import UCPClient

MIN_PASSWORD = 12


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)  # hard rule 13

    ucp_api_url: str = "http://127.0.0.1:8000"
    ucp_api_key: str = ""
    mcp_port: int = 8765
    mcp_public_url: str = ""  # the tunnel's https address; empty means local only
    mcp_owner_password: str = ""


def http_app(ucp: UCPClient, public_url: str, password: str, port: int) -> Starlette:
    """The HTTP transport always asks for sign-in: a tunnel opened by mistake must not expose the agent."""
    if len(password) < MIN_PASSWORD:
        raise SystemExit(f"set MCP_OWNER_PASSWORD to at least {MIN_PASSWORD} characters")
    public_url = public_url.rstrip("/") or f"http://localhost:{port}"
    owner = OwnerAuth(public_url, password)
    server = build_server(ucp, auth_server_provider=owner, auth=AuthSettings(
        issuer_url=public_url, resource_server_url=owner.resource, validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                              default_scopes=[SCOPE])))
    server.custom_route("/login", methods=["GET", "POST"])(owner.login)
    public_host = urlparse(public_url).netloc
    security = TransportSecuritySettings(  # DNS rebinding protection, with the tunnel's host added
        allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[public_url, "https://claude.ai", "http://127.0.0.1:*", "http://localhost:*"])
    return server.streamable_http_app(stateless_http=True, transport_security=security)


def main(argv: list[str]) -> None:
    config, command = Config(), argv[0] if argv else "stdio"
    if command == "register":
        r = httpx.post(f"{config.ucp_api_url}/v1/agents", json={"name": "Claude via MCP"})
        r.raise_for_status()
        # shown once, to the person running this, to put in .env
        print(f"UCP_API_KEY={r.json()['api_key']}")
        return
    if command not in ("stdio", "http"):
        raise SystemExit(__doc__)
    if not config.ucp_api_key:
        raise SystemExit("set UCP_API_KEY (make one with: python -m mcp_server register)")
    ucp = UCPClient(httpx.Client(base_url=config.ucp_api_url, timeout=30), config.ucp_api_key)
    if command == "stdio":
        build_server(ucp).run("stdio")
    else:
        import uvicorn

        # bound to this machine only: the tunnel connects from here
        uvicorn.run(http_app(ucp, config.mcp_public_url, config.mcp_owner_password, config.mcp_port),
                    host="127.0.0.1", port=config.mcp_port, log_level=os.getenv("LOG_LEVEL", "warning"),
                    access_log=False)  # URLs here carry sign-in ids


if __name__ == "__main__":
    main(sys.argv[1:])
