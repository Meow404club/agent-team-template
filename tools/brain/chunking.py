"""cAST chunking aligned with the official reference implementation
(github.com/yilinjz/astchunk, arXiv:2506.15655).

Algorithm (matching astchunk_builder.py):
  step 1  greedily assign AST nodes to windows by non-whitespace char count,
          recursing into children of oversized nodes (ancestors tracked);
  step 1b merge adjacent sibling windows greedily;
  step 2  optional AST-node overlap between windows (we default 0);
  step 3  windows -> chunks with metadata (start/end line, nws size,
          ancestor path = class/function lineage);
  step 4  chunk expansion: prepend a contextual header block (file path +
          ancestor path + node signature) -- this is our contextual-retrieval
          prefix, satisfying the "Contextual Awareness" limitation the cAST
          authors acknowledge.

Differences from the reference (documented in docs/RESEARCH-NOTES.md):
  - Python implementation, single language binding here (Java); markdown has a
    heading-based equivalent.
  - Leading comments are attached to the following declaration (multi-view).
  - A final hard cap guarantees max chars per chunk even after expansion
    (embedding-model safety); the reference caps pre-expansion only.
Fallback chain: tree-sitter -> heading/brace heuristics -> line windows.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass

MAX_CHUNK_NWS = 1500          # official example value for code
HARD_MAX_CHARS = 2000         # post-expansion hard cap (embedder safety)

try:
    import tree_sitter
    import tree_sitter_java as _ts_java
    _JAVA_LANG = tree_sitter.Language(_ts_java.language())
    _JAVA_PARSER = tree_sitter.Parser(_JAVA_LANG)
except Exception:  # pragma: no cover
    _JAVA_PARSER = None

_WS = set(string.whitespace)


def _nws_cumsum(b: bytes):
    flags = [1 if x not in _WS else 0 for x in b]
    cs = [0]
    acc = 0
    for f in flags:
        acc += f
        cs.append(acc)
    return cs


def _nws(cs, start: int, stop: int) -> int:
    return cs[stop] - cs[start]


@dataclass
class _Node:
    ts_node: object
    size: int
    ancestors: list          # list of (kind, name) tuples
    src: bytes
    comment: str = ""        # leading comments glued to this node (multi-view)


# ------------------------------------------------------------- Java path ----

_DECL = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
_NAMED = {"method_declaration", "constructor_declaration", "field_declaration",
          "class_declaration", "interface_declaration", "enum_declaration",
          "record_declaration", "annotation_type_declaration", "static_initializer"}
_COMMENTS = {"line_comment", "block_comment"}


def _ident(ts_node, src: bytes) -> str:
    for ch in ts_node.children:
        if ch.type == "identifier" or ch.type == "type_identifier":
            return ch.text.decode("utf-8", "replace")
    return "(anon)"


def _sig_head(ts_node, src: bytes) -> str:
    body = ts_node.text.decode("utf-8", "replace")
    brace = body.find("{")
    head = body[:brace] if brace > 0 else body
    return " ".join(head.split())[:160]


def _attach_comments(nodes: list, src: bytes):
    """Multi-view: group leading comments with the next named node."""
    grouped: list[tuple[object, str]] = []  # (node, prepended_comment_text)
    pending: list[str] = []
    for n in nodes:
        if n.type in _COMMENTS:
            pending.append(n.text.decode("utf-8", "replace"))
        else:
            grouped.append((n, "\n".join(pending)))
            pending = []
    if pending:
        grouped.append((None, "\n".join(pending)))
    return grouped


def _assign(nodes: list, cs, max_nws: int, ancestors: list, src: bytes,
            comments_of) -> list[list[_Node]]:
    """Official step 1: greedy window packing with recursion into oversized nodes."""
    windows: list[list[_Node]] = []
    cur: list[_Node] = []
    cur_size = 0

    for node, comment_text in comments_of(nodes):
        # attach leading comment to the node text (multi-view); size counts both
        body = node.text.decode("utf-8", "replace")
        full = f"{comment_text}\n{body}" if comment_text.strip() else body
        size = _nws(cs, node.start_byte, node.end_byte) \
            + sum(1 for c in comment_text if c not in _WS)
        exceeds = size > max_nws
        if (not cur and exceeds) or (cur_size + size > max_nws):
            if cur:
                windows.append(cur)
                cur, cur_size = [], 0
            if exceeds:
                child_windows = _assign(node.children, cs, max_nws,
                                        ancestors + [(node.type, _ident(node, src))],
                                        src, comments_of)
                if child_windows:
                    windows.extend(_merge_windows(child_windows, max_nws))
            else:
                cur.append(_Node(node, size, ancestors, src, comment_text))
        else:
            cur.append(_Node(node, size, ancestors, src, comment_text))
    if cur:
        windows.append(cur)
    return windows


def _merge_windows(windows: list[list[_Node]], max_nws: int) -> list[list[_Node]]:
    """Official step 1b: greedy adjacent-window merge (siblings only by construction)."""
    if not windows:
        return windows
    merged = [windows[0][:]]
    for w in windows[1:]:
        total = sum(n.size for n in merged[-1]) + sum(n.size for n in w)
        if total <= max_nws:
            merged[-1].extend(w)
        else:
            merged.append(w[:])
    return merged


def _header_line(file_path: str, ancestors: list, sig: str = "") -> str:
    anc = ".".join(name for _, name in ancestors if name != "(anon)")
    parts = [file_path]
    if anc:
        parts.append(anc)
    if sig:
        parts.append(sig)
    return " > ".join(parts)


def chunk_java_ast(text: str, file_path: str = "",
                   max_nws: int = MAX_CHUNK_NWS,
                   chunk_expansion: bool = True) -> list[dict]:
    """Returns list of {line, header, text, nws, ancestors} dicts (official step 3/4)."""
    if _JAVA_PARSER is None:
        raise RuntimeError("tree-sitter-java unavailable")
    src = text.encode("utf-8", errors="replace")
    cs = _nws_cumsum(src)
    tree = _JAVA_PARSER.parse(src)

    def comments_of(nodes):
        return _attach_comments(list(nodes), src)

    root = tree.root_node
    if _nws(cs, root.start_byte, root.end_byte) <= max_nws:
        windows = [[_Node(root, _nws(cs, 0, len(src)), [], src)]]
    else:
        windows = _assign(root.children, cs, max_nws, [], src, comments_of)
    # official step 2: AST-node overlap — default off (0)

    out: list[dict] = []
    for w in windows:
        if not w:
            continue
        first = w[0]
        start_line = first.ts_node.start_point[0] + 1
        end_line = w[-1].ts_node.end_point[0] + 1
        parts = []
        for n in w:
            t = n.ts_node.text.decode("utf-8", "replace")
            if getattr(n, "comment", ""):
                t = n.comment + "\n" + t
            parts.append(t)
        body = "\n".join(parts)
        # ancestors from the first node + a signature view of the first node
        ancestors = list(first.ancestors)
        sig = ""
        if first.ts_node.type in _NAMED:
            sig = _sig_head(first.ts_node, src)
        header = _header_line(file_path, ancestors, sig)
        text_out = body
        if chunk_expansion:
            anc_lines = "\n".join(f"{'  '*i}{name}" for i, (_, name) in enumerate(ancestors))
            exp = "\n".join(x for x in (file_path, anc_lines, sig) if x)
            text_out = f"// {exp}\n{body}" if exp else body
        # hard cap: slice the rare window that still exceeds after expansion
        if len(text_out) > HARD_MAX_CHARS:
            for ln, _h, piece in _hard_split(text_out, start_line):
                out.append({"line": ln, "header": header, "text": piece,
                            "nws": _nws(cs, first.ts_node.start_byte, w[-1].ts_node.end_byte)})
        else:
            out.append({"line": start_line, "header": header, "text": text_out,
                        "nws": _nws(cs, first.ts_node.start_byte, w[-1].ts_node.end_byte)})
    return out


def _hard_split(body: str, base_line: int) -> list[tuple[int, str, str]]:
    if len(body) <= HARD_MAX_CHARS:
        return [(base_line, "", body)]
    lines = body.splitlines(keepends=True)
    out: list[tuple[int, str, str]] = []
    buf: list[str] = []
    size = 0
    start_ln = base_line
    for k, line in enumerate(lines):
        if size + len(line) > HARD_MAX_CHARS and buf:
            out.append((start_ln, "", "".join(buf)))
            buf, size, start_ln = [], 0, base_line + k
        buf.append(line)
        size += len(line)
    if buf:
        out.append((start_ln, "", "".join(buf)))
    final: list[tuple[int, str, str]] = []
    for ln, h, piece in out:
        if len(piece) <= HARD_MAX_CHARS:
            final.append((ln, h, piece))
            continue
        for s in range(0, len(piece), HARD_MAX_CHARS):
            final.append((ln + (s // 80), h, piece[s:s + HARD_MAX_CHARS]))
    return final


# ------------------------------------------------------------- markdown -----

_MD_H = re.compile(r"^(#{1,6})\s+(.*)$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")


def chunk_markdown(text: str, file_path: str = "",
                   max_chars: int = 6000) -> list[dict]:
    """Heading-lineage chunking for docs (setext + ATX).

    Docs are rarely split (按节不按块): a full section is ONE chunk up to
    `max_chars` (~1.5-2k tokens), keeping complete context as the user
    requested. Only oversized sections (huge tutorials) get para-wrapped;
    every chunk keeps the full heading lineage `A > B > C` in its header for
    hierarchical context.
    """
    lines = text.splitlines(keepends=True)
    sections: list[tuple[int, str, str]] = []
    stack: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_line = 1

    def close():
        nonlocal cur
        if cur:
            sections.append((cur_line, " > ".join(t for _, t in stack) or "(top)", "".join(cur)))
            cur = []

    for i, line in enumerate(lines):
        m = _MD_H.match(line)
        is_setext = False
        level = 0
        if not m and i + 1 < len(lines) and line.strip():
            sm = _SETEXT.match(lines[i + 1])
            if sm and not line.startswith(("    ", "\t")):
                is_setext = True
                level = 1 if sm.group(1)[0] == "=" else 2
        if m or is_setext:
            close()
            level = len(m.group(1)) if m else level
            title = (m.group(2).strip() if m else line.strip()) or "(untitled)"
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_line = i + 1
            cur.append(line)
        else:
            if cur or stack:
                cur.append(line)
    close()

    chunks: list[dict] = []
    for line_no, path, body in sections:
        body = body.strip()
        if not body:
            continue
        header = _header_line(file_path, [("doc", path)])
        pieces: list[str] = []
        if len(body) <= max_chars:
            pieces = [body]
        else:
            part: list[str] = []
            size = 0
            for para in body.split("\n\n"):
                if size + len(para) > max_chars and part:
                    pieces.append("\n\n".join(part))
                    part, size = [], 0
                part.append(para)
                size += len(para) + 2
            if part:
                pieces.append("\n\n".join(part))
        for j, piece in enumerate(pieces):
            if len(piece) > HARD_MAX_CHARS:
                for ln2, h2, p2 in _hard_split(piece, line_no):
                    chunks.append({"line": ln2, "header": header, "text": p2, "nws": -1})
            else:
                chunks.append({"line": line_no, "header": header, "text": piece, "nws": -1})
    return chunks or [{"line": 1, "header": file_path, "text": text[:HARD_MAX_CHARS], "nws": -1}]


def chunk_file(lang: str, text: str, file_path: str = "") -> list[dict]:
    """Entry point. Returns [{line, header, text}]; never raises, never oversizes."""
    try:
        if lang == "java":
            return chunk_java_ast(text, file_path)
    except Exception:
        pass
    try:
        if lang == "md":
            return chunk_markdown(text, file_path)
    except Exception:
        pass
    return [{"line": ln, "header": file_path or "(file)", "text": t, "nws": -1}
            for ln, _, t in _hard_split(text, 1)]
