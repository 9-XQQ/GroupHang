"""Check inline and local external JavaScript without opening APIs."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, unquote


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.sources = []
        self.current = None
        self.is_module = False

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attrs = dict(attrs)
        kind = (attrs.get("type") or "").lower()
        if kind not in ("", "text/javascript", "application/javascript", "module"):
            return
        if "src" in attrs:
            self.sources.append((attrs["src"], kind == "module"))
        else:
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
    path = path.resolve()
    parser = InlineScripts()
    parser.feed(path.read_text(encoding="utf-8"))
    sources = list(parser.scripts)
    for src, is_module in parser.sources:
        parsed = urlsplit(src)
        if parsed.scheme or parsed.netloc:
            continue  # Never fetch remote scripts during syntax verification.
        relative = unquote(parsed.path).lstrip("/")
        script = (path.parent / relative).resolve()
        if not script.is_relative_to(path.parent):
            raise ValueError("本地脚本不能越出页面目录")
        sources.append((script.read_text(encoding="utf-8"), is_module))
    if not sources:
        print("JavaScript检查失败：未找到脚本。")
        return 1
    with tempfile.TemporaryDirectory(prefix="trip_frontend_check_") as folder:
        for index, (source, is_module) in enumerate(sources):
            script = Path(folder) / (str(index) + (".mjs" if is_module else ".js"))
            script.write_text(source, encoding="utf-8")
            result = subprocess.run([node, "--check", str(script)], timeout=15)
            if result.returncode:
                return result.returncode
    print(f"{path.name}: JavaScript语法检查通过，{len(sources)}个脚本。")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    paths = [args.path] if args.path else sorted(
        (Path(__file__).resolve().parent.parent / "frontend").glob("*.html"))
    for path in paths:
        result = check(path)
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
