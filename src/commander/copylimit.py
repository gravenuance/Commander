"""How many copies of one card a Commander deck may hold: one, unless it is a basic land or its rules text
says otherwise, as Relentless Rats ("A deck can have any number of cards named ...") and Seven Dwarves
("A deck can have up to seven cards named ...") do."""

import re

from commander.carddb import CardInfo

_ANY_NUMBER = re.compile(r"\ba deck can have any number of cards named\b", re.IGNORECASE)
_UP_TO = re.compile(r"\ba deck can have up to (\w+) cards named\b", re.IGNORECASE)
_NUMBERS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def copy_limit(info: CardInfo, text: str) -> int | None:
    """The most copies of the card a deck may hold, from its type line and rules text; None for any number."""
    if info.front_type_line.startswith("Basic "):
        return None
    if _ANY_NUMBER.search(text):
        return None
    found = _UP_TO.search(text)
    if found is None:
        return 1
    word = found.group(1).casefold()
    return int(word) if word.isdigit() else _NUMBERS.get(word, 1)
