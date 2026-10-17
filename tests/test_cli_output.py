import io

from commander.cli_output import write_json


def test_write_json_indents_keeps_unicode_and_ends_with_a_newline() -> None:
    out = io.StringIO()

    write_json({"deck": "Ellivere \U0001f333", "ids": [1, 2]}, out)

    assert out.getvalue() == '{\n  "deck": "Ellivere \U0001f333",\n  "ids": [\n    1,\n    2\n  ]\n}\n'
