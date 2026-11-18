"""Build the private plugin; share one workflow source with MCP initialize."""
import argparse
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Output ZIP outside plugin source")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "plugin/chatnovella"
    output = args.output.resolve()
    if output.is_relative_to(source):
        parser.error("Output must be outside the plugin source directory")
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == source.name
    assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
    json.loads((source / "mcp.json").read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("plugin.json", "mcp.json", "skills/chatnovella/SKILL.md"):
            archive.write(source / name, "chatnovella/" + name)
        archive.write(root / "docs/PLUGIN_INSTRUCTIONS.md",
                      "chatnovella/skills/chatnovella/references/workflow.md")
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == 4
        assert archive.testzip() is None
        for name in ("chatnovella/plugin.json", "chatnovella/mcp.json"):
            json.loads(archive.read(name))
    print(output)


if __name__ == "__main__":
    main()
