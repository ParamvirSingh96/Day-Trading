"""Static regression guard: no unescaped interpolation reaches innerHTML.

Scope
-----
Every ``Frontend/*.js`` module builds its markup with template literals that are
assigned to ``innerHTML`` / ``outerHTML`` / ``insertAdjacentHTML`` (directly, or
via a local that is then assigned, or via ``rows.map(renderRow).join("")``).
This test lexes each file (comments, strings, regex literals and nested
template literals are all handled), finds every *HTML-shaped* template literal
and proves that each ``${...}`` inside it is safe, where "safe" means one of:

* wrapped in ``esc(...)`` (the shared helper in ``Frontend/utils.js``);
* a string / number literal, or another checked template literal;
* a provably numeric/boolean expression (arithmetic or comparison operator,
  ``Math.*``, ``Number()``, ``parseFloat()``, ``x.toFixed(n)``, ``x.length``,
  ``Date`` getters/formatters, ``+`` / ``||`` / ``??`` / ternary of safe parts);
* an identifier whose lexically-visible ``const``/``let`` declaration (and, for
  ``let``, every reassignment in scope) is itself safe -- function parameters,
  ``catch`` bindings, ``for..of`` bindings and destructured names are treated
  as opaque, so shadowing cannot launder a value;
* a call to a local function / arrow (or one imported from ``./utils.js``)
  every ``return`` of which is safe;
* ``items.map(cb).join("<literal>")`` where ``cb`` returns a checked template;
* or an entry of the explicit per-file ``ALLOWLIST`` below (every entry carries
  a justification and must be *used*, so stale entries fail the test too).

A template literal counts as HTML-shaped when its static text contains a tag
(``<x``, ``</x``, ``<!``), starts with an attribute (``title="``), is nested
inside an HTML-shaped template, or is the direct right-hand side of one of the
three sinks.  Inline ``on*=`` handlers and ``javascript:`` URLs are rejected
outright.

Standard library + pytest only.  ``python tests/test_frontend_escaping.py``
runs the same checks without pytest.
"""
from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "Frontend"
UTILS_MODULE = "./utils.js"

# The escaping axiom: these helpers are trusted to neutralise &, <, >, ", '.
ESC_FUNCS = {"esc", "escapeHtml"}

# ---------------------------------------------------------------------------
# Per-file allowlist: (enclosing function, exact ${...} text) -> justification.
# Keep this minimal. Each entry is something the static rules cannot prove but
# a human has verified. Entries must be used or the test fails (stale entry).
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, dict[tuple[str, str], str]] = {
    "dashboard.js": {
        ("renderBreakdownTable", "labelHeader"):
            "Function parameter; the three call sites pass the literals "
            '"Symbol", "Strategy", "Hour (ET)".',
        ("tradeCard", "label"):
            'Function parameter; call sites pass the literals "Best Trade" / "Worst Trade".',
        ("tradeCard", "cls"):
            'Function parameter used as a CSS class; call sites pass "best" / "worst".',
    },
    "ict-overlay.js": {
        ("renderICTPills", "kzLabel[data.killzone]"):
            "Lookup into the literal kzLabel map, guarded by "
            "`data.killzone && kzLabel[data.killzone]` so only map values render.",
    },
    "lessons.js": {
        ("renderLessonsByStock", "g.count"):
            "Local accumulator initialised to 0 and only ever `+= 1` -> always a number.",
    },
    "main.js": {
        ("_signalStackHtml", 'parts.join(" ")'):
            "Array literal of checked template literals plus pill(), which esc()s "
            "its label/title and formats the multiplier numerically.",
        ("_regimeChipHtml", 'pieces.join(" · ")'):
            "Every pieces.push() is esc(...) or _fmtMult() (numeric ×N.NN string).",
        ("_newsChipHtml", 'pieces.join(" · ")'):
            "Every pieces.push() is esc(...), a numeric count template, or _fmtMult().",
        ("_rsChipHtml", 'pieces.join(" · ")'):
            "Every pieces.push() is esc(...) or _fmtMult().",
        ("_evidenceChipRowHtml", 'chips.join("")'):
            "Array literal of the *ChipHtml() helpers, each of which is checked by "
            "this test (their templates are HTML-shaped).",
    },
}

# ---------------------------------------------------------------------------
# Lexer: template literals, blanked code, brace blocks
# ---------------------------------------------------------------------------
IDENT = r"[A-Za-z_$][\w$]*"
REGEX_PREV_CHARS = set("(,=:[!&|?{};+-*%<>~^")
REGEX_PREV_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete",
                    "void", "throw", "case", "do", "else"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else",
            "do", "try", "with", "new", "typeof", "void", "delete", "in", "instanceof",
            "await", "yield", "async", "class", "const", "let", "var"}
TAG_RE = re.compile(r"<[A-Za-z!/]")
ATTR_START_RE = re.compile(r"^\s*[A-Za-z-]+=[\"']")
SINK_RE = re.compile(
    r"(?:\.innerHTML|\.outerHTML)\s*=\s*$|\.insertAdjacentHTML\(\s*(?:\"[^\"]*\"|'[^']*')\s*,\s*$"
)
ON_HANDLER_RE = re.compile(r"\son[a-z]+\s*=\s*(?:\"[^\"]*|'[^']*)$", re.I)
JS_URL_RE = re.compile(r"\b(?:href|src|action|formaction)\s*=\s*[\"']\s*javascript:", re.I)


@dataclass
class Template:
    start: int
    end: int = -1
    parent: "Template | None" = None
    exprs: list = field(default_factory=list)      # [(abs_start, abs_end)]
    static: str = ""


@dataclass
class Block:
    open: int
    close: int
    fn_name: str | None = None
    bound: set = field(default_factory=set)
    pseudo: bool = False       # expression-bodied arrow: scope only, no braces


@dataclass
class Decl:
    name: str
    kind: str                 # const | let | var | function | import | opaque
    pos: int
    init: tuple | None        # (start, end) of initializer / arrow / function header
    block: "Block | None"     # innermost enclosing block (None = module scope)
    module: str | None = None  # for imports


class Lexed:
    """One lexed JS source file."""

    def __init__(self, path: Path, src: str):
        self.path = path
        self.name = path.name
        self.src = src
        self.n = len(src)
        self.code = list(src)
        self.templates: list[Template] = []
        self.blocks: list[Block] = []
        self._scan(0, None, None)
        self.code = "".join(self.code)
        for b in self.blocks:
            b.fn_name, b.bound = self._block_header(b.open)
        self.blocks.extend(self._arrow_scopes())
        self.blocks.sort(key=lambda b: b.open)
        self.decls = self._collect_decls()
        self._tpl_by_start = {t.start: t for t in self.templates}

    # -- scanning -----------------------------------------------------------
    def _blank(self, a, b):
        for k in range(a, b):
            if self.src[k] != "\n":
                self.code[k] = " "

    def _skip_string(self, i):
        q = self.src[i]
        i += 1
        while i < self.n:
            c = self.src[i]
            if c == "\\":
                i += 2
                continue
            if c == q:
                return i + 1
            if c == "\n":
                return i
            i += 1
        return self.n

    def _skip_regex(self, i):
        i += 1
        in_class = False
        while i < self.n:
            c = self.src[i]
            if c == "\\":
                i += 2
                continue
            if in_class:
                if c == "]":
                    in_class = False
            elif c == "[":
                in_class = True
            elif c == "/":
                i += 1
                while i < self.n and self.src[i].isalpha():
                    i += 1
                return i
            elif c == "\n":
                return i
            i += 1
        return self.n

    def _scan(self, i, end_char, parent):
        src, n = self.src, self.n
        last_sig, last_word = "", ""
        stack = []
        while i < n:
            c = src[i]
            if c == "/" and src.startswith("//", i):
                j = src.find("\n", i)
                j = n if j < 0 else j
                self._blank(i, j)
                i = j
                continue
            if c == "/" and src.startswith("/*", i):
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                self._blank(i, j)
                i = j
                continue
            if c in "\"'":
                j = self._skip_string(i)
                self._blank(i + 1, max(i + 1, j - 1))
                last_sig, last_word = c, ""
                i = j
                continue
            if c == "`":
                t = Template(start=i, parent=parent)
                self.templates.append(t)
                i = self._scan_template(i + 1, t)
                last_sig, last_word = "`", ""
                continue
            if c == "/" and (last_sig == "" or last_sig in REGEX_PREV_CHARS
                             or last_word in REGEX_PREV_WORDS):
                j = self._skip_regex(i)
                self.code[i] = "0"          # stand-in operand
                self._blank(i + 1, j)
                last_sig, last_word = "0", ""
                i = j
                continue
            if c == "{":
                stack.append(i)
            elif c == "}":
                if stack:
                    self.blocks.append(Block(stack.pop(), i))
                elif end_char == "}":
                    return i
            if c.isalnum() or c in "_$":
                m = re.match(r"[A-Za-z0-9_$]+", src[i:])
                last_word = m.group(0)
                last_sig = last_word[-1]
                i += len(last_word)
                continue
            if not c.isspace():
                last_sig, last_word = c, ""
            i += 1
        return n

    def _scan_template(self, i, t):
        src, n = self.src, self.n
        static = []
        while i < n:
            c = src[i]
            if c == "\\":
                static.append(src[i:i + 2])
                self._blank(i, i + 2)
                i += 2
                continue
            if c == "`":
                t.end = i + 1
                t.static = "".join(static)
                return i + 1
            if src.startswith("${", i):
                e_start = i + 2
                e_end = self._scan(e_start, "}", t)
                t.exprs.append((e_start, e_end))
                i = e_end + 1
                continue
            static.append(c)
            if c != "\n":
                self.code[i] = " "
            i += 1
        t.end = n
        t.static = "".join(static)
        return n

    # -- blocks & declarations -------------------------------------------------
    @staticmethod
    def _params(text):
        names = set()
        depth = 0
        part = []
        parts = []
        for ch in text + ",":
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(part))
                part = []
            else:
                part.append(ch)
        for p in parts:
            p = p.strip().lstrip(".")
            if not p:
                continue
            if p[0] in "{[":
                names.update(re.findall(IDENT, p))
            else:
                m = re.match(IDENT, p)
                if m:
                    names.add(m.group(0))
        return names

    def _block_header(self, open_off):
        h = self.code[max(0, open_off - 600):open_off].rstrip()
        m = re.search(r"\bfunction\s*(" + IDENT + r")?\s*\(([^()]*)\)\s*$", h)
        if m:
            return m.group(1), self._params(m.group(2))
        m = re.search(r"\(([^()]*)\)\s*=>\s*$", h)
        if m:
            nm = re.search(r"(" + IDENT + r")\s*=\s*(?:async\s+)?$", h[:m.start()])
            return (nm.group(1) if nm else None), self._params(m.group(1))
        m = re.search(r"(?<![\w$.])(" + IDENT + r")\s*=>\s*$", h)
        if m:
            nm = re.search(r"(" + IDENT + r")\s*=\s*(?:async\s+)?$", h[:m.start()])
            return (nm.group(1) if nm else None), {m.group(1)}
        m = re.search(r"\bcatch\s*\(\s*(" + IDENT + r")\s*\)\s*$", h)
        if m:
            return None, {m.group(1)}
        m = re.search(r"\bfor\s*\(\s*(?:const|let|var)\s+(.+?)\s+(?:of|in)\s[^{]*\)\s*$", h, re.S)
        if m:
            return None, set(re.findall(IDENT, m.group(1)))
        m = re.search(r"\bfor\s*\(\s*(?:let|var)\s+(" + IDENT + r")\s*=[^{]*\)\s*$", h, re.S)
        if m:
            return None, {m.group(1)}
        m = re.search(r"(?<![\w$.])(" + IDENT + r")\s*\(([^()]*)\)\s*$", h)
        if m and m.group(1) not in KEYWORDS:      # object method shorthand
            return m.group(1), self._params(m.group(2))
        return None, set()

    def _arrow_scopes(self):
        """Pseudo-blocks for expression-bodied arrows so `(t) => \\`${t}\\`` shadows
        an outer `t` exactly like a braced function body would."""
        out = []
        code = self.code
        for m in re.finditer(r"=>", code):
            j = m.end()
            while j < self.n and code[j].isspace():
                j += 1
            if j >= self.n or code[j] == "{":
                continue                                  # braced body: real block
            k = m.start() - 1
            while k >= 0 and code[k].isspace():
                k -= 1
            if k >= 0 and code[k] == ")":
                depth, o = 0, k
                while o >= 0:
                    if code[o] in ")]}":
                        depth += 1
                    elif code[o] in "([{":
                        depth -= 1
                        if depth == 0:
                            break
                    o -= 1
                params = self._params(code[o + 1:k])
            else:
                pm = re.search(IDENT + r"$", code[:k + 1])
                params = {pm.group(0)} if pm else set()
            depth, e = 0, j
            while e < self.n:
                c = code[e]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif c in ",;" and depth == 0:
                    break
                e += 1
            out.append(Block(m.start(), e, None, params, pseudo=True))
        return out

    def innermost_block(self, off):
        best = None
        for b in self.blocks:
            if b.open < off < b.close and (best is None or b.open > best.open):
                best = b
        return best

    def enclosing_blocks(self, off):
        bs = [b for b in self.blocks if b.open < off < b.close]
        bs.sort(key=lambda b: -b.open)
        return bs

    def fn_name_at(self, off):
        for b in self.enclosing_blocks(off):
            if b.fn_name:
                return b.fn_name
        return "<module>"

    def stmt_end(self, i, limit=None):
        """Offset of the depth-0 ';' or '}' that ends the expression starting at i."""
        limit = self.n if limit is None else limit
        depth = 0
        while i < limit:
            c = self.code[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    return i
                depth -= 1
            elif c == ";" and depth == 0:
                return i
            i += 1
        return limit

    def _match_brace(self, i):
        depth = 0
        while i < self.n:
            c = self.code[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return self.n

    def _collect_decls(self):
        decls = []
        code = self.code
        for m in re.finditer(r"\b(const|let|var)\s+(" + IDENT + r")\b\s*(=(?![=>])|)", code):
            kind, name = m.group(1), m.group(2)
            if m.group(3):
                a = m.end()
                init = (a, self.stmt_end(a))
            else:
                init = None
            decls.append(Decl(name, kind, m.start(), init, self.innermost_block(m.start())))
        for m in re.finditer(r"\b(?:const|let|var)\s*([\[{])", code):
            j = self._match_brace(m.start(1))
            for nm in set(re.findall(IDENT, code[m.start(1):j])):
                decls.append(Decl(nm, "opaque", m.start(), None, self.innermost_block(m.start())))
        for m in re.finditer(r"\bfunction\s+(" + IDENT + r")\s*\(", code):
            close = self._match_brace(m.end() - 1)
            body = code.find("{", close)
            decls.append(Decl(m.group(1), "function", m.start(), (m.end() - 1, body),
                              self.innermost_block(m.start())))
        for m in re.finditer(r"\bclass\s+(" + IDENT + r")", code):
            decls.append(Decl(m.group(1), "opaque", m.start(), None, self.innermost_block(m.start())))
        for m in re.finditer(r"\bimport\s*\{([^}]*)\}\s*from\s*(\"[^\"]*\"|'[^']*')", code):
            module = self.src[m.start(2) + 1:m.end(2) - 1]
            for part in m.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                nm = part.split()[-1]          # "a as b" -> b
                decls.append(Decl(nm, "import", m.start(), None, None, module))
        for m in re.finditer(r"\bimport\s+(" + IDENT + r")\s+from", code):
            decls.append(Decl(m.group(1), "opaque", m.start(), None, None))
        return decls

    # -- template helpers --------------------------------------------------------
    def template_at(self, start):
        return self._tpl_by_start.get(start)

    def is_html(self, t: Template):
        if TAG_RE.search(t.static) or ATTR_START_RE.search(t.static):
            return True
        if t.parent is not None and self.is_html(t.parent):
            return True
        before = self.code[max(0, t.start - 160):t.start]
        return bool(SINK_RE.search(before))

    def display(self, a, b):
        """Original text of [a,b) with nested top-level templates shown as __TPL__."""
        txt = self.src[a:b]
        nested = [t for t in self.templates
                  if a <= t.start < b and not (t.parent and a <= t.parent.start and t.parent.end <= b)]
        for t in sorted(nested, key=lambda t: -t.start):
            txt = txt[:t.start - a] + "__TPL__" + txt[t.end - a:]
        return re.sub(r"\s+", " ", txt).strip()

    def line(self, off):
        return self.src.count("\n", 0, off) + 1


# ---------------------------------------------------------------------------
# Expression tokenizer (over blanked code, template literals as single tokens)
# ---------------------------------------------------------------------------
PUNCT = sorted([">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=",
                "??=", "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--",
                "+=", "-=", "*=", "/=", "%=", "**", "<<", ">>"] + list("-+*/%<>=!~&|^?:;,.()[]{}"),
               key=len, reverse=True)
TOKEN_RE = re.compile(
    r"(?P<ws>\s+)|(?P<str>\"[^\"]*\"|'[^']*')|(?P<num>\d[\w.]*|\.\d+)|(?P<id>" + IDENT + r")|(?P<punct>"
    + "|".join(re.escape(p) for p in PUNCT) + r")|(?P<other>.)", re.S)


@dataclass
class Tok:
    type: str      # str num id punct tpl
    text: str
    pos: int
    tpl: Template | None = None

    def __eq__(self, other):
        if isinstance(other, tuple):
            return (self.type, self.text) == other
        return NotImplemented

    def __hash__(self):
        return hash((self.type, self.text))


def tokenize(lx: Lexed, a, b):
    toks = []
    i = a
    while i < b:
        if lx.code[i] == "`":
            t = lx.template_at(i)
            if t is None:
                raise ValueError("unmatched template literal")
            toks.append(Tok("tpl", "__TPL__", i, t))
            i = t.end
            continue
        m = TOKEN_RE.match(lx.code, i)
        if not m:
            raise ValueError("tokenizer stalled")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        if kind == "other":
            raise ValueError("unexpected character %r" % m.group(0))
        toks.append(Tok(kind, m.group(0), m.start()))
    return toks


def _match(toks, i):
    """Index of the bracket closing toks[i]."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].type == "punct":
            if toks[j].text in "([{":
                depth += 1
            elif toks[j].text in ")]}":
                depth -= 1
                if depth == 0:
                    return j
    return None


def _depth0(toks):
    """Yield (index, token) for tokens at bracket depth 0."""
    depth = 0
    for i, t in enumerate(toks):
        if t.type == "punct" and t.text in ")]}":
            depth -= 1
        if depth == 0:
            yield i, t
        if t.type == "punct" and t.text in "([{":
            depth += 1


def _split(toks, texts):
    parts, cur, ops = [], [], []
    depth = 0
    for t in toks:
        if t.type == "punct" and t.text in ")]}":
            depth -= 1
        if depth == 0 and t.type == "punct" and t.text in texts:
            parts.append(cur)
            cur = []
            ops.append(t)
        else:
            cur.append(t)
        if t.type == "punct" and t.text in "([{":
            depth += 1
    parts.append(cur)
    return parts, ops


def _strip_parens(toks):
    while toks and toks[0] == ("punct", "(") and _match(toks, 0) == len(toks) - 1:
        toks = toks[1:-1]
    return toks


def _is_operand_end(t: Tok):
    return t.type in ("str", "num", "id", "tpl") or (t.type == "punct" and t.text in ")]}")


NUMERIC_BINOPS = {"-", "*", "/", "%", "**", "|", "^", "&", "<<", ">>", ">>>",
                  "==", "!=", "===", "!==", "<", ">", "<=", ">="}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&&=", "||=", "??="}
LITERAL_IDS = {"true", "false", "null", "undefined", "NaN", "Infinity"}
NUMERIC_IDS = {"NaN", "Infinity"}
DATE_FORMATTERS = {"toLocaleDateString", "toLocaleTimeString", "toISOString", "toDateString",
                   "toTimeString", "toUTCString"}
NUMERIC_GETTERS = {"getTime", "getFullYear", "getMonth", "getDate", "getDay", "getHours",
                   "getMinutes", "getSeconds", "getMilliseconds", "valueOf"}
NUMERIC_GLOBALS = {"Number", "parseFloat", "parseInt", "Boolean", "isNaN", "isFinite"}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self, lexed: dict[str, Lexed], allowlist: dict | None = None):
        self.lexed = lexed                       # name -> Lexed
        self.allowlist = allowlist or {}
        self.used_allow = set()
        self.violations: list[tuple[str, int, str]] = []
        self._tpl_memo: dict[tuple[str, int], bool] = {}
        self._stack: set = set()

    # -- entry point ------------------------------------------------------------
    def check_file(self, lx: Lexed):
        for t in lx.templates:
            if lx.is_html(t):
                self.template_safe(lx, t)
                self._check_static(lx, t)

    def _check_static(self, lx, t):
        prev = t.start + 1
        for (es, ee) in t.exprs:
            static = lx.src[prev:es - 2]
            if ON_HANDLER_RE.search(static):
                self.violations.append((lx.name, lx.line(es),
                                        "${...} inside an inline on*= handler: " + lx.display(es, ee)))
            prev = ee + 1
        if JS_URL_RE.search(t.static):
            self.violations.append((lx.name, lx.line(t.start), "javascript: URL in template"))
        if re.search(r"<script\b", t.static, re.I):
            self.violations.append((lx.name, lx.line(t.start), "<script> built from a template literal"))

    def template_safe(self, lx, t: Template) -> bool:
        key = (lx.name, t.start)
        if key in self._tpl_memo:
            return self._tpl_memo[key]
        self._tpl_memo[key] = False               # cycle guard
        ok = True
        for (es, ee) in t.exprs:
            disp = lx.display(es, ee)
            akey = (lx.fn_name_at(es), disp)
            if akey in self.allowlist.get(lx.name, {}):
                self.used_allow.add((lx.name,) + akey)
                continue
            try:
                toks = tokenize(lx, es, ee)
                good = self.safe(lx, toks, es, frozenset(), "safe")
            except ValueError:
                good = False
            if not good:
                ok = False
                self.violations.append((lx.name, lx.line(es), "${" + disp + "}"))
        self._tpl_memo[key] = ok
        return ok

    # -- expression rules ----------------------------------------------------------
    def safe(self, lx, toks, off, shadow, want) -> bool:
        """want == "safe": value renders harmlessly; want == "numeric": value is a number."""
        toks = _strip_parens(list(toks))
        if not toks:
            return False
        d0 = list(_depth0(toks))
        if any(t.type == "punct" and (t.text == "=>" or t.text in ASSIGN_OPS) for _, t in d0):
            return False
        parts, _ = _split(toks, {","})
        if len(parts) > 1:
            return self.safe(lx, parts[-1], off, shadow, want)
        # ternary (lowest precedence)
        q = next((i for i, t in d0 if t == ("punct", "?")), None)
        if q is not None:
            depth, nest, colon = 0, 0, None
            for j in range(q + 1, len(toks)):
                t = toks[j]
                if t.type == "punct":
                    if t.text in "([{":
                        depth += 1
                    elif t.text in ")]}":
                        depth -= 1
                    elif depth == 0 and t.text == "?":
                        nest += 1
                    elif depth == 0 and t.text == ":":
                        if nest == 0:
                            colon = j
                            break
                        nest -= 1
            if colon is None:
                return False
            return (self.safe(lx, toks[q + 1:colon], off, shadow, want)
                    and self.safe(lx, toks[colon + 1:], off, shadow, want))
        parts, _ = _split(toks, {"??", "||"})
        if len(parts) > 1:
            return all(self.safe(lx, p, off, shadow, want) for p in parts)
        parts, _ = _split(toks, {"&&"})
        if len(parts) > 1:
            return self.safe(lx, parts[-1], off, shadow, want)
        # any remaining depth-0 binary operator other than '+' yields number/boolean
        for i, t in d0:
            if t.type == "punct" and t.text in NUMERIC_BINOPS:
                if t.text == "-" and not (i > 0 and _is_operand_end(toks[i - 1])):
                    continue
                return True
            if t.type == "id" and t.text in ("in", "instanceof") and i > 0:
                return True
        # binary '+'
        depth, cuts = 0, []
        for i, t in enumerate(toks):
            if t.type == "punct" and t.text in ")]}":
                depth -= 1
            if depth == 0 and t == ("punct", "+") and i > 0 and _is_operand_end(toks[i - 1]):
                cuts.append(i)
            if t.type == "punct" and t.text in "([{":
                depth += 1
        if cuts:
            parts, last = [], 0
            for c in cuts:
                parts.append(toks[last:c])
                last = c + 1
            parts.append(toks[last:])
            return all(self.safe(lx, p, off, shadow, want) for p in parts)
        # unary prefix
        t0 = toks[0]
        if t0.type == "punct" and t0.text in ("!", "-", "+", "~"):
            return True
        if t0.type == "id" and t0.text in ("typeof", "void"):
            return want == "safe"
        return self.chain(lx, toks, off, shadow, want)

    def chain(self, lx, toks, off, shadow, want) -> bool:
        i = 0
        primary = None
        if toks[0] == ("id", "new"):
            i = 1
            while i < len(toks) and (toks[i].type == "id" or toks[i] == ("punct", ".")):
                i += 1
            if i < len(toks) and toks[i] == ("punct", "("):
                i = _match(toks, i) + 1
            primary = ("new", None)
        elif toks[0] == ("punct", "("):
            j = _match(toks, 0)
            if j is None:
                return False
            primary = ("group", toks[1:j])
            i = j + 1
        elif toks[0].type == "punct" and toks[0].text in "[{":
            j = _match(toks, 0)
            if j is None:
                return False
            primary = ("literal_obj", None)
            i = j + 1
        elif toks[0].type in ("str", "num", "tpl", "id"):
            primary = (toks[0].type, toks[0])
            i = 1
        else:
            return False
        ops = []
        while i < len(toks):
            t = toks[i]
            if t.type == "punct" and t.text in (".", "?.") and i + 1 < len(toks) and toks[i + 1].type == "id":
                ops.append(("member", toks[i + 1].text))
                i += 2
            elif t.type == "punct" and t.text == "?." and i + 1 < len(toks) and toks[i + 1] == ("punct", "("):
                i += 1
            elif t == ("punct", "("):
                j = _match(toks, i)
                if j is None:
                    return False
                ops.append(("call", toks[i + 1:j]))
                i = j + 1
            elif t == ("punct", "["):
                j = _match(toks, i)
                if j is None:
                    return False
                ops.append(("index", toks[i + 1:j]))
                i = j + 1
            elif t.type == "punct" and t.text in ("++", "--"):
                return True
            else:
                return False
        kind, tok = primary
        if not ops:
            if kind == "num":
                return True
            if kind == "str":
                return want == "safe"
            if kind == "tpl":
                return want == "safe" and self.template_safe(lx, tok.tpl)
            if kind == "group":
                return self.safe(lx, tok, off, shadow, want)
            if kind == "id":
                if tok.text in LITERAL_IDS:
                    return want == "safe" or tok.text in NUMERIC_IDS
                return self.resolve_value(lx, tok.text, off, shadow, want)
            return False
        last = ops[-1]
        if last[0] == "member" and last[1] == "length":
            return True
        if last[0] == "call":
            if kind == "id" and tok.text == "Math" and len(ops) == 2 and ops[0][0] == "member":
                return True                                   # Math.round(...) etc.
            if kind == "id" and tok.text == "Date" and len(ops) == 2 and ops[0] == ("member", "now"):
                return True                                   # Date.now()
            if len(ops) >= 2 and ops[-2][0] == "member":
                m = ops[-2][1]
                if m in NUMERIC_GETTERS:
                    return True
                if m == "toFixed" or m in DATE_FORMATTERS:
                    return want == "safe"
                if m == "toLocaleString":
                    return self.receiver_numeric(lx, kind, tok, ops[:-2], off, shadow)
                if m == "join" and want == "safe":
                    return self.map_join_safe(lx, ops, last[1], off, shadow)
                return False
            if len(ops) == 1 and kind == "id":
                name = tok.text
                if name in ESC_FUNCS:
                    return want == "safe"
                if name in NUMERIC_GLOBALS:
                    return True
                return self.resolve_call(lx, name, off, shadow)
            return False
        return False

    def receiver_numeric(self, lx, kind, tok, ops, off, shadow):
        """Is `primary ops...` (the receiver of .toLocaleString) a number?"""
        if not ops:
            if kind == "num":
                return True
            if kind == "group":
                return self.safe(lx, tok, off, shadow, "numeric")
            if kind == "id":
                return self.resolve_value(lx, tok.text, off, shadow, "numeric")
            return False
        last = ops[-1]
        if last[0] == "member" and last[1] == "length":
            return True
        if last[0] == "call" and len(ops) >= 2 and ops[-2][0] == "member" and ops[-2][1] in NUMERIC_GETTERS:
            return True
        if last[0] == "call" and len(ops) == 1 and kind == "id" and tok.text in NUMERIC_GLOBALS:
            return True
        if kind == "id" and tok.text == "Math" and len(ops) == 2 and last[0] == "call":
            return True
        return False

    def map_join_safe(self, lx, ops, join_args, off, shadow):
        """`xs.map(cb).join("lit")` where cb returns a checked template."""
        if not (len(join_args) == 1 and join_args[0].type == "str"):
            return False
        if len(ops) < 4 or ops[-3][0] != "call" or ops[-4] != ("member", "map"):
            return False
        cb = ops[-3][1]
        if len(cb) == 1 and cb[0].type == "id":
            return self.resolve_call(lx, cb[0].text, off, shadow)
        return self.arrow_safe(lx, cb, off, shadow)

    # -- functions -----------------------------------------------------------------
    def arrow_safe(self, lx, toks, off, shadow) -> bool:
        toks = list(toks)
        if toks and toks[0] == ("id", "async"):
            toks = toks[1:]
        if not toks:
            return False
        if toks[0] == ("punct", "("):
            j = _match(toks, 0)
            if j is None or j + 1 >= len(toks) or toks[j + 1] != ("punct", "=>"):
                return False
            params = Lexed._params(" ".join(t.text for t in toks[1:j]))
            body = toks[j + 2:]
        elif toks[0].type == "id" and len(toks) > 2 and toks[1] == ("punct", "=>"):
            params = {toks[0].text}
            body = toks[2:]
        else:
            return False
        if not body:
            return False
        shadow = shadow | frozenset(params)
        if body[0] == ("punct", "{"):
            j = _match(body, 0)
            if j is None:
                return False
            return self.returns_safe(lx, body[0].pos, body[j].pos, shadow)
        return self.safe(lx, body, body[0].pos, shadow, "safe")

    def returns_safe(self, lx, a, b, shadow) -> bool:
        """Every `return EXPR;` inside code[a:b] (a function body) renders safely."""
        code = lx.code
        found = False
        for m in re.finditer(r"\breturn\b", code[a:b]):
            found = True
            q = a + m.end()
            while q < b and code[q] in " \t":
                q += 1
            if q >= b or code[q] in ";}\n":
                continue
            e = lx.stmt_end(q, b)
            try:
                toks = tokenize(lx, q, e)
            except ValueError:
                return False
            if not toks or not self.safe(lx, toks, q, shadow, "safe"):
                return False
        return found

    # -- identifier resolution ----------------------------------------------------
    def _lookup(self, lx, name, off, shadow):
        """Lexically visible declarations of `name` at `off`, or None if opaque."""
        if name in shadow:
            return None
        for blk in lx.enclosing_blocks(off) + [None]:
            if blk is not None and name in blk.bound:
                return None
            ds = [d for d in lx.decls if d.name == name and d.block is blk]
            if ds:
                return ds
        return None

    def _assignments(self, lx, d: Decl):
        """(rhs_start, rhs_end) of every reassignment of a let/var in its scope."""
        a, b = (d.block.open, d.block.close) if d.block else (0, lx.n)
        out = []
        pat = re.compile(r"(?<![\w$.])" + re.escape(d.name) + r"\s*(=|\+=|\|\|=|\?\?=)(?![=>])")
        for m in pat.finditer(lx.code, a, b):
            if d.init and d.init[0] - 3 <= m.end() <= d.init[0] + 1:
                continue                    # the declaration itself
            if d.pos <= m.start() < d.pos + len(d.name) + 8:
                continue
            out.append((m.end(), lx.stmt_end(m.end())))
        return out

    def resolve_value(self, lx, name, off, shadow, want) -> bool:
        ds = self._lookup(lx, name, off, shadow)
        if not ds:
            return False
        for d in ds:
            key = ("v", lx.name, d.pos, want)
            if key in self._stack:
                return False
            self._stack.add(key)
            try:
                if d.kind in ("const", "let", "var"):
                    if d.init is None:
                        if d.kind == "const":
                            return False
                    else:
                        toks = tokenize(lx, *d.init)
                        if not self.safe(lx, toks, d.init[0], frozenset(), want):
                            return False
                    if d.kind != "const":
                        for (ra, rb) in self._assignments(lx, d):
                            toks = tokenize(lx, ra, rb)
                            if not self.safe(lx, toks, ra, frozenset(), want):
                                return False
                else:
                    return False
            except ValueError:
                return False
            finally:
                self._stack.discard(key)
        return True

    def resolve_call(self, lx, name, off, shadow) -> bool:
        ds = self._lookup(lx, name, off, shadow)
        if not ds:
            return False
        for d in ds:
            key = ("c", lx.name, d.pos)
            if key in self._stack:
                return False
            self._stack.add(key)
            try:
                if d.kind == "function":
                    hdr_open, body_open = d.init
                    params = Lexed._params(lx.code[hdr_open + 1:lx._match_brace(hdr_open)])
                    if not self.returns_safe(lx, body_open, lx._match_brace(body_open), frozenset(params)):
                        return False
                elif d.kind == "const" and d.init:
                    if not self.arrow_safe(lx, tokenize(lx, *d.init), d.init[0], frozenset()):
                        return False
                elif d.kind == "import" and d.module == UTILS_MODULE and "utils.js" in self.lexed:
                    ulx = self.lexed["utils.js"]
                    if name in ESC_FUNCS:
                        continue
                    if not self.resolve_call(ulx, name, ulx.n - 1, frozenset()):
                        return False
                else:
                    return False
            except ValueError:
                return False
            finally:
                self._stack.discard(key)
        return True


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------
def run_checker(paths, allowlist=None):
    lexed = {p.name: Lexed(p, p.read_text(encoding="utf-8")) for p in paths}
    ck = Checker(lexed, allowlist)
    for lx in lexed.values():
        ck.check_file(lx)
    return ck


def frontend_js_files():
    files = sorted(FRONTEND.glob("*.js"))
    assert files, "no Frontend/*.js files found at %s" % FRONTEND
    return files


def _fmt(violations):
    return "\n".join("  %s:%d  %s" % v for v in sorted(violations))


# ---------------------------------------------------------------------------
# Tests against the real tree
# ---------------------------------------------------------------------------
def test_every_interpolation_in_html_templates_is_escaped_or_allowlisted():
    ck = run_checker(frontend_js_files(), ALLOWLIST)
    assert not ck.violations, (
        "Unescaped ${...} inside HTML template literals (wrap in esc(...) from "
        "./utils.js, or justify an ALLOWLIST entry):\n" + _fmt(ck.violations))


def test_allowlist_entries_are_all_used():
    ck = run_checker(frontend_js_files(), ALLOWLIST)
    declared = {(f,) + k for f, entries in ALLOWLIST.items() for k in entries}
    stale = declared - ck.used_allow
    assert not stale, "Stale ALLOWLIST entries (no longer present in source): %r" % sorted(stale)


def test_esc_is_imported_wherever_it_is_used():
    pat_use = re.compile(r"(?<![\w$.])esc\(")
    pat_import = re.compile(r"import\s*\{[^}]*\besc\b[^}]*\}\s*from\s*[\"']\./utils\.js[\"']")
    bad = []
    for p in frontend_js_files():
        src = p.read_text(encoding="utf-8")
        if p.name == "utils.js":
            continue
        lx = Lexed(p, src)
        if pat_use.search(lx.code) and not pat_import.search(src):
            bad.append(p.name)
    assert not bad, "esc() used without `import { esc } from \"./utils.js\"`: %r" % bad


def test_no_private_escape_helpers_remain():
    """One escaper only: utils.esc. Private &<>-only copies drifted before."""
    offenders = []
    for p in frontend_js_files():
        if p.name == "utils.js":
            continue
        src = p.read_text(encoding="utf-8")
        if re.search(r"\.replace\(/&/g,\s*[\"']&amp;[\"']\)", src):
            offenders.append(p.name)
    assert not offenders, "ad-hoc HTML escaping found (use esc from ./utils.js): %r" % offenders


# ---------------------------------------------------------------------------
# Self-tests: the guard must catch regressions (and not cry wolf)
# ---------------------------------------------------------------------------
def _check_snippet(js: str):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fixture.js"
        p.write_text(js, encoding="utf-8")
        return run_checker([p]).violations


HEADER = 'import { esc } from "./utils.js";\n'


def test_guard_flags_raw_interpolation():
    assert _check_snippet(HEADER + "el.innerHTML = `<b>${raw.name}</b>`;\n")


def test_guard_flags_raw_interpolation_multiline_and_in_attribute():
    js = HEADER + 'el.innerHTML = `\n  <div title="${raw}">\n    ok\n  </div>`;\n'
    assert _check_snippet(js)


def test_guard_accepts_escaped_interpolation():
    assert _check_snippet(HEADER + "el.innerHTML = `<b>${esc(raw.name)}</b>`;\n") == []


def test_guard_accepts_numeric_and_literal_interpolations():
    js = HEADER + (
        "function render(p) {\n"
        "  const n = Math.round(p.x);\n"
        "  const cls = p.up ? \"positive\" : \"negative\";\n"
        "  let hold = \"—\";\n"
        "  if (p.s != null) hold = `${Math.floor(p.s / 60)}m`;\n"
        "  el.innerHTML = `<span class=\"${cls}\">${n} ${p.price.toFixed(2)} ${hold} ${p.items.length}</span>`;\n"
        "}\n")
    assert _check_snippet(js) == []


def test_guard_flags_unescaped_local_and_let_reassignment():
    js = HEADER + "const x = raw;\nel.innerHTML = `<b>${x}</b>`;\n"
    assert _check_snippet(js)
    js = HEADER + "let s = \"a\";\ns = raw;\nel.innerHTML = `<i>${s}</i>`;\n"
    assert _check_snippet(js)


def test_guard_flags_shadowed_parameter():
    js = HEADER + "const t = 1;\nel.innerHTML = items.map((t) => `<b>${t}</b>`).join(\"\");\n"
    assert _check_snippet(js)


def test_guard_handles_map_join_rows():
    ok = HEADER + "el.innerHTML = `<ul>${items.map((i) => `<li>${esc(i.name)}</li>`).join(\"\")}</ul>`;\n"
    assert _check_snippet(ok) == []
    bad = HEADER + "el.innerHTML = `<ul>${items.map((i) => `<li>${i.name}</li>`).join(\"\")}</ul>`;\n"
    assert _check_snippet(bad)


def test_guard_flags_inline_handler_and_js_url():
    js = HEADER + "el.innerHTML = `<b onclick=\"go('${esc(x)}')\">x</b>`;\n"
    assert _check_snippet(js)
    js = HEADER + "el.innerHTML = `<a href=\"javascript:void(0)\">x</a>`;\n"
    assert _check_snippet(js)
    js = HEADER + "el.innerHTML = `<script>${esc(x)}</script>`;\n"
    assert _check_snippet(js)


def test_guard_ignores_comments_strings_and_regex_literals():
    js = HEADER + (
        "// a stray ` backtick in a comment and \"quote\n"
        "const q = \"`${raw}`\";\n"
        "const r = s.replace(/\"/g, \"&quot;\");\n"
        "el.innerHTML = `<b>${esc(r)}</b>`;\n")
    assert _check_snippet(js) == []


def test_guard_checks_function_returns():
    ok = HEADER + (
        "function chip(c) { if (!c) return \"\"; return `<span>${esc(c.label)}</span>`; }\n"
        "el.innerHTML = `<div>${chip(x)}</div>`;\n")
    assert _check_snippet(ok) == []
    bad = HEADER + (
        "function chip(c) { if (!c) return c.label; return `<span>${esc(c.label)}</span>`; }\n"
        "el.innerHTML = `<div>${chip(x)}</div>`;\n")
    assert _check_snippet(bad)


# ---------------------------------------------------------------------------
# Standalone runner (no pytest needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                failures += 1
                print("FAIL", name)
                print("     ", str(e).replace("\n", "\n      "))
    print("\n%d failure(s)" % failures)
    sys.exit(1 if failures else 0)
