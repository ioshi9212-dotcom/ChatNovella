"""Build the private ChatNovella V2 plugin only after its real URL is verified.

Usage: python -m novella_v2.build_plugin --url <actual HTTPS origin> --output <zip>
No credential argument, environment capture or token file is supported.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile


def build(url: str, output: Path) -> None:
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")
            or parts.port not in (None, 443)):
        raise ValueError("Use the verified public HTTPS origin without credentials, path or query")
    base = url.rstrip("/")
    subtitle = "Новеллы с отдельной памятью"
    assert len(subtitle) <= 30
    plugin = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "chatnovella-v2", "version": "0.1.0",
        "description": "Самостоятельные новеллы NV2_ с каноном, личной памятью, отношениями и открытыми арками.",
        "author": {"name": "ioshi9212-dotcom"},
        "extensions": {"com.openai": {"interface": {
            "displayName": "ChatNovella V2", "shortDescription": subtitle,
            "longDescription": "Новые игровые сессии с отдельной памятью персонажей, подтверждением канона, историей и откатом.",
            "developerName": "ioshi9212-dotcom",
            "capabilities": [],
            "defaultPrompt": "Начнём новую новеллу в ChatNovella V2.",
        }}},
    }
    mcp = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"chatnovella-v2": {
            "type": "streamable-http", "url": base + "/mcp",
            "extensions": {"com.openai": {
                "auth": {"type": "api_key", "headerScheme": "bearer"},
            }},
        }},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {"plugin.json": plugin, "mcp.json": mcp}
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(files)
        for name, value in files.items():
            assert json.loads(archive.read(name)) == value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.url, args.output)
    print(args.output.resolve())
