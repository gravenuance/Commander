"""A meta report as JSON: the `cmdr meta --json` answer and the desktop app's Meta section."""

from commander.meta import CardShare, MetaReport
from commander.models import short_commander_name


def report_json(report: MetaReport) -> dict[str, object]:
    sample = report.sample
    return {
        "deck": report.deck,
        "commander": report.commander,
        "since": report.since.isoformat(),
        "sample": {
            "known": sample.known,
            "entries": sample.entries,
            "withLists": sample.with_lists,
            "topCuts": sample.top_cuts,
            "read": sample.performing,
            "basis": sample.basis.name.lower(),
            "thin": sample.thin,
        },
        "playStyle": [style.value for style in report.style.styles],
        "racing": report.style.races,
        "missing": [{**_share_json(c, "onSideboard"), "fit": c.fit.value or None} for c in report.missing],
        "spicy": [
            {
                "name": p.name,
                "spice": p.spice,
                "lists": p.lists,
                "best": {
                    "standing": p.best.standing,
                    "size": p.best.tournament.size,
                    "event": p.best.tournament.name,
                    "date": p.best.tournament.held_on.isoformat(),
                    "decklist": p.best.decklist_url,
                },
            }
            for p in report.spicy
        ],
        "outliers": [_share_json(c, "kept") for c in report.outliers],
        "keyCards": [{"name": k.name, "reasons": list(k.reasons)} for k in report.key_cards],
        "threats": [
            {
                "name": t.name,
                "kinds": [k.label for k in t.kinds],
                "hits": list(t.hits),
                "exposure": round(t.exposure, 5),
                "playedBy": [{"commander": n, "share": round(s, 4)} for n, s in t.played_by],
            }
            for t in report.threats
        ],
        "combos": [
            {
                "id": c.id,
                "url": c.url,
                "pieces": list(c.pieces),
                "requirements": list(c.requirements),
                "results": list(c.results),
                "exposure": round(c.exposure, 5),
                "playedBy": [
                    {"commander": n, "short": short_commander_name(n), "share": round(s, 4)}
                    for n, s in c.played_by
                ],
                "answers": [
                    {"card": a.card, "how": a.how, "role": a.role.value, "catch": a.catch} for a in c.answers
                ],
            }
            for c in report.combos
        ],
        "ownCombos": [
            {
                "id": c.id,
                "url": c.url,
                "pieces": list(c.pieces),
                "results": list(c.results),
                "answers": [
                    {
                        "card": a.card,
                        "how": a.how,
                        "role": a.role.value,
                        "catch": a.catch,
                        "exposure": round(a.exposure, 5),
                        "playedBy": [
                            {"commander": n, "short": short_commander_name(n), "share": round(s, 4)}
                            for n, s in a.played_by
                        ],
                    }
                    for a in c.answers
                ],
            }
            for c in report.own_combos
        ],
        "field": [
            {
                "commander": s.name,
                "entries": s.entries,
                "topCuts": s.top_cuts,
                "conversion": round(s.conversion, 4),
                "metaShare": round(s.meta_share, 5),
            }
            for s in report.field
        ],
        "peers": [
            {
                "id": p.id,
                "name": p.name,
                "views": p.views,
                "updatedAt": p.updated_at.isoformat(),
                "url": p.url,
            }
            for p in report.peers
        ],
        "peersSkipped": report.peers_skipped,
        "sources": [
            {
                "name": s.name,
                "source": s.source.value,
                "fetchedAt": s.fetched_at.isoformat() if s.fetched_at else None,
                "error": s.error,
            }
            for s in report.sources
        ],
    }


def _share_json(card: CardShare, flag: str) -> dict[str, object]:
    return {
        "name": card.name,
        "lists": card.lists,
        "share": round(card.share, 4),
        "peerLists": card.peer_lists,
        "peerShare": round(card.peer_share, 4),
        "spice": card.spice,
        flag: card.flagged,
    }
