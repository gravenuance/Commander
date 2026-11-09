"""A small element tree over the stdlib's `html.parser`, for readers of pages that are not ours (ADR 0067)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from html.parser import HTMLParser

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track"}
    | {"wbr"}
)
# Their content is code, not text a reader should see.
_RAW_TEXT_TAGS = frozenset({"script", "style"})

type Predicate = Callable[[Node], bool]


class Node:
    """An element: its tag, attributes (values already unescaped), parent and children (nodes and text)."""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def elements(self) -> Iterator[Node]:
        """Every descendant element, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.elements()

    def find_all(self, predicate: Predicate) -> list[Node]:
        return [node for node in self.elements() if predicate(node)]

    def find(self, predicate: Predicate) -> Node | None:
        return next((node for node in self.elements() if predicate(node)), None)

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def own_text(self) -> str:
        """The text directly inside this element, leaving out its child elements' text."""
        return "".join(child for child in self.children if isinstance(child, str))

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)


def tagged(tag: str, *, cls: str | None = None, **attrs: str) -> Predicate:
    """A predicate for elements with this tag, class token and exact attribute values."""

    def matches(node: Node) -> bool:
        return (
            node.tag == tag
            and (cls is None or node.has_class(cls))
            and all(node.attrs.get(name) == value for name, value in attrs.items())
        )

    return matches


def with_class(cls: str) -> Predicate:
    return lambda node: node.has_class(cls)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self._open = self.root
        self._in_raw_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs}, self._open)
        self._open.children.append(node)
        if tag in _VOID_TAGS:
            return
        self._open = node
        self._in_raw_text = tag in _RAW_TEXT_TAGS

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open.children.append(Node(tag, {name: value or "" for name, value in attrs}, self._open))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self._open
        while node is not None and node.tag != tag:
            node = node.parent
        if node is None or node.parent is None:
            return
        self._open = node.parent
        self._in_raw_text = False

    def handle_data(self, data: str) -> None:
        if not self._in_raw_text:
            self._open.children.append(data)


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root
