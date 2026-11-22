"""Build either plugin with a lossless Skin Builder from the engine source."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


SKIN_PATH = "skills/chatnovella/references/skin_builder.md"
START = "<!-- BUNDLED_SKIN_START -->"
END = "<!-- BUNDLED_SKIN_END -->"


def sync_skin(root):
    skin = (root / "engine/skin_builder.md").read_bytes()
    sha = hashlib.sha256(skin).hexdigest()
    block = f"""{START}
Полный [Skin Builder](references/skin_builder.md) встроен в этот плагин.
SHA256 встроенной копии: `{sha}`.
На каждом ходе прочитай полный актуальный Skin Builder в свежем серверном
пакете, как и прежде. Если его file_hashes["engine/skin_builder.md"] совпадает
с указанным SHA256, это тот же полный текст: не загружай встроенную копию
второй раз. При расхождении используй полный актуальный серверный текст;
не смешивай версии. Встроенный файл читай при отдельном просмотре правил
плагина, когда свежий серверный текст не загружается. Он не заменяет обязательный
пакет, чтение всех чанков, проверку версии и сохранение хода. Не добавляй запросы,
повторные чтения или отдельные проверки только из-за наличия встроенного файла.
{END}"""
    for variant in ("chatnovella", "chatnovella-amvera"):
        source = root / "plugin" / variant
        target = source / SKIN_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(skin)
        skill_path = source / "skills/chatnovella/SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        if START in text:
            text, count = re.subn(re.escape(START) + r".*?" + re.escape(END),
                                 lambda _: block, text, flags=re.S)
            assert count == 1
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
        skill_path.write_text(text, encoding="utf-8")
    return skin


def build(root, output, variant="chatnovella"):
    source = root / "plugin" / variant
    output = output.resolve()
    if output.is_relative_to(root / "plugin"):
        raise ValueError("Output must be outside plugin source directories")
    skin = sync_skin(root)
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
    if variant == "chatnovella":
        assert manifest["name"] == source.name
        json.loads((source / "mcp.json").read_text(encoding="utf-8"))
        names = ["plugin.json", "mcp.json", "skills/chatnovella/SKILL.md", SKIN_PATH]
    else:
        names = ["plugin.json", ".app.json", ".codex-plugin/plugin.json",
                 "skills/chatnovella/SKILL.md", SKIN_PATH]
    prefix = manifest["name"] + "/"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(source / name, prefix + name)
        workflow = (root / "docs/PLUGIN_INSTRUCTIONS.md" if variant == "chatnovella"
                    else source / "skills/chatnovella/references/workflow.md")
        archive.write(workflow, prefix + "skills/chatnovella/references/workflow.md")
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == len(names) + 1
        assert archive.testzip() is None
        assert archive.read(prefix + SKIN_PATH) == skin
        for name in names:
            if name.endswith(".json"):
                json.loads(archive.read(prefix + name))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Output ZIP outside plugin source")
    parser.add_argument("--variant", choices=("chatnovella", "chatnovella-amvera"),
                        default="chatnovella")
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output, args.variant))


if __name__ == "__main__":
    main()
