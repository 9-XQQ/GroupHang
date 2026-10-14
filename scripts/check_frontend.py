"""Check inline JavaScript with Node without opening a browser or loading APIs."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current = None
        self.is_module = False

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attrs = dict(attrs)
        kind = attrs.get("type", "").lower()
        if "src" not in attrs and kind in ("", "text/javascript", "application/javascript", "module"):
            self.current = []
            self.is_module = kind == "module"

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append(("".join(self.current), self.is_module))
            self.current = None


def check(path: Path) -> int:
    node = shutil.which("node")
    if node is None:
        print("JavaScript检查失败：请先安装Node.js并加入PATH。")
        return 2
    parser = InlineScripts()
    parser.feed(path.read_text(encoding="utf-8"))
    if not parser.scripts:
        print("JavaScript检查失败：未找到内嵌脚本。")
        return 1
    with tempfile.TemporaryDirectory(prefix="trip_frontend_check_") as folder:
        for index, (source, is_module) in enumerate(parser.scripts):
            script = Path(folder) / (str(index) + (".mjs" if is_module else ".js"))
            script.write_text(source, encoding="utf-8")
            result = subprocess.run([node, "--check", str(script)], timeout=15)
            if result.returncode:
                return result.returncode
    print(f"JavaScript语法检查通过：{len(parser.scripts)}个内嵌脚本。")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path(__file__).resolve().parent.parent / "frontend" / "index.html")
    args = parser.parse_args()
    return check(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
