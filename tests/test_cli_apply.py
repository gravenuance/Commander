"""`cmdr login`, `cmdr logout` and `cmdr apply` end to end, against the fake Archidekt."""

import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.carddb import CARD_DB_FILE
from commander.cli import main
from commander.credentials import SERVICE
from commander.net import Pacer
from commander.store import Workspace
from conftest import DECK_ID, PASSWORD, REFRESH_TOKEN, FakeArchidekt, Json, MemoryKeyring

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DECK_URL = f"https://archidekt.com/decks/{DECK_ID}"
SNAPSHOT = "before cmdr apply 2026-10-15 12:00:00 UTC"
PLAN = """\
deck = "magda"
add = ["Arcane Signet"]
remove = ["Disruptor Flute", {name = "Mountain", keep = 5}]
"""
PREVIEW = f"""\
Honestly, though.. ({DECK_ID}), bracket 5: {DECK_URL}
  cards in deck: 9 -> 8
  - Disruptor Flute
  - Mountain: 6 -> 5 copies
  + Arcane Signet (FRC 20, €0.30) in Artifact
  warning: the deck will have 8 cards, not 100
"""


class Terminal(io.BytesIO):
    def isatty(self) -> bool:
        return True


class Run:
    def __init__(self, code: int, out: str, err: str, prompts: list[str]) -> None:
        self.code = code
        self.out = out
        self.err = err
        self.prompts = prompts


@pytest.fixture
def archidekt(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    return fake


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    return tmp_path


def run(
    args: list[str],
    home: Path,
    fake: FakeArchidekt,
    *,
    stdin: io.BytesIO | None = None,
    password: str = PASSWORD,
) -> Run:
    out, err = io.StringIO(), io.StringIO()
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return password

    env = {
        "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
        "CMDR_CACHE_DIR": str(home / "cache"),
        "CMDR_USER": "MAKKIS",
    }
    code = main(
        args,
        env=env,
        client_factory=fake.client,
        stdout=out,
        stderr=err,
        now=lambda: NOW,
        prompt_password=prompt,
        stdin=stdin if stdin is not None else io.BytesIO(),
        pacer_factory=lambda _: Pacer(0),
    )
    return Run(code, out.getvalue(), err.getvalue(), prompts)


def signed_in(keyring_: MemoryKeyring) -> None:
    keyring_.secrets[(SERVICE, "makkis")] = REFRESH_TOKEN


@pytest.fixture
def decks(home: Path, archidekt: FakeArchidekt) -> Path:
    folder = home / "decks"
    assert run(["sync", "--no-cards", "--no-ideas", "--workspace", str(folder)], home, archidekt).code == 0
    archidekt.requests.clear()
    return folder


def apply(home: Path, fake: FakeArchidekt, decks: Path, *extra: str, plan: str = PLAN) -> Run:
    path = home / "plan.toml"
    path.write_text(plan, encoding="utf-8")
    return run(["apply", str(path), "--workspace", str(decks), *extra], home, fake)


def saved_deck(decks: Path) -> Json:
    data: Json = json.loads((decks / "10004639-honestly-though" / "deck.json").read_text(encoding="utf-8"))
    return data


def test_login_asks_on_the_terminal_and_keeps_only_the_token(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    result = run(["login"], home, archidekt, stdin=Terminal())

    assert result.code == 0
    assert result.prompts == ["Archidekt password for MAKKIS: "]
    assert result.out == "signed in to Archidekt as MAKKIS; the login is kept in Windows Credential Manager\n"
    assert memory_keyring.secrets == {(SERVICE, "makkis"): REFRESH_TOKEN}
    assert PASSWORD not in result.out + result.err


def test_login_reads_the_password_from_stdin_when_asked(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    stdin = io.BytesIO(f"{PASSWORD}\r\nnext line\n".encode())

    result = run(["login", "--password-stdin", "--as", "me@example.org"], home, archidekt, stdin=stdin)

    assert result.code == 0
    assert result.prompts == []
    assert archidekt.writes[0][2] == {"email": "me@example.org", "password": PASSWORD}
    assert (SERVICE, "makkis") in memory_keyring.secrets


def test_login_without_a_terminal_needs_password_stdin(home: Path, archidekt: FakeArchidekt) -> None:
    result = run(["login"], home, archidekt)

    assert result.code == 2
    assert result.err == "cmdr: no terminal to ask for the password on; pass --password-stdin\n"
    assert archidekt.writes == []


def test_a_wrong_password_saves_nothing(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    result = run(["login"], home, archidekt, stdin=Terminal(), password="nope")

    assert result.code == 1
    assert result.err == "cmdr: Archidekt refused the login; check the name and password\n"
    assert memory_keyring.secrets == {}


def test_logout_forgets_the_login(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)

    first = run(["logout"], home, archidekt)
    second = run(["logout"], home, archidekt)

    assert (first.code, first.out) == (0, "removed the saved login for MAKKIS\n")
    assert (second.code, second.out) == (0, "no saved login for MAKKIS\n")
    assert memory_keyring.secrets == {}


def test_apply_previews_without_signing_in_or_writing(
    home: Path, archidekt: FakeArchidekt, decks: Path
) -> None:
    result = apply(home, archidekt, decks)

    assert result.code == 0
    assert (
        result.out
        == PREVIEW
        + "  bracket after these changes: 4+ (Ruthless)\n"
        + "preview only: run it again with --save to make these changes on Archidekt\n"
    )
    assert archidekt.requests == [f"/api/decks/{DECK_ID}/"]
    assert archidekt.writes == []


def test_apply_save_needs_a_login(home: Path, archidekt: FakeArchidekt, decks: Path) -> None:
    result = apply(home, archidekt, decks, "--save")

    assert result.code == 1
    assert result.err == "cmdr: not signed in to Archidekt as MAKKIS; run `cmdr login`\n"
    assert archidekt.writes == []


def test_apply_save_changes_the_deck_and_the_local_copy_and_is_safe_to_repeat(
    home: Path, archidekt: FakeArchidekt, decks: Path, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)

    result = apply(home, archidekt, decks, "--save")

    assert result.code == 0, result.err
    assert result.out == (
        PREVIEW
        + f"saved 3 change(s); snapshot '{SNAPSHOT}' is on Archidekt\n"
        + f"local copy updated: {decks / '10004639-honestly-though'}\n"
    )
    assert [(m, p) for m, p, _, _ in archidekt.writes] == [
        ("POST", "/api/rest-auth/token/refresh/"),
        ("POST", f"/api/decks/{DECK_ID}/snapshots/"),
        ("PATCH", f"/api/decks/{DECK_ID}/modifyCards/v2/"),
    ]
    assert [(e["action"], e["modifications"]["quantity"]) for e in archidekt.card_writes()] == [
        ("remove", 1),
        ("modify", 5),
        ("add", 1),
    ]
    names = {c["name"]: c["quantity"] for c in saved_deck(decks)["cards"]}
    assert names["Mountain"] == 5
    assert "Arcane Signet" in names
    assert "Disruptor Flute" not in names

    again = apply(home, archidekt, decks, "--save")

    assert again.code == 0
    assert again.out.endswith("nothing to change: the deck already matches the plan\n")
    assert "  skipped, add Arcane Signet: already 1 in the deck\n" in again.out
    assert len(archidekt.writes) == 4  # only the token refresh of the second run


def test_a_plan_with_problems_changes_nothing_even_with_save(
    home: Path, archidekt: FakeArchidekt, decks: Path, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    plan = 'deck = "magda"\nadd = ["Arcane Signet", "Rhystic Study"]\n'

    result = apply(home, archidekt, decks, "--save", plan=plan)

    assert result.code == 2
    assert "  problem: add Rhystic Study: its colours (U) are outside the deck's (R)\n" in result.out
    assert result.out.endswith("nothing was changed: fix the problems above, then run it again\n")
    assert [p for _, p, _, _ in archidekt.writes] == ["/api/rest-auth/token/refresh/"]


def test_apply_refuses_a_deck_someone_else_owns(
    home: Path, archidekt: FakeArchidekt, decks: Path, raw_deck: Json
) -> None:
    raw_deck["owner"]["username"] = "SomeoneElse"
    archidekt.put(raw_deck)

    result = apply(home, archidekt, decks)

    assert result.code == 2
    assert result.err == f"cmdr: deck {DECK_ID} belongs to SomeoneElse, not MAKKIS; cmdr edits only yours\n"


def test_apply_by_id_works_for_a_private_deck_outside_the_deck_folder(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    archidekt.private.add(DECK_ID)
    signed_in(memory_keyring)
    plan = f'deck = {DECK_ID}\nremove = ["Disruptor Flute"]\n'

    result = run(["apply", "-", "--save"], home, archidekt, stdin=io.BytesIO(plan.encode()))

    assert result.code == 0, result.err
    assert "the deck is not in your deck folder, so no local copy was written\n" in result.out


def test_a_preview_with_an_expired_login_goes_on_without_it(
    home: Path, archidekt: FakeArchidekt, decks: Path, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets[(SERVICE, "makkis")] = "expired"

    result = apply(home, archidekt, decks)

    assert result.code == 0
    assert result.err == (
        "cmdr: the saved Archidekt login has expired; run `cmdr login`; previewing without signing in\n"
    )


def test_apply_json(home: Path, archidekt: FakeArchidekt, decks: Path) -> None:
    document = json.loads(apply(home, archidekt, decks, "--json").out)

    assert document["deck"] == {"id": DECK_ID, "name": "Honestly, though..", "url": DECK_URL, "bracket": 5}
    assert (document["cardsBefore"], document["cardsAfter"], document["saved"]) == (9, 8, False)
    assert document["changes"][2] == {
        "kind": "add",
        "name": "Arcane Signet",
        "before": 0,
        "after": 1,
        "category": "Artifact",
        "inDeck": True,
        "set": "frc",
        "collectorNumber": "20",
        "scryfallId": document["changes"][2]["scryfallId"],
        "priceCents": 30,
        "raises": None,
    }
    assert document["warnings"] == ["the deck will have 8 cards, not 100"]


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ('deck = "magda"\n', "nothing to add, remove or drop"),
        ('deck = "nomatch"\nadd = ["Sol Ring"]\n', "no deck matches 'nomatch'"),
        ("deck = ", "cannot read plan"),
    ],
)
def test_bad_plans_are_usage_errors(
    home: Path, archidekt: FakeArchidekt, decks: Path, plan: str, message: str
) -> None:
    result = apply(home, archidekt, decks, plan=plan)

    assert result.code == 2
    assert message in result.err
    assert archidekt.writes == []


def test_a_password_on_stdin_that_is_not_utf8_is_a_usage_error(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    stdin = io.BytesIO("pässword\n".encode("cp1252"))

    result = run(["login", "--password-stdin"], home, archidekt, stdin=stdin)

    assert result.code == 2
    assert result.err == "cmdr: the password on stdin must be UTF-8 text\n"
    assert archidekt.writes == []
    assert memory_keyring.secrets == {}


def test_sync_keeps_the_account_labels_for_the_app_when_signed_in(
    home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    archidekt.labels = [
        {"id": 1, "color": "#FA0D11", "name": "Don't Have"},
        {"id": 2, "color": "", "name": "x"},
    ]
    signed_in(memory_keyring)
    result = run(["sync", "--no-cards", "--no-ideas", "--workspace", str(home / "decks")], home, archidekt)
    assert result.code == 0
    assert "card labels: Don't Have (from your Archidekt account)\n" in result.out
    assert Workspace(home / "decks").account_labels() == [("Don't Have", "#fa0d11")]


def test_sync_without_a_login_reads_no_labels(decks: Path, archidekt: FakeArchidekt) -> None:
    assert Workspace(decks).account_labels() is None
    assert "/api/decks/colorTags/" not in archidekt.requests


def test_a_save_labels_incoming_cards_dont_have_in_the_account_colour(
    home: Path, archidekt: FakeArchidekt, decks: Path, memory_keyring: MemoryKeyring
) -> None:
    archidekt.labels = [{"id": 2, "color": "#fa0d11", "name": "Don't Have"}]
    signed_in(memory_keyring)
    assert apply(home, archidekt, decks, "--save").code == 0
    added = [w for w in archidekt.card_writes() if "deckRelationId" not in w]
    assert added
    assert {w["modifications"]["label"] for w in added} == {"Don't Have,#fa0d11"}
