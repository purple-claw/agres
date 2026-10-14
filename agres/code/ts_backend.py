"""Tree-sitter backend: exact symbols / scopes / calls / bases for ~15 languages.

Optional. If ``tree_sitter`` or a grammar wheel is missing, ``parse`` returns
None and ``langs.parse_source`` falls back to the regex scanner. Imports are
still extracted by the (language-specific) regex extractors in ``langs``; they
are position-independent and robust, while symbols/calls/bases benefit most from
a real syntax tree.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any, Optional

from .langs import CONTAINERS, KEYWORDS, MAX_REFS, SPECS, Facts, Sym, _doc_before
from ..util import sha1_text, truncate

try:  # pragma: no cover - exercised in envs with tree-sitter
    from tree_sitter import Language, Parser
    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False

# language key -> (module, factory attr)
GRAMMARS = {
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "scala": ("tree_sitter_scala", "language"),
    "dart": ("tree_sitter_dart", "language"),
    "swift": ("tree_sitter_swift", "language"),
}
_parsers: dict[str, Any] = {}

NAME_TYPES = ("identifier", "type_identifier", "simple_identifier", "name", "constant", "property_identifier")
REF_TYPES = {"identifier", "type_identifier", "property_identifier", "field_identifier", "constant",
             "simple_identifier", "shorthand_property_identifier"}

# per-language node-type tables ------------------------------------------------
# containers: node type -> kind ; funcs: node type -> kind ; calls: node types
CFG: dict[str, dict] = {}
_JS = dict(
    containers={"class_declaration": "class", "abstract_class_declaration": "class", "class": "class",
                "interface_declaration": "interface", "enum_declaration": "enum"},
    leaves={"type_alias_declaration": "type"},
    funcs={"function_declaration": "function", "generator_function_declaration": "function",
           "method_definition": "method"},
    calls={"call_expression": "function", "new_expression": "constructor"},
)
CFG["javascript"] = CFG["typescript"] = CFG["tsx"] = _JS
CFG["go"] = dict(containers={}, leaves={}, funcs={"function_declaration": "function", "method_declaration": "method"},
                 calls={"call_expression": "function"})
CFG["rust"] = dict(containers={"struct_item": "struct", "enum_item": "enum", "trait_item": "trait", "impl_item": "impl"},
                   leaves={"type_item": "type"}, funcs={"function_item": "function"},
                   calls={"call_expression": "function"})
CFG["java"] = dict(containers={"class_declaration": "class", "interface_declaration": "interface",
                               "enum_declaration": "enum", "record_declaration": "record"},
                   leaves={}, funcs={"method_declaration": "method", "constructor_declaration": "method"},
                   calls={"method_invocation": "*java", "object_creation_expression": "type"})
CFG["csharp"] = dict(containers={"class_declaration": "class", "interface_declaration": "interface",
                                 "struct_declaration": "struct", "enum_declaration": "enum", "record_declaration": "record"},
                     leaves={}, funcs={"method_declaration": "method", "constructor_declaration": "method"},
                     calls={"invocation_expression": "function", "object_creation_expression": "type"})
_C = dict(containers={"struct_specifier": "struct", "class_specifier": "class", "enum_specifier": "enum",
                      "namespace_definition": "namespace"},
          leaves={}, funcs={"function_definition": "function"}, calls={"call_expression": "function"})
CFG["c"] = CFG["cpp"] = _C
CFG["ruby"] = dict(containers={"class": "class", "module": "module"}, leaves={},
                   funcs={"method": "function", "singleton_method": "function"}, calls={"call": "*ruby"})
CFG["php"] = dict(containers={"class_declaration": "class", "interface_declaration": "interface", "trait_declaration": "trait"},
                  leaves={}, funcs={"function_definition": "function", "method_declaration": "method"},
                  calls={"function_call_expression": "function", "member_call_expression": "*php", "scoped_call_expression": "*php",
                         "object_creation_expression": "type"})
CFG["kotlin"] = dict(containers={"class_declaration": "class", "object_declaration": "object"}, leaves={},
                     funcs={"function_declaration": "function"}, calls={"call_expression": "*first"})
CFG["scala"] = dict(containers={"class_definition": "class", "object_definition": "object", "trait_definition": "trait"},
                    leaves={}, funcs={"function_definition": "function"}, calls={"call_expression": "function"})
CFG["dart"] = dict(containers={"class_definition": "class", "mixin_declaration": "mixin", "enum_declaration": "enum",
                               "extension_declaration": "extension"}, leaves={},
                   funcs={"function_signature": "function", "method_signature": "method"}, calls={})
CFG["swift"] = dict(containers={"class_declaration": "class", "protocol_declaration": "protocol"}, leaves={},
                    funcs={"function_declaration": "function"}, calls={"call_expression": "*first"})


def available(lang: str) -> bool:
    return AVAILABLE and lang in GRAMMARS and _get_parser(lang) is not None


def _get_parser(lang: str):
    if lang in _parsers:
        return _parsers[lang]
    p = None
    try:
        mod, fn = GRAMMARS[lang]
        p = Parser(Language(getattr(importlib.import_module(mod), fn)()))
    except Exception:
        p = None
    _parsers[lang] = p
    return p


def _txt(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _name_of(node, src: bytes) -> Optional[str]:
    n = node.child_by_field_name("name")
    if n is None:
        for c in node.children:
            if c.type in NAME_TYPES:
                n = c
                break
    return _txt(n, src) if n is not None else None


def _declarator_name(node, src: bytes) -> Optional[str]:
    """C/C++: function_definition -> declarator chain -> identifier."""
    d = node.child_by_field_name("declarator")
    depth = 0
    while d is not None and depth < 8:
        if d.type in ("identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name"):
            return _txt(d, src).split("::")[-1]
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            for c in d.children:
                if c.type in ("identifier", "field_identifier", "qualified_identifier"):
                    return _txt(c, src).split("::")[-1]
            return None
        d = nxt
        depth += 1
    return None


def _callee_name(text: str) -> Optional[str]:
    t = text.replace("::", ".").replace("->", ".").replace("?.", ".").replace("\\", ".")
    t = "".join(t.split())
    if not t or len(t) > 80:
        return None
    if "(" in t or "[" in t or "{" in t or "<" in t:
        tail = t.rsplit(".", 1)[-1]
        tail = "".join(ch for ch in tail if ch.isalnum() or ch in "_$")
        return "." + tail if tail else None
    return t.lstrip(".") if t.startswith("...") else t


def _java_like_call(node, src: bytes, kind: str) -> Optional[str]:
    if kind == "*java":
        obj, nm = node.child_by_field_name("object"), node.child_by_field_name("name")
        n = _txt(nm, src) if nm is not None else None
        if n is None:
            return None
        return _callee_name(f"{_txt(obj, src)}.{n}") if obj is not None else n
    if kind == "*php":
        obj = node.child_by_field_name("object") or node.child_by_field_name("scope")
        nm = node.child_by_field_name("name")
        n = _txt(nm, src) if nm is not None else None
        if n is None:
            return None
        return _callee_name(f"{_txt(obj, src).lstrip('$')}.{n}") if obj is not None else n
    if kind == "*ruby":
        recv, m = node.child_by_field_name("receiver"), node.child_by_field_name("method")
        n = _txt(m, src) if m is not None else None
        if n is None:
            return None
        return _callee_name(f"{_txt(recv, src)}.{n}") if recv is not None else n
    if kind == "*first":
        c = node.children[0] if node.children else None
        return _callee_name(_txt(c, src)) if c is not None else None
    return None


def _bases(node, src: bytes, lang: str) -> list[str]:
    out: list[str] = []

    def add(n) -> None:
        t = _callee_name(_txt(n, src))
        if t and len(t) < 60:
            out.append(t.split("<")[0])

    if lang in ("javascript", "typescript", "tsx"):
        for c in node.children:
            if c.type in ("class_heritage", "extends_clause", "extends_type_clause"):
                stack = [c]
                while stack:
                    x = stack.pop()
                    if x.type in ("identifier", "type_identifier", "member_expression", "nested_type_identifier"):
                        add(x)
                    elif x.type in ("class_heritage", "extends_clause", "implements_clause", "extends_type_clause",
                                    "generic_type"):
                        stack.extend(reversed(x.named_children[:1] if x.type == "generic_type" else x.named_children))
    elif lang == "java":
        for fld in ("superclass", "interfaces"):
            n = node.child_by_field_name(fld)
            if n is not None:
                for t in _find(n, ("type_identifier", "scoped_type_identifier")):
                    add(t)
    elif lang in ("csharp", "kotlin", "scala", "php", "swift", "cpp", "dart"):
        for c in node.children:
            if c.type in ("base_list", "base_clause", "delegation_specifiers", "extends_clause", "base_class_clause",
                          "superclass", "inheritance_specifier", "class_parents", "interfaces", "mixins"):
                for t in _find(c, ("identifier", "type_identifier", "name", "qualified_name", "user_type")):
                    add(t)
    elif lang == "ruby":
        sc = node.child_by_field_name("superclass")
        if sc is not None:
            for t in _find(sc, ("constant", "scope_resolution")):
                add(t)
                break
    elif lang == "rust":
        tr = node.child_by_field_name("trait")
        if tr is not None:
            add(tr)
    return list(dict.fromkeys(out))


def _find(node, types: tuple, limit: int = 12) -> list:
    out, stack = [], [node]
    while stack and len(out) < limit:
        n = stack.pop()
        if n.type in types:
            out.append(n)
            continue
        stack.extend(reversed(n.children))
    return out


def parse(src_text: str, lang: str) -> Optional[Facts]:
    """Parse with tree-sitter; None if unavailable / unsupported (caller falls back)."""
    if not available(lang) or lang not in CFG:
        return None
    parser = _get_parser(lang)
    src = src_text.encode("utf-8", "replace")
    lines = src_text.splitlines()
    tree = parser.parse(src)
    root = tree.root_node
    cfg = CFG[lang]
    base_lang = "typescript" if lang == "tsx" else lang
    f = Facts(lang=base_lang, loc=len(lines))
    f.extra["parser"] = "tree-sitter"
    spec = SPECS.get(base_lang)
    if spec and spec.imports:
        try:
            f.imports = spec.imports(src_text)
        except Exception:
            f.imports = []
    syms: list[Sym] = []
    seen_q: dict[str, int] = {}
    refs: dict[str, None] = {}
    has_main = False
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 6000))

    def add_call(owner: Optional[Sym], raw: Optional[str]) -> None:
        if not raw or raw in KEYWORDS:
            return
        lst = f.calls.setdefault(owner.qualname if owner else "", [])
        if raw not in lst:
            lst.append(raw)

    def mk(node, name: str, kind: str, parent_q: Optional[str], hidden: bool = False) -> Sym:
        q = f"{parent_q}.{name}" if parent_q else name
        if not hidden:
            n = seen_q.get(q, 0)
            seen_q[q] = n + 1
            if n:
                q = f"{q}#{n + 1}"
        row = node.start_point[0]
        end = node.end_point[0] + 1
        sig = truncate(lines[row].strip().rstrip("{").strip(), 140) if row < len(lines) else name
        s = Sym(name=name, qualname=q, kind=kind, line=row + 1, end_line=end, sig=sig,
                doc=_doc_before(lines, row), parent=parent_q,
                body_hash=sha1_text("\n".join(lines[row:end]))[:16])
        if not hidden:
            syms.append(s)
        return s

    def callee_of(node, kind: str) -> Optional[str]:
        if kind.startswith("*"):
            return _java_like_call(node, src, kind)
        if kind == "constructor":
            c = node.child_by_field_name("constructor")
            return _callee_name(_txt(c, src)) if c is not None else None
        if kind == "type":
            c = node.child_by_field_name("type") or (node.named_children[0] if node.named_children else None)
            return _callee_name(_txt(c, src)) if c is not None else None
        c = node.child_by_field_name("function")
        return _callee_name(_txt(c, src)) if c is not None else None

    def go_receiver(node) -> Optional[str]:
        r = node.child_by_field_name("receiver")
        if r is None:
            return None
        ids = _find(r, ("type_identifier",))
        return _txt(ids[0], src) if ids else None

    def visit(node, parent: Optional[Sym], owner: Optional[Sym]) -> None:
        nonlocal has_main
        t = node.type
        in_func = owner is not None
        if t in cfg["containers"]:
            kind = cfg["containers"][t]
            name = None
            if t == "impl_item":
                ty = node.child_by_field_name("type")
                name = _txt(ty, src).split("<")[0].split("::")[-1] if ty is not None else None
            else:
                name = _name_of(node, src)
            if name and not in_func:
                hidden = kind in ("impl", "extension")
                sym = mk(node, name, kind, parent.qualname if parent else None, hidden)
                bs = _bases(node, src, lang)
                if bs:
                    f.bases.setdefault(sym.qualname, []).extend(b for b in bs if b not in f.bases.get(sym.qualname, []))
                for c in node.children:
                    visit(c, sym, owner)
                return
        elif t in cfg["leaves"] and not in_func:
            name = _name_of(node, src)
            if name:
                mk(node, name, cfg["leaves"][t], parent.qualname if parent else None)
        elif t in cfg["funcs"]:
            kind = cfg["funcs"][t]
            name = _declarator_name(node, src) if lang in ("c", "cpp") else _name_of(node, src)
            if lang == "go" and t == "method_declaration":
                pq = go_receiver(node)
            else:
                pq = parent.qualname if parent else None
            if name and not in_func:
                if parent is not None and parent.kind in CONTAINERS and kind == "function":
                    kind = "method"
                sym = mk(node, name, kind, pq)
                if name == "main" and kind == "function":
                    has_main = True
                for c in node.children:
                    visit(c, parent, sym)
                return
        elif t in cfg["calls"]:
            add_call(owner, callee_of(node, cfg["calls"][t]))
        elif lang in ("javascript", "typescript", "tsx") and t == "variable_declarator" and not in_func:
            val = node.child_by_field_name("value")
            nm = node.child_by_field_name("name")
            if val is not None and nm is not None and val.type in ("arrow_function", "function_expression", "function") \
                    and nm.type == "identifier":
                sym = mk(node, _txt(nm, src), "function", parent.qualname if parent else None)
                for c in node.children:
                    visit(c, parent, sym)
                return
        elif lang in ("javascript", "typescript", "tsx") and t in ("public_field_definition", "field_definition") \
                and not in_func and parent is not None:
            val = node.child_by_field_name("value")
            nm = node.child_by_field_name("name") or node.child_by_field_name("property")
            if val is not None and nm is not None and val.type in ("arrow_function", "function_expression"):
                sym = mk(node, _txt(nm, src), "method", parent.qualname)
                for c in node.children:
                    visit(c, parent, sym)
                return
        elif t in REF_TYPES:
            w = _txt(node, src)
            if len(w) > 2 and w not in KEYWORDS and len(refs) < MAX_REFS * 2:
                refs.setdefault(w, None)
            return
        for c in node.children:
            visit(c, parent, owner)

    try:
        visit(root, None, None)
    finally:
        sys.setrecursionlimit(old_limit)
    f.symbols = syms
    f.refs = list(refs)[:MAX_REFS]
    if has_main and lang in ("go", "rust", "c", "cpp", "dart", "kotlin", "swift"):
        f.entry = True
    return f
