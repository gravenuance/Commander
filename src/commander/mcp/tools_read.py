"""The read tools: they never change a deck, a board or Archidekt (the caches fill on a miss).

Each tool's docstring is its description. `deck` is a deck id or part of its name or commander."""

from mcp.server.mcpserver import MCPServer

from commander.mcp.argv import optional_deck, suggest_options
from commander.mcp.hints import READS, register_tools
from commander.mcp.session import Session, ToolResult
from commander.mcp.text import ArgumentError, flag, name_arg

MAX_CARDS_PER_LOOKUP = 20


class ReadTools:
    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, server: MCPServer) -> None:
        register_tools(
            server,
            READS,
            self.list_decks,
            self.analyze,
            self.price,
            self.card,
            self.tags,
            self.tokens,
            self.profile,
            self.board_show,
            self.board_log,
            self.toget_list,
            self.bracket,
            self.suggest,
            self.board_plan,
            self.board_commit_preview,
            self.apply_preview,
        )

    def list_decks(self) -> ToolResult:
        """List the decks saved locally: id, name, commanders, bracket."""
        return self._session.json_call(["list", "--json"])

    def analyze(self, deck: str | None = None) -> ToolResult:
        """Mana curve, card types, colours, role counts against Scryfall's Tagger tags (with the cards where
        they differ) and game changers of one deck, or of every deck when deck is omitted."""
        return self._session.json_call(["analyze", "--json", *optional_deck(deck)])

    def price(self, deck: str | None = None) -> ToolResult:
        """Deck value in EUR cents (Cardmarket prices via Scryfall), for one deck or every deck."""
        return self._session.json_call(["price", "--json", *optional_deck(deck)])

    def card(self, names: list[str]) -> ToolResult:
        """Rules text, roles, price and Tagger tags of cards, by name (any case; the front face is enough)."""
        if not 1 <= len(names) <= MAX_CARDS_PER_LOOKUP:
            raise ArgumentError(f"give 1 to {MAX_CARDS_PER_LOOKUP} card names")
        return self._session.json_call(["card", "--json", *(name_arg(name, "card name") for name in names)])

    def tags(self, text: str) -> ToolResult:
        """Tagger tags whose name contains the text (such as stax or blink), with their card counts."""
        return self._session.json_call(["tags", "--json", name_arg(text, "text")])

    def tokens(self, deck: str | None = None) -> ToolResult:
        """Tokens, emblems and helper cards that a deck's cards make, or every deck's."""
        return self._session.json_call(["tokens", "--json", *optional_deck(deck)])

    def profile(self, deck: str) -> ToolResult:
        """A deck's profile as suggest reads it: archetype, themes, exclusions, targets, key-card settings
        and what the deck leans into."""
        return self._session.json_call(["profile", "--json", name_arg(deck, "deck")])

    def board_show(self, deck: str) -> ToolResult:
        """A deck's board: cards under consideration with reasons and replacements, decisions, and queued
        count changes."""
        return self._session.json_call(["board", "show", "--json", name_arg(deck, "deck")])

    def board_log(self, deck: str | None = None, since: str | None = None) -> ToolResult:
        """What changed on the boards, oldest first, for one deck or all. since is a UTC time like
        2026-10-15T18:00: use it to catch up on the user's decisions."""
        options = [] if since is None else [flag("since", name_arg(since, "since"))]
        return self._session.json_call(["board", "log", "--json", *options, *optional_deck(deck)])

    def toget_list(self, deck: str | None = None) -> ToolResult:
        """The cards still to get (labelled Don't Have or Getting) for one deck or all."""
        return self._session.json_call(["toget", "--json", *optional_deck(deck)])

    def bracket(self, deck: str | None = None, *, refresh: bool = False) -> ToolResult:
        """Bracket estimate from Commander Spellbook beside the bracket set on Archidekt, with game
        changers, tutors and combos. Asks Spellbook only when its answer is not cached (a week); refresh
        asks again."""
        options = ["--refresh"] if refresh else []
        return self._session.json_call(["bracket", "--json", *options, *optional_deck(deck)])

    def suggest(
        self,
        deck: str | None = None,
        count: int | None = None,
        max_price_eur: float | None = None,
        *,
        any_price: bool = False,
        within_bracket: bool = False,
        refresh: bool = False,
    ) -> ToolResult:
        """Ideas for a deck: cards that complete combos, cards for roles short of target, and cuts to weigh.
        Does not touch the board (suggest_to_board does). Slow for every deck at once. max_price_eur leaves
        out dearer cards; any_price ignores the deck's own budget; within_bracket leaves out ideas that
        would lift the deck above its bracket."""
        options = suggest_options(
            count=count,
            max_price_eur=max_price_eur,
            any_price=any_price,
            within_bracket=within_bracket,
            refresh=refresh,
        )
        return self._session.json_call(["suggest", "--json", *options, *optional_deck(deck)])

    def board_plan(self, deck: str) -> ToolResult:
        """The board's queued cards, as the deck on Archidekt needs them, as a plan file (TOML) that
        apply_preview and apply_save read. Reads the deck from Archidekt."""
        return self._session.text_call(["board", "plan", name_arg(deck, "deck")])

    def board_commit_preview(self, deck: str) -> ToolResult:
        """Preview saving a deck's board to Archidekt: the changes, warnings and the bracket after. Changes
        nothing."""
        return self._session.text_call(["board", "commit", name_arg(deck, "deck")])

    def apply_preview(self, plan_toml: str) -> ToolResult:
        """Preview a plan of cards to add and remove on one deck against the deck on Archidekt: the changes,
        warnings, problems and the bracket after. plan_toml is the plan file's text, for example
        deck = "magda", add = ["Arcane Signet"], remove = ["Disruptor Flute", {name = "Mountain", keep = 5}].
        Changes nothing."""
        return self._session.json_call(["apply", "--json", "-"], stdin=plan_toml)
