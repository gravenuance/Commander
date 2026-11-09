"""Scripts that fill in and click a Cardmarket page's forms, for `FormBrowser.act` and `submit` (ADR 0067).

Each script is a fixed template; every value goes in through `json.dumps`, so no text of a card or a list ever
becomes code. A script answers a string and never throws: `ok` or `SUBMITTED` when it did its work, otherwise
a sentence saying what it did not find. Selectors are Cardmarket's own and undocumented.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from commander.cmsession import SUBMITTED

if TYPE_CHECKING:
    from collections.abc import Sequence

OK = "ok"


def set_field_script(selector: str, value: str) -> str:
    """Set the first field matching `selector` to `value` and fire the events a person's entry would, so the
    page's own scripts see it. Answers `OK` once the field reads back as `value`."""
    return f"""(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return 'no element matches ' + {json.dumps(selector)};
  el.value = {json.dumps(value)};
  el.dispatchEvent(new Event('input', {{bubbles: true}}));
  el.dispatchEvent(new Event('change', {{bubbles: true}}));
  if (el.value !== {json.dumps(value)}) {{
    return 'the field ' + {json.dumps(selector)} + ' kept ' + JSON.stringify(el.value);
  }}
  return {json.dumps(OK)};
}})()"""


def submit_script(selector: str) -> str:
    """Click the first enabled element matching `selector`; answers `SUBMITTED` once it has."""
    return f"""(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return 'no element matches ' + {json.dumps(selector)};
  if (el.disabled) return {json.dumps(selector)} + ' is disabled';
  el.click();
  return {json.dumps(SUBMITTED)};
}})()"""


def tick_wants_script(want_ids: Sequence[str]) -> str:
    """Tick exactly the wants-list rows with these ids, and untick every other row. Answers JSON with
    `found` (how many of the ids have a row) and `checked` (how many rows are ticked in the end)."""
    return f"""(() => {{
  const wanted = new Set({json.dumps(list(want_ids))});
  const boxes = Array.from(document.querySelectorAll('input[name="checkWantsRow[]"]'));
  let found = 0;
  for (const box of boxes) {{
    const should = wanted.has(box.getAttribute('data-id-want'));
    if (should) found++;
    if (box.checked !== should) box.click();
  }}
  return JSON.stringify({{found: found, checked: boxes.filter(b => b.checked).length}});
}})()"""


def tick_all_wants_script() -> str:
    """Tick the wants-list's "check all" box. Answers JSON with `found` (rows) and `checked` (rows ticked)."""
    return """(() => {
  const all = document.querySelector('input[name="checkAll"]');
  if (!all) return 'no element matches input[name="checkAll"]';
  if (!all.checked) all.click();
  const boxes = Array.from(document.querySelectorAll('input[name="checkWantsRow[]"]'));
  return JSON.stringify({found: boxes.length, checked: boxes.filter(b => b.checked).length});
})()"""


def read_ticks(answer: str) -> tuple[int, int] | None:
    """`(found, checked)` from a tick script's answer; None when it answered anything else."""
    try:
        data: object = json.loads(answer)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast("dict[str, object]", data)
    found, checked = fields.get("found"), fields.get("checked")
    if isinstance(found, int) and isinstance(checked, int):
        return found, checked
    return None
