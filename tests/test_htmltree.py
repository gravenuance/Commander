from commander.htmltree import parse_html, tagged, with_class


class TestParseHtml:
    def test_nesting_text_and_unescaped_attributes(self) -> None:
        root = parse_html('<div id="a" class="x y">Hi <b title="1&nbsp;|&nbsp;2">there</b>!</div>')

        div = root.find(tagged("div", id="a"))
        assert div is not None
        assert (div.has_class("x"), div.has_class("y"), div.has_class("z")) == (True, True, False)
        assert (div.own_text(), div.text()) == ("Hi !", "Hi there!")
        bold = div.find(tagged("b"))
        assert bold is not None
        assert bold.get("title") == "1\xa0|\xa02"
        assert bold.parent is div

    def test_void_tags_do_not_swallow_what_follows(self) -> None:
        root = parse_html('<p><input name="a"><br>after</p><p>second</p>')

        paragraphs = root.find_all(tagged("p"))
        assert [p.text() for p in paragraphs] == ["after", "second"]
        assert [n.tag for n in paragraphs[0].elements()] == ["input", "br"]

    def test_a_stray_end_tag_is_ignored_and_unclosed_tags_end_with_the_parent(self) -> None:
        root = parse_html("<div><span>one</i></div><p>two</p>")

        assert [n.text() for n in root.find_all(tagged("span"))] == ["one"]
        assert root.find(tagged("p")) is not None
        paragraph = root.find(tagged("p"))
        assert paragraph is not None
        assert paragraph.parent is root

    def test_script_and_style_content_is_not_text(self) -> None:
        root = parse_html("<main>a<script>var x = '<b>';</script><style>p {}</style>b</main>")

        main = root.find(tagged("main"))
        assert main is not None
        assert main.text() == "ab"

    def test_find_by_class_in_document_order(self) -> None:
        root = parse_html('<i class="k">1</i><u><i class="k">2</i></u><i class="m">3</i>')

        assert [n.text() for n in root.find_all(with_class("k"))] == ["1", "2"]
        inner = root.find_all(with_class("k"))[1]
        assert inner.parent is not None
        assert [n.tag for n in inner.ancestors()] == ["u", "#document"]
