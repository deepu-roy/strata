"""`code-kg mcp http --port` overrides MCP_HTTP_PORT (avoid clashing with other apps)."""

from typer.testing import CliRunner

from code_kg import cli


def test_mcp_port_and_host_flags_override_settings(monkeypatch):
    monkeypatch.setenv("NEO4J__URI", "bolt://unused:7687")
    monkeypatch.setenv("NEO4J__USER", "u")
    monkeypatch.setenv("NEO4J__PASSWORD", "p")
    monkeypatch.setenv("MCP_HTTP_PORT", "8765")
    seen = {}

    class _Server:
        def run(self, transport):
            seen["transport"] = transport

    def fake_create_server(settings):
        seen["port"] = settings.mcp_http_port
        seen["host"] = settings.mcp_http_host
        return _Server()

    monkeypatch.setattr("code_kg.mcp.server.create_server", fake_create_server)
    result = CliRunner().invoke(cli.app, ["mcp", "http", "--port", "8791", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert seen == {"port": 8791, "host": "0.0.0.0", "transport": "streamable-http"}


def test_mcp_default_port_from_env(monkeypatch):
    monkeypatch.setenv("NEO4J__URI", "bolt://unused:7687")
    monkeypatch.setenv("NEO4J__USER", "u")
    monkeypatch.setenv("NEO4J__PASSWORD", "p")
    monkeypatch.setenv("MCP_HTTP_PORT", "8812")
    seen = {}

    class _Server:
        def run(self, transport):
            pass

    monkeypatch.setattr(
        "code_kg.mcp.server.create_server",
        lambda s: seen.setdefault("port", s.mcp_http_port) and _Server(),
    )
    CliRunner().invoke(cli.app, ["mcp", "http"])
    assert seen["port"] == 8812
