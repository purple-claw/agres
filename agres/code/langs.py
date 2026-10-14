"""Source -> structured facts (symbols, imports, references, calls, bases).

Python is parsed with the real ``ast`` (exact). Other languages use a
table-driven, scope-aware scanner that strips comments/strings first and tracks
brace depth (or indentation) so methods get their owning class and every symbol
gets an end line. It is a heuristic by design (no native deps), but isolated
behind ``parse_source`` so a tree-sitter backend can replace it per language.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..util import sha1_text, truncate

MAX_REFS = 1500

# ------------------------------------------------------------------ models


@dataclass
class Sym:
    name: str
    qualname: str
    kind: str
    line: int
    end_line: Optional[int] = None
    sig: str = ""
    doc: str = ""
    parent: Optional[str] = None
    decorators: list = field(default_factory=list)
    body_hash: str = ""


@dataclass
class Imp:
    raw: str
    names: list = field(default_factory=list)  # [(orig, alias)]
    level: int = 0
    line: int = 0
    lang: str = ""


@dataclass
class Facts:
    lang: str = ""
    symbols: list = field(default_factory=list)
    imports: list = field(default_factory=list)
    refs: list = field(default_factory=list)
    calls: dict = field(default_factory=dict)  # owner qualname ('' = module level) -> [raw call names]
    bases: dict = field(default_factory=dict)  # class qualname -> [raw base names]
    loc: int = 0
    doc: str = ""
    entry: bool = False
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "lang": self.lang,
            "symbols": [s.__dict__ for s in self.symbols],
            "imports": [i.__dict__ for i in self.imports],
            "refs": self.refs,
            "calls": self.calls,
            "bases": self.bases,
            "loc": self.loc,
            "doc": self.doc,
            "entry": self.entry,
            "extra": self.extra,
        }

    @staticmethod
    def from_json(d: dict) -> "Facts":
        f = Facts(lang=d.get("lang", ""), refs=d.get("refs", []), calls=d.get("calls", {}),
                  bases=d.get("bases", {}), loc=d.get("loc", 0), doc=d.get("doc", ""),
                  entry=d.get("entry", False), extra=d.get("extra", {}))
        f.symbols = [Sym(**s) for s in d.get("symbols", [])]
        f.imports = [Imp(**{**i, "names": [tuple(n) for n in i.get("names", [])]}) for i in d.get("imports", [])]
        return f


EXT_LANG = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".vue": "javascript", ".svelte": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".cs": "csharp", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".hh": "cpp", ".rb": "ruby", ".php": "php", ".dart": "dart", ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".md": "markdown", ".markdown": "markdown",
    ".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".sql": "sql",
    ".html": "html", ".css": "css", ".scss": "css", ".xml": "xml", ".gradle": "gradle",
    ".bb": "bitbake", ".bbappend": "bitbake", ".bbclass": "bitbake", ".inc": "bitbake", ".conf": "bitbake",
    ".dts": "devicetree", ".dtsi": "devicetree",
}
MANIFESTS = {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pubspec.yaml", "composer.json"}


def detect_lang(path: str) -> str:
    base = os.path.basename(path)
    if base in MANIFESTS or re.match(r"requirements.*\.txt$", base):
        return "manifest"
    ext = os.path.splitext(base)[1].lower()
    return EXT_LANG.get(ext, "")


# ------------------------------------------------------------------ python


def _dotted(e: ast.AST) -> Optional[str]:
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        b = _dotted(e.value)
        return f"{b}.{e.attr}" if b else None
    return None


def _call_name(func: ast.AST) -> Optional[str]:
    d = _dotted(func)
    if d:
        return d
    if isinstance(func, ast.Attribute):
        return "." + func.attr  # method call on an unknown receiver
    return None


def _walk_no_defs(node: ast.AST):
    """ast.walk that does not descend into function/class bodies."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                todo.append(c)


def parse_python(src: str) -> Facts:
    tree = ast.parse(src)
    lines = src.splitlines()
    f = Facts(lang="python", loc=len(lines))
    mdoc = ast.get_docstring(tree)
    f.doc = truncate(mdoc.splitlines()[0], 160) if mdoc else ""

    def bh(n: ast.AST) -> str:
        end = getattr(n, "end_lineno", None) or n.lineno
        return sha1_text("\n".join(lines[n.lineno - 1 : end]))[:16]

    def collect_calls(owner: str, nodes) -> None:
        out = f.calls.setdefault(owner, [])
        for root in nodes:
            for n in ast.walk(root):
                if isinstance(n, ast.Call):
                    nm = _call_name(n.func)
                    if nm and nm not in out:
                        out.append(nm)

    def visit_func(node, parent: Optional[str]) -> None:
        q = f"{parent}.{node.name}" if parent else node.name
        doc = ast.get_docstring(node)
        try:
            args = ast.unparse(node.args)
        except Exception:  # pragma: no cover
            args = "..."
        ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        pre = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        f.symbols.append(Sym(
            name=node.name, qualname=q, kind="method" if parent else "function", line=node.lineno,
            end_line=getattr(node, "end_lineno", None), sig=truncate(f"{pre} {node.name}({args}){ret}", 160),
            doc=truncate(doc.splitlines()[0], 160) if doc else "", parent=parent,
            decorators=[truncate(ast.unparse(d), 60) for d in node.decorator_list], body_hash=bh(node),
        ))
        collect_calls(q, [node])

    def visit_class(node: ast.ClassDef, parent: Optional[str]) -> None:
        q = f"{parent}.{node.name}" if parent else node.name
        bases = [d for d in (_dotted(b) for b in node.bases) if d]
        doc = ast.get_docstring(node)
        f.symbols.append(Sym(
            name=node.name, qualname=q, kind="class", line=node.lineno,
            end_line=getattr(node, "end_lineno", None),
            sig=truncate(f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}", 160),
            doc=truncate(doc.splitlines()[0], 160) if doc else "", parent=parent,
            decorators=[truncate(ast.unparse(d), 60) for d in node.decorator_list], body_hash=bh(node),
        ))
        if bases:
            f.bases[q] = bases
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_func(child, q)
            elif isinstance(child, ast.ClassDef):
                visit_class(child, q)

    module_stmts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit_func(node, None)
        elif isinstance(node, ast.ClassDef):
            visit_class(node, None)
        else:
            module_stmts.append(node)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1 and \
                            sum(1 for s in f.symbols if s.kind == "const") < 40:
                        f.symbols.append(Sym(name=t.id, qualname=t.id, kind="const", line=node.lineno,
                                             end_line=getattr(node, "end_lineno", None),
                                             sig=truncate(lines[node.lineno - 1].strip(), 120),
                                             body_hash=bh(node)))
            if isinstance(node, ast.If):
                try:
                    if "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test):
                        f.entry = True
                except Exception:  # pragma: no cover
                    pass
    # module-level calls (excluding def bodies)
    mod_out = f.calls.setdefault("", [])
    for stmt in module_stmts:
        for n in _walk_no_defs(stmt):
            if isinstance(n, ast.Call):
                nm = _call_name(n.func)
                if nm and nm not in mod_out:
                    mod_out.append(nm)
    f.calls = {k: v for k, v in f.calls.items() if v}

    seen: dict[str, None] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                f.imports.append(Imp(raw=a.name, names=[("*mod", a.asname or a.name.split(".")[0])],
                                     line=n.lineno, lang="python"))
        elif isinstance(n, ast.ImportFrom):
            f.imports.append(Imp(raw=n.module or "", level=n.level or 0, line=n.lineno, lang="python",
                                 names=[(a.name, a.asname or a.name) for a in n.names]))
        elif isinstance(n, ast.Name):
            seen.setdefault(n.id, None)
        elif isinstance(n, ast.Attribute):
            seen.setdefault(n.attr, None)
    f.refs = [r for r in seen if len(r) > 2][:MAX_REFS]
    return f


# --------------------------------------------------------- generic scanner

KEYWORDS = set("""
if else elif for while do switch case default catch try finally return function def class typeof sizeof
await async yield super this self cls import export from as in is not and or void var let const static
public private protected final abstract override virtual extern inline struct enum interface trait impl fn
func type namespace using package module end begin then fi done echo true false null nil none undefined
delete throw throws instanceof lambda with pass break continue assert require include use where
string number boolean int float double char long short byte bool any unknown never readonly val
""".split())
# words that can never be a function/method *name* (control-flow look-alikes)
CONTROL = {"if", "else", "elif", "for", "while", "switch", "catch", "return", "do", "try", "finally",
           "function", "def", "class", "throw", "await", "yield", "typeof", "sizeof", "delete", "case", "with"}

CONTAINERS = {"class", "struct", "interface", "enum", "trait", "impl", "object", "module", "namespace",
              "protocol", "extension", "mixin", "record", "node"}
HIDDEN_CONTAINERS = {"impl", "extension"}

_NAME = r"(?P<name>[A-Za-z_$][\w$]*)"


@dataclass
class Spec:
    scope: str = "brace"  # brace | indent | none
    line_comments: tuple = ("//",)
    block_comment: Optional[tuple] = ("/*", "*/")
    char_only_single_quote: bool = False
    defs: list = field(default_factory=list)  # [(kind, regex)] top-level & container patterns
    methods: list = field(default_factory=list)  # [(kind, regex)] only inside containers
    imports: Optional[Callable[[str], list]] = None
    caps: dict = field(default_factory=dict)  # kind -> max symbols of that kind per file


def _re(p: str) -> re.Pattern:
    return re.compile(p)


def _js_clause(clause: str) -> list:
    names: list = []
    clause = clause.strip()
    m = re.search(r"\{([^}]*)\}", clause)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)
            mm = re.match(r"([\w$]+)(?:\s+as\s+([\w$]+))?", part)
            if mm:
                names.append((mm.group(1), mm.group(2) or mm.group(1)))
        clause = clause.replace(m.group(0), "")
    ns = re.search(r"\*\s+as\s+([\w$]+)", clause)
    if ns:
        names.append(("*ns", ns.group(1)))
        clause = clause.replace(ns.group(0), "")
    d = re.match(r"\s*(?:type\s+)?([\w$]+)\s*,?", clause)
    if d and d.group(1) not in ("type",):
        names.append(("default", d.group(1)))
    return names


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def _imports_js(src: str) -> list:
    out: list = []
    for m in re.finditer(r"""\bimport\s+(?P<c>[^;'"()]*?)\s*from\s*['"](?P<m>[^'"]+)['"]""", src):
        out.append(Imp(m.group("m"), _js_clause(m.group("c")), 0, _line_of(src, m.start()), "js"))
    for m in re.finditer(r"""\bimport\s*['"](?P<m>[^'"]+)['"]""", src):
        out.append(Imp(m.group("m"), [], 0, _line_of(src, m.start()), "js"))
    for m in re.finditer(r"""\bexport\s+(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s*from\s*['"](?P<m>[^'"]+)['"]""", src):
        out.append(Imp(m.group("m"), [], 0, _line_of(src, m.start()), "js"))
    for m in re.finditer(r"""(?:\bconst|\blet|\bvar)\s+(?P<c>\{[^}]*\}|[\w$]+)\s*=\s*(?:await\s+)?require\(\s*['"](?P<m>[^'"]+)['"]\s*\)""", src):
        c = m.group("c")
        names = _js_clause(c) if c.startswith("{") else [("default", c)]
        out.append(Imp(m.group("m"), names, 0, _line_of(src, m.start()), "js"))
    for m in re.finditer(r"""\b(?:require|import)\(\s*['"](?P<m>[^'"]+)['"]\s*\)""", src):
        if not any(i.raw == m.group("m") for i in out):
            out.append(Imp(m.group("m"), [], 0, _line_of(src, m.start()), "js"))
    return out


def _imports_go(src: str) -> list:
    out: list = []
    for m in re.finditer(r"(?m)^\s*import\s*\((?P<b>.*?)^\s*\)", src, re.S):
        for mm in re.finditer(r'(?:(?P<a>[\w.]+)\s+)?"(?P<m>[^"]+)"', m.group("b")):
            out.append(Imp(mm.group("m"), [], 0, _line_of(src, m.start()), "go"))
    for m in re.finditer(r'(?m)^\s*import\s+(?:(?P<a>[\w.]+)\s+)?"(?P<m>[^"]+)"', src):
        out.append(Imp(m.group("m"), [], 0, _line_of(src, m.start()), "go"))
    return out


def _imports_rust(src: str) -> list:
    out: list = []
    for m in re.finditer(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(?P<m>[\w:]+?)(?:::\{(?P<n>[^}]*)\}|::(?P<x>\w+|\*))?\s*;", src):
        names = []
        if m.group("n"):
            names = [(n.strip().split(" as ")[0], n.strip().split(" as ")[-1]) for n in m.group("n").split(",") if n.strip()]
        elif m.group("x"):
            names = [(m.group("x"), m.group("x"))]
        out.append(Imp(m.group("m"), names, 0, _line_of(src, m.start()), "rust"))
    for m in re.finditer(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<m>\w+)\s*;", src):
        out.append(Imp(m.group("m"), [], 0, _line_of(src, m.start()), "rust-mod"))
    return out


def _simple(pattern: str, lang: str, flags: int = re.M) -> Callable[[str], list]:
    rx = re.compile(pattern, flags)

    def fn(src: str) -> list:
        return [Imp(m.group("m"), [], 0, _line_of(src, m.start()), lang) for m in rx.finditer(src)]
    return fn


def _imports_ruby(src: str) -> list:
    out = [Imp(m.group("m"), [], 0, _line_of(src, m.start()), "ruby-rel")
           for m in re.finditer(r"""(?m)^\s*require_relative\s*\(?\s*['"](?P<m>[^'"]+)['"]""", src)]
    out += [Imp(m.group("m"), [], 0, _line_of(src, m.start()), "ruby")
            for m in re.finditer(r"""(?m)^\s*require\s*\(?\s*['"](?P<m>[^'"]+)['"]""", src)]
    return out


def _imports_php(src: str) -> list:
    out = [Imp(m.group("m"), [], 0, _line_of(src, m.start()), "php")
           for m in re.finditer(r"(?m)^\s*use\s+(?P<m>[\w\\]+)(?:\s+as\s+\w+)?\s*;", src)]
    out += [Imp(m.group("m"), [], 0, _line_of(src, m.start()), "path")
            for m in re.finditer(r"""(?:require|include)(?:_once)?\s*\(?\s*['"](?P<m>[^'"]+)['"]""", src)]
    return out


def _imports_md(src: str) -> list:
    out: list = []
    for m in re.finditer(r"\[[^\]]*\]\((?P<m>[^)#\s]+)(?:#[^)]*)?\)", src):
        t = m.group("m")
        if not re.match(r"[a-z]+:", t):
            out.append(Imp(t, [], 0, _line_of(src, m.start()), "path"))
    return out


def _imports_bb(src: str) -> list:
    out = [Imp(m.group("m"), [], 0, _line_of(src, m.start()), "bb")
           for m in re.finditer(r"(?m)^\s*(?:require|include)\s+(?P<m>[^\s#]+)", src)]
    for m in re.finditer(r"(?m)^\s*inherit(?:_defer)?\s+(?P<m>[^#\n]+)", src):
        out += [Imp(f"classes/{c}.bbclass", [], 0, _line_of(src, m.start()), "bb")
                for c in m.group("m").replace("\\", " ").split() if "${" not in c]
    return out


SPECS: dict[str, Spec] = {}

_JS_DEFS = [
    ("class", _re(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+" + _NAME +
                  r"(?:\s*<[^>{]*>)?(?:\s+extends\s+(?P<base>[\w$.]+))?")),
    ("interface", _re(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+" + _NAME + r"(?:\s*<[^>{]*>)?(?:\s+extends\s+(?P<base>[\w$.]+))?")),
    ("type", _re(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+" + _NAME + r"\s*(?:<[^=]*>)?\s*=")),
    ("enum", _re(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+" + _NAME)),
    ("function", _re(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*" + _NAME + r"\s*(?P<sig>[<(].*)?")),
    ("function", _re(r"^\s*(?:export\s+)?(?:const|let|var)\s+" + _NAME +
                     r"\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b[^(]*\(|\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>)(?P<sig>.*)?")),
]
_JS_METHODS = [
    ("method", _re(r"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?\s*"
                   r"(?P<name>[A-Za-z_$#][\w$]*)\s*(?:<[^>]*>)?\s*(?P<sig>\([^)]*\)?.*?)\s*(?::\s*[^{;=]+)?\s*\{.*$")),
    ("method", _re(r"^\s*(?:(?:public|private|protected|static|readonly)\s+)*(?P<name>[A-Za-z_$#][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>")),
]
SPECS["javascript"] = SPECS["typescript"] = Spec(defs=_JS_DEFS, methods=_JS_METHODS, imports=_imports_js)
SPECS["go"] = Spec(
    char_only_single_quote=True,
    defs=[
        ("method", _re(r"^func\s*\(\s*(?:\w+\s+)?\*?(?P<recv>[\w.]+)(?:\[[^\]]*\])?\s*\)\s*" + _NAME + r"\s*(?:\[[^\]]*\])?\s*(?P<sig>\(.*)?")),
        ("function", _re(r"^func\s+" + _NAME + r"\s*(?:\[[^\]]*\])?\s*(?P<sig>\(.*)?")),
        ("struct", _re(r"^type\s+" + _NAME + r"\s+struct\b")),
        ("interface", _re(r"^type\s+" + _NAME + r"\s+interface\b")),
        ("type", _re(r"^type\s+" + _NAME + r"\s+(?!struct\b|interface\b)\S")),
    ], imports=_imports_go)
SPECS["rust"] = Spec(
    char_only_single_quote=True,
    defs=[
        ("function", _re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+" + _NAME + r"\s*(?:<[^>]*>)?\s*(?P<sig>\(.*)?")),
        ("struct", _re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+" + _NAME)),
        ("enum", _re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+" + _NAME)),
        ("trait", _re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+" + _NAME)),
        ("impl", _re(r"^\s*(?:unsafe\s+)?impl\s*(?:<[^>]*>)?\s*(?:[\w:<>, ]+?\s+for\s+)?(?P<name>[A-Za-z_]\w*)")),
        ("type", _re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?type\s+" + _NAME + r"\s*(?:<[^>]*>)?\s*=")),
    ], imports=_imports_rust)
_MODS = r"(?:(?:public|private|protected|internal|static|final|abstract|sealed|open|override|virtual|async|partial|data|inline|suspend|synchronized|native|default|readonly|extern|unsafe|new)\s+)*"
_JVM_CLASS = ("class", _re(r"^\s*" + _MODS + r"(?:class|record)\s+" + _NAME + r"(?:\s*<[^>{]*>)?(?:\s*\([^)]*\))?(?:\s+extends\s+(?P<base>[\w.]+))?"))
_JVM_COMMON = [
    _JVM_CLASS,
    ("interface", _re(r"^\s*" + _MODS + r"(?:@)?interface\s+" + _NAME)),
    ("enum", _re(r"^\s*" + _MODS + r"enum(?:\s+class)?\s+" + _NAME)),
    ("object", _re(r"^\s*" + _MODS + r"(?:object|trait)\s+" + _NAME)),
]
SPECS["java"] = Spec(defs=_JVM_COMMON, methods=[
    ("method", _re(r"^\s*" + _MODS + r"(?:<[^>]+>\s+)?(?:[\w<>\[\]?,.&]+\s+)+(?P<name>[A-Za-z_]\w*)\s*(?P<sig>\([^;]*\)?)\s*(?:throws\s+[\w.,\s]+)?\s*\{?\s*$")),
], imports=_simple(r"^\s*import\s+(?:static\s+)?(?P<m>[\w.]+?)(?:\.\*)?\s*;?\s*$", "java"))
SPECS["kotlin"] = Spec(defs=_JVM_COMMON + [
    ("function", _re(r"^\s*" + _MODS + r"fun\s+(?:<[^>]+>\s+)?(?:[\w.<>?]+\.)?" + _NAME + r"\s*(?P<sig>\(.*)?")),
], imports=_simple(r"^\s*import\s+(?P<m>[\w.]+?)(?:\.\*)?\s*$", "java"))
SPECS["scala"] = Spec(defs=_JVM_COMMON + [
    ("function", _re(r"^\s*" + _MODS + r"def\s+" + _NAME + r"\s*(?P<sig>[\[(].*)?")),
], imports=_simple(r"^\s*import\s+(?P<m>[\w.]+?)(?:\.[_{].*)?\s*$", "java"))
SPECS["csharp"] = Spec(defs=_JVM_COMMON + [
    ("namespace", _re(r"^\s*namespace\s+(?P<name>[\w.]+)")),
], methods=SPECS["java"].methods, imports=_simple(r"^\s*using\s+(?:static\s+)?(?P<m>[\w.]+)\s*;", "csharp"))
_C_FUNC = _re(r"^(?!\s*(?:if|for|while|switch|return|else|do|typedef|#)\b)(?:[A-Za-z_][\w:<>,*&\s]*?[\s*&])(?:[\w:]+::)?(?P<name>~?[A-Za-z_]\w*)\s*(?P<sig>\([^;{]*\)?)\s*(?:const|noexcept|override)?\s*\{?\s*$")
SPECS["c"] = SPECS["cpp"] = Spec(defs=[
    ("class", _re(r"^\s*(?:typedef\s+)?class\s+" + _NAME + r"(?:\s*:\s*(?:public|private|protected)?\s*(?P<base>[\w:]+))?")),
    ("struct", _re(r"^\s*(?:typedef\s+)?struct\s+" + _NAME + r"\s*(?:\{|$|:)")),
    ("enum", _re(r"^\s*(?:typedef\s+)?enum(?:\s+class)?\s+" + _NAME)),
    ("namespace", _re(r"^\s*namespace\s+" + _NAME)),
    ("function", _C_FUNC),
], imports=_simple(r'^\s*#\s*include\s+"(?P<m>[^"]+)"', "c"))
SPECS["ruby"] = Spec(scope="indent", line_comments=("#",), block_comment=None, defs=[
    ("class", _re(r"^\s*class\s+(?P<name>[A-Z][\w:]*)(?:\s*<\s*(?P<base>[\w:]+))?")),
    ("module", _re(r"^\s*module\s+(?P<name>[A-Z][\w:]*)")),
    ("function", _re(r"^\s*def\s+(?:self\.)?(?P<name>[\w?!=]+)\s*(?P<sig>\(.*)?")),
], imports=_imports_ruby)
SPECS["php"] = Spec(line_comments=("//", "#"), defs=[
    ("class", _re(r"^\s*(?:abstract\s+|final\s+)?class\s+" + _NAME + r"(?:\s+extends\s+(?P<base>[\w\\]+))?")),
    ("interface", _re(r"^\s*interface\s+" + _NAME)),
    ("trait", _re(r"^\s*trait\s+" + _NAME)),
    ("function", _re(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?" + _NAME + r"\s*(?P<sig>\(.*)?")),
], imports=_imports_php)
SPECS["dart"] = Spec(defs=[
    ("class", _re(r"^\s*(?:abstract\s+|base\s+|final\s+|sealed\s+|interface\s+)*class\s+" + _NAME + r"(?:<[^>{]*>)?(?:\s+extends\s+(?P<base>[\w.]+))?")),
    ("mixin", _re(r"^\s*mixin\s+" + _NAME)),
    ("enum", _re(r"^\s*enum\s+" + _NAME)),
    ("extension", _re(r"^\s*extension\s+" + _NAME + r"\s+on\s+")),
    ("function", _re(r"^(?!\s*(?:if|for|while|switch|return|else|catch)\b)(?:[A-Za-z_][\w<>?,.\s]*\s+)" + _NAME + r"\s*(?P<sig>\([^;]*\)?)\s*(?:async\*?|sync\*)?\s*(?:\{|=>)")),
], methods=[
    ("method", _re(r"^\s+(?!(?:if|for|while|switch|return|else|catch|await)\b)(?:(?:static|final|const|late|external|@override)\s+)*(?:[A-Za-z_][\w<>?,.\s]*\s+)?" + _NAME + r"\s*(?P<sig>\([^;]*\)?)\s*(?:async\*?|sync\*)?\s*(?:\{|=>)")),
], imports=_simple(r"""^\s*(?:import|export|part)\s+['"](?P<m>[^'"]+)['"]""", "dart"))
SPECS["swift"] = Spec(defs=[
    ("class", _re(r"^\s*(?:(?:public|private|internal|open|final|fileprivate)\s+)*class\s+" + _NAME + r"(?:\s*:\s*(?P<base>[\w.]+))?")),
    ("struct", _re(r"^\s*(?:(?:public|private|internal|fileprivate)\s+)*struct\s+" + _NAME)),
    ("enum", _re(r"^\s*(?:(?:public|private|internal|fileprivate|indirect)\s+)*enum\s+" + _NAME)),
    ("protocol", _re(r"^\s*(?:(?:public|private|internal)\s+)*protocol\s+" + _NAME)),
    ("extension", _re(r"^\s*(?:(?:public|private|internal)\s+)*extension\s+" + _NAME)),
    ("function", _re(r"^\s*(?:(?:public|private|internal|open|fileprivate|static|class|final|override|mutating)\s+)*func\s+" + _NAME + r"\s*(?:<[^>]*>)?\s*(?P<sig>\(.*)?")),
], imports=_simple(r"^\s*import\s+(?P<m>\w+)", "swift"))
SPECS["shell"] = Spec(line_comments=("#",), block_comment=None, defs=[
    ("function", _re(r"^\s*(?:function\s+)?(?P<name>[A-Za-z_][\w-]*)\s*\(\s*\)\s*\{?")),
    ("function", _re(r"^\s*function\s+(?P<name>[A-Za-z_][\w-]*)")),
], imports=_simple(r"^\s*(?:source|\.)\s+(?P<m>[\w./$\"'{}-]+)", "path"))
_BB_FUNC = _re(r"^\s*(?:(?:fakeroot|python)\s+)*(?P<name>[A-Za-z_][\w:.+\-${}]*)\s*\(\s*\)\s*\{?")
_BB_VAR = _re(r"^(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)(?:\[[\w\-.]+\])?(?::[\w\-.${}:+]+)*\s*"
              r"(?:\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)")
SPECS["bitbake"] = Spec(line_comments=("#",), block_comment=None, defs=[("function", _BB_FUNC), ("variable", _BB_VAR)],
                        caps={"variable": 60}, imports=_imports_bb)
_DT_NAME = r"[\w,.+@\-]+"
SPECS["devicetree"] = Spec(defs=[
    ("node", _re(r"^\s*(?P<name>[A-Za-z_]\w*)\s*:\s*[\w,.+@/\-]+\s*\{")),   # label: node@addr {
    ("node", _re(r"^\s*&(?P<name>[A-Za-z_]\w*)\s*\{")),                       # &label {
    ("node", _re(r"^\s*(?P<name>" + _DT_NAME + r"|/)\s*\{")),                  # node@addr {   and   / {
], imports=_simple(r'^\s*(?:#\s*include|/include/)\s+"(?P<m>[^"]+)"', "c"))
SPECS["markdown"] = Spec(scope="none", line_comments=(), block_comment=None, imports=_imports_md)
SPECS["sql"] = Spec(scope="none", line_comments=("--",), block_comment=("/*", "*/"), defs=[
    ("table", _re(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"`]+)")),
    ("function", _re(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:function|procedure)\s+(?P<name>[\w.\"`]+)")),
])
for _k in ("json", "toml", "yaml", "html", "css", "xml", "gradle"):
    SPECS[_k] = Spec(scope="none", line_comments=(), block_comment=None)

_STR_RX = {
    False: re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'),
    True: re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'|`[^`]*`'),
}
_CALL_RX = re.compile(r"(?<![\w$.:])(?P<recv>(?:[A-Za-z_$][\w$]*(?:\.|::))*)(?P<name>[A-Za-z_$][\w$]*)\s*\(")
_IDENT_RX = re.compile(r"[A-Za-z_$][\w$]{2,}")


def _strip(spec: Spec, lines: list[str]) -> list[str]:
    """Return per-line code with comments blanked and string contents removed."""
    out: list[str] = []
    in_block = False
    bc = spec.block_comment
    strx = _STR_RX[spec.char_only_single_quote]
    lc = [re.escape(c) for c in spec.line_comments]
    for line in lines:
        s = line
        if in_block:
            end = s.find(bc[1]) if bc else -1
            if end < 0:
                out.append("")
                continue
            s = " " * (end + len(bc[1])) + s[end + len(bc[1]):]
            in_block = False
        s = strx.sub(lambda m: '""', s)
        if bc:
            while True:
                st = s.find(bc[0])
                if st < 0:
                    break
                en = s.find(bc[1], st + len(bc[0]))
                if en < 0:
                    s = s[:st]
                    in_block = True
                    break
                s = s[:st] + " " + s[en + len(bc[1]):]
        for c in lc:
            m = re.search(c, s)
            if m:
                s = s[: m.start()]
        out.append(s)
    return out


def _doc_before(lines: list[str], idx: int) -> str:
    """First line of a leading comment block directly above line idx (0-based)."""
    j = idx - 1
    buf: list[str] = []
    while j >= 0 and lines[j].strip():
        t = lines[j].strip()
        if t.startswith(("///", "//", "#", "*", "/**", "/*")) and not t.startswith("#include") and not t.startswith("#!"):
            buf.append(t.lstrip("/#* ").rstrip("*/ "))
            j -= 1
        elif t.startswith("@"):  # annotations / decorators between doc and def
            j -= 1
        else:
            break
    buf = [b for b in reversed(buf) if b]
    return truncate(buf[0], 160) if buf else ""


def parse_generic(src: str, lang: str) -> Facts:
    spec = SPECS.get(lang) or Spec(scope="none")
    lines = src.splitlines()
    code = _strip(spec, lines)
    f = Facts(lang=lang, loc=len(lines))
    if spec.imports:
        try:
            f.imports = spec.imports(src)
        except re.error:  # pragma: no cover
            f.imports = []
    if lang == "markdown":
        heads = 0
        for i, ln in enumerate(lines):
            m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", ln)
            if m and heads < 40:
                heads += 1
                f.symbols.append(Sym(name=truncate(m.group(2), 80), qualname=f"{i+1}:{truncate(m.group(2), 60)}",
                                     kind="section", line=i + 1, sig=m.group(1) + " " + truncate(m.group(2), 100)))
        for ln in lines:
            if ln.strip():
                f.doc = truncate(ln.lstrip("# "), 160)
                break
        return f

    stack: list[tuple[Sym, int, int]] = []  # (sym, depth_before_open, indent)
    depth = 0
    pending: Optional[Sym] = None
    syms: list[Sym] = []
    qual_seen: dict[str, int] = {}
    kind_n: dict[str, int] = {}
    cur_calls: dict[str, list[str]] = {}
    refs: dict[str, None] = {}
    defined_here: set[str] = set()
    has_main = False

    for i, line in enumerate(code):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if spec.scope == "indent":
            while stack and indent <= stack[-1][2] and not stripped.startswith("end"):
                s, _d, _i = stack.pop()
                s.end_line = i
        opens, closes = line.count("{"), line.count("}")
        if pending is not None and stripped.startswith("{"):
            stack.append((pending, depth, indent))
            pending = None
        matched: Optional[Sym] = None
        hidden = False
        parent = stack[-1][0] if stack else None
        if parent is not None and parent.kind not in CONTAINERS:
            parent_ok = False  # inside a function body: don't record nested defs
        else:
            parent_ok = True
        if parent_ok and spec.scope != "none":
            cands = list(spec.defs)
            if parent is not None and parent.kind in CONTAINERS:
                cands = list(spec.methods) + cands
            for kind, rx in cands:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group("name")
                if name in CONTROL and kind in ("function", "method"):
                    continue
                if kind in spec.caps and kind_n.get(kind, 0) >= spec.caps[kind]:
                    continue
                if lang in ("javascript", "typescript") and kind == "method" and parent is None:
                    continue
                if kind == "function" and parent is not None and parent.kind in CONTAINERS:
                    kind = "method"
                gd = m.groupdict()
                recv = gd.get("recv")
                pq = parent.qualname if parent is not None else None
                if recv:
                    pq = recv
                    kind = "method"
                q = f"{pq}.{name}" if pq else name
                if kind not in HIDDEN_CONTAINERS:
                    n_seen = qual_seen.get(q, 0)
                    qual_seen[q] = n_seen + 1
                    if n_seen:
                        q = f"{q}#{n_seen + 1}"
                orig = lines[i] if i < len(lines) else line
                sig = truncate(orig.strip().rstrip("{").strip(), 140)
                if kind in HIDDEN_CONTAINERS:
                    hidden = True
                matched = Sym(name=name, qualname=q, kind=kind, line=i + 1, sig=sig,
                              doc=_doc_before(lines, i), parent=pq)
                kind_n[kind] = kind_n.get(kind, 0) + 1
                if gd.get("base") and kind in CONTAINERS | {"class", "interface"}:
                    f.bases.setdefault(q, []).append(gd["base"])
                break
        if matched is not None:
            if not hidden:
                syms.append(matched)
                defined_here.add(matched.name)
            if opens > closes or (spec.scope == "indent"):
                stack.append((matched, depth, indent))
            elif opens == closes and opens > 0:
                matched.end_line = i + 1
            elif spec.scope == "brace" and not stripped.endswith(";") and \
                    (matched.kind in CONTAINERS or matched.kind in ("function", "method")):
                pending = matched  # opening brace is on a following line
            else:
                matched.end_line = i + 1
            if matched.name == "main" and matched.kind == "function":
                has_main = True
            if matched.kind in ("function", "method") and ("{" in line or "=>" in line):
                body = line[line.index("{") + 1:] if "{" in line else line[line.index("=>") + 2:]
                for cm in _CALL_RX.finditer(body):
                    nm = cm.group("name")
                    if nm in KEYWORDS:
                        continue
                    raw = ((cm.group("recv") + nm) if cm.group("recv") else nm).replace("::", ".")
                    lst = cur_calls.setdefault(matched.qualname, [])
                    if raw not in lst:
                        lst.append(raw)
        else:
            if parent is not None or stack:
                owner = None
                for s, _d, _i in reversed(stack):
                    if s.kind in ("function", "method"):
                        owner = s
                        break
                if owner is not None:
                    for cm in _CALL_RX.finditer(line):
                        nm = cm.group("name")
                        if nm in KEYWORDS:
                            continue
                        recv = cm.group("recv")
                        raw = ((recv + nm) if recv else nm).replace("::", ".")
                        lst = cur_calls.setdefault(owner.qualname, [])
                        if raw not in lst:
                            lst.append(raw)
            else:
                for cm in _CALL_RX.finditer(line):
                    nm = cm.group("name")
                    if nm in KEYWORDS:
                        continue
                    raw = ((cm.group("recv") + nm) if cm.group("recv") else nm).replace("::", ".")
                    lst = cur_calls.setdefault("", [])
                    if raw not in lst:
                        lst.append(raw)
        if spec.scope != "indent":
            depth += opens - closes
            while stack and depth <= stack[-1][1] and (opens or closes):
                s, _d, _i = stack.pop()
                s.end_line = i + 1
    for s, _d, _i in stack:
        s.end_line = s.end_line or len(lines)
    if pending is not None:
        pending.end_line = pending.line
    for s in syms:
        end = s.end_line or s.line
        s.body_hash = sha1_text("\n".join(lines[s.line - 1 : end]))[:16]
    f.symbols = syms
    f.calls = cur_calls
    for ln in code:
        for m in _IDENT_RX.finditer(ln):
            w = m.group(0)
            if w not in KEYWORDS:
                refs.setdefault(w, None)
        if len(refs) >= MAX_REFS * 2:
            break
    f.refs = [r for r in refs][:MAX_REFS]
    # entry-point heuristics
    if lang == "go" and re.search(r"(?m)^package\s+main\b", src) and has_main:
        f.entry = True
    elif lang == "rust" and has_main:
        f.entry = True
    elif lang in ("java", "kotlin", "scala", "csharp") and re.search(r"\bstatic\s+(?:void|int)\s+[Mm]ain\s*\(|fun\s+main\s*\(", src):
        f.entry = True
    elif lang == "dart" and has_main:
        f.entry = True
    elif lang in ("c", "cpp") and has_main:
        f.entry = True
    elif lang in ("javascript", "typescript") and re.search(r"require\.main\s*===\s*module|process\.argv|#!/usr/bin/env node", src):
        f.entry = True
    elif lang == "shell" and src.startswith("#!"):
        f.entry = True
    if not f.doc:
        for ln in lines[:8]:
            t = ln.strip()
            if t.startswith(("//", "#", "/*", "*", "///")) and not t.startswith(("#!", "#include", "#import")):
                f.doc = truncate(t.lstrip("/#* "), 160)
                break
    return f


# ----------------------------------------------------------------- manifests


def _toml_loads(text: str) -> dict:
    try:
        import tomllib  # py3.11+

        return tomllib.loads(text)
    except Exception:
        return {}


def parse_manifest(path: str, text: str) -> Facts:
    base = os.path.basename(path)
    f = Facts(lang="manifest", loc=text.count("\n") + 1)
    deps: list[str] = []
    extra: dict[str, Any] = {"manifest": base}
    try:
        if base == "package.json":
            d = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                deps += list((d.get(key) or {}).keys())
            extra.update(name=d.get("name"), description=d.get("description"), scripts=list((d.get("scripts") or {}).keys())[:12])
            ent = []
            if isinstance(d.get("main"), str):
                ent.append(d["main"])
            if isinstance(d.get("bin"), str):
                ent.append(d["bin"])
            elif isinstance(d.get("bin"), dict):
                ent += [v for v in d["bin"].values() if isinstance(v, str)]
            extra["entries"] = ent
        elif base == "pyproject.toml":
            d = _toml_loads(text)
            proj = d.get("project", {})
            extra.update(name=proj.get("name") or d.get("tool", {}).get("poetry", {}).get("name"),
                         description=proj.get("description"))
            reqs = list(proj.get("dependencies", []))
            for v in (proj.get("optional-dependencies") or {}).values():
                reqs += v
            reqs += list((d.get("tool", {}).get("poetry", {}).get("dependencies") or {}).keys())
            if not d:  # no tomllib: crude fallback
                reqs += re.findall(r'^\s*"([A-Za-z0-9_.\-]+)[^"]*",?\s*$', text, re.M)
            deps += [re.split(r"[<>=!~\[; ]", r.strip(), 1)[0] for r in reqs if isinstance(r, str)]
            deps = [x for x in deps if x and x.lower() != "python"]
            scripts = proj.get("scripts") or d.get("tool", {}).get("poetry", {}).get("scripts") or {}
            extra["entries"] = [str(v).split(":")[0].replace(".", "/") for v in scripts.values()]
        elif base.startswith("requirements"):
            for ln in text.splitlines():
                m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)", ln)
                if m and not ln.strip().startswith(("-", "#")):
                    deps.append(m.group(1))
        elif base == "go.mod":
            m = re.search(r"(?m)^module\s+(\S+)", text)
            extra["name"] = m.group(1) if m else None
            deps += re.findall(r"(?m)^\s*(?:require\s+)?([\w.\-]+\.[\w.\-]+/[\w./\-]+)\s+v[\w.\-+]+", text)
        elif base == "Cargo.toml":
            d = _toml_loads(text)
            extra.update(name=d.get("package", {}).get("name"), description=d.get("package", {}).get("description"))
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps += list((d.get(key) or {}).keys())
        elif base == "pubspec.yaml":
            m = re.search(r"(?m)^name:\s*(\S+)", text)
            dm = re.search(r"(?m)^description:\s*(.+)$", text)
            extra.update(name=m.group(1) if m else None, description=dm.group(1).strip() if dm else None)
            for block in ("dependencies", "dev_dependencies"):
                bm = re.search(rf"(?ms)^{block}:\s*\n((?:[ \t]+.*\n?|\n)*)", text)
                if bm:
                    deps += re.findall(r"(?m)^  ([A-Za-z_][\w]*):", bm.group(1))
            deps = [x for x in deps if x not in ("flutter", "flutter_test", "sdk")] + [x for x in deps if x in ("flutter",)]
        elif base == "composer.json":
            d = json.loads(text)
            deps += [k for k in (d.get("require") or {}) if k != "php"]
            extra.update(name=d.get("name"), description=d.get("description"))
    except (ValueError, KeyError, TypeError):
        pass
    seen = dict.fromkeys(x for x in deps if x)
    f.imports = [Imp(raw=d, lang="dep") for d in list(seen)[:150]]
    f.extra = extra
    f.doc = truncate(str(extra.get("description") or ""), 160)
    return f


# ---------------------------------------------------------------- entrypoint


def parse_source(path: str, text: str) -> tuple[Facts | None, str | None]:
    """Parse ``text`` of file ``path``. Returns (facts, error)."""
    lang = detect_lang(path)
    if not lang:
        return None, None
    try:
        if lang == "manifest":
            return parse_manifest(path, text), None
        if lang == "python":
            try:
                pf = parse_python(text)
                pf.extra["parser"] = "ast"
                return pf, None
            except (SyntaxError, ValueError, RecursionError) as e:
                f = parse_generic(text, "python")  # degraded: regex fallback
                return f, f"ast failed ({type(e).__name__}); regex fallback"
        if lang in SPECS:
            f = _parse_ts(path, text, lang)
            if f is not None:
                return f, None
            f = parse_generic(text, lang)
            f.extra["parser"] = "regex"
            return f, None
        f = Facts(lang=lang, loc=text.count("\n") + 1)
        return f, None
    except RecursionError:  # pragma: no cover
        return Facts(lang=lang, loc=text.count("\n") + 1), "recursion"


def _parse_ts(path: str, text: str, lang: str) -> Optional[Facts]:
    """Tree-sitter parse + shared heuristics; None -> caller uses regex scanner."""
    if os.environ.get("AGRES_PARSER") == "regex":
        return None
    try:
        from . import ts_backend
        key = "tsx" if path.lower().endswith(".tsx") else lang
        f = ts_backend.parse(text, key)
    except Exception:
        return None
    if f is None:
        return None
    if lang in ("javascript", "typescript") and re.search(r"require\.main\s*===\s*module|process\.argv|^#!/usr/bin/env node", text, re.M):
        f.entry = True
    if lang in ("java", "kotlin", "scala", "csharp") and re.search(r"\bstatic\s+(?:void|int)\s+[Mm]ain\s*\(|fun\s+main\s*\(", text):
        f.entry = True
    if lang == "shell" and text.startswith("#!"):
        f.entry = True
    for ln in text.splitlines()[:8]:
        t = ln.strip()
        if t.startswith(("//", "#", "/*", "*", "///")) and not t.startswith(("#!", "#include", "#import")):
            f.doc = truncate(t.lstrip("/#* "), 160)
            break
    return f


# Python regex fallback spec (used only when ast fails)
SPECS["python"] = Spec(scope="indent", line_comments=("#",), block_comment=None, defs=[
    ("class", _re(r"^\s*class\s+" + _NAME + r"(?:\s*\((?P<base>[\w.]+))?")),
    ("function", _re(r"^\s*(?:async\s+)?def\s+" + _NAME + r"\s*(?P<sig>\(.*)?")),
], imports=lambda src: [
    Imp(m.group(1) or m.group(2), [], 0, _line_of(src, m.start()), "python")
    for m in re.finditer(r"(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", src)])

_STDLIB_39 = """abc argparse array ast asyncio atexit base64 bisect builtins bz2 calendar cgi cmath cmd codecs collections
colorsys concurrent configparser contextlib contextvars copy copyreg csv ctypes curses dataclasses datetime dbm decimal
difflib dis doctest email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch fractions ftplib functools gc
getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http imaplib imghdr imp importlib inspect io
ipaddress itertools json keyword lib2to3 linecache locale logging lzma mailbox marshal math mimetypes mmap
multiprocessing netrc numbers operator optparse os pathlib pdb pickle pipes pkgutil platform plistlib poplib posix
pprint profile pstats pty pwd py_compile pydoc queue quopri random re readline reprlib resource runpy sched secrets
select selectors shelve shlex shutil signal site smtplib socket socketserver sqlite3 ssl stat statistics string
stringprep struct subprocess sys sysconfig syslog tarfile telnetlib tempfile termios textwrap threading time
timeit tkinter token tokenize tomllib trace traceback tracemalloc tty turtle types typing unicodedata unittest urllib
uu uuid venv warnings wave weakref webbrowser winreg wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
""".split()
STDLIB = set(getattr(sys, "stdlib_module_names", _STDLIB_39)) | {"__future__", "_thread"}
