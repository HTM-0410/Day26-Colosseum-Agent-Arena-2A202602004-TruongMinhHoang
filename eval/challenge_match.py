"""Generate a two-sided Mirror Challenger replay.

The challenger uses the student's own Gateway, prosecutor, deck, and lineup on
both sides under the kit's deterministic MockBroker exchange.  It is explicitly
labelled as a simulation, never represented as another student's real team.
Unlike ``spar.py --ui``, this writer includes both sides' traces and claim
outcomes so neither defence row remains ``(waiting)`` in the replay UI.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Mapping

import spar
from kit.referee.rubric import weight_of


ROOT = Path(__file__).resolve().parents[1]
START_HP = 100
TRACE_TYPES = frozenset({
    "command", "decision", "enforced", "tool_call", "tool_result",
    "mutation", "answer", "integrity",
})


def _claim_events(
    put,
    result: Mapping[str, Any],
    *,
    prosecutor_side: str,
    round_no: int,
) -> None:
    for claim in result.get("verified", ()):
        cls = claim["cls"]
        put(
            2, "claim_outcome", side=prosecutor_side, producer="referee", round=round_no,
            cls=cls, evidence=claim.get("evidence", ()), outcome="verified",
            weight=weight_of(cls), scaled=round(weight_of(cls) * spar.round_scale(round_no)),
        )
    for claim in result.get("false", ()):
        cls = claim["cls"]
        put(
            2, "claim_outcome", side=prosecutor_side, producer="referee", round=round_no,
            cls=cls, evidence=claim.get("evidence", ()), outcome="false",
            weight=weight_of(cls), scaled=-round(0.8 * weight_of(cls) * spar.round_scale(round_no)),
        )
    for claim in result.get("pending", ()):
        put(
            2, "claim_filed", side=prosecutor_side, producer="student", round=round_no,
            cls=claim.get("cls"), evidence=claim.get("evidence", ()),
            expected=claim.get("expected"), observed=claim.get("observed"),
            argument=claim.get("argument"),
        )


def generate(*, seed: int = 2, rounds: int = 10, run_name: str | None = None) -> Path:
    world = spar._load_world()
    gateway_cls, prosecute_fn, deck, lineup = spar._load_side("you")
    cards = {card["id"]: card for card in deck["cards"]}
    run_name = run_name or f"challenge-mirror-{seed}"
    run_dir = ROOT / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    hp_you = hp_mirror = START_HP
    summary: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seq = 0

    def put(layer: int, type_: str, side: str | None = None, producer: str = "arena", **payload: Any) -> None:
        nonlocal seq
        round_no = int(payload.pop("round", 0) or 0)
        events.append({
            "v": 1,
            "layer": layer,
            "seq": seq,
            "t": round(seq * 0.10, 3),
            "run_id": run_name,
            "duel_id": "mirror-challenge",
            "exchange_id": "events",
            "round": round_no,
            "side": side,
            "producer": producer,
            "type": type_,
            "p": payload,
        })
        seq += 1

    for round_no in range(1, min(rounds, len(lineup)) + 1):
        card_you = cards[lineup[round_no - 1]]
        card_mirror = cards[lineup[round_no - 1]]

        you_defend = spar._exchange(
            "MIRROR CHALLENGER", "YOU", gateway_cls, prosecute_fn,
            card_mirror, world, round_no, rng, "learner:sv-0417",
        )
        mirror_defends = spar._exchange(
            "YOU", "MIRROR CHALLENGER", gateway_cls, prosecute_fn,
            card_you, world, round_no, rng, "learner:sv-0417",
        )

        for side, attacker, defender, card, result in (
            ("A", "MIRROR CHALLENGER", "YOU", card_mirror, you_defend),
            ("B", "YOU", "MIRROR CHALLENGER", card_you, mirror_defends),
        ):
            put(
                1, "exchange_start", side=side, round=round_no,
                attacker=attacker, defender=defender, card_id=card.get("id"),
                ask=card.get("ask"), world_id=world.manifest.get("world_id"),
            )
            for event in result["trace"]:
                if event.get("type") not in TRACE_TYPES:
                    continue
                payload = event.get("p") if isinstance(event.get("p"), Mapping) else {}
                put(1, str(event["type"]), side=side, round=round_no, **dict(payload))

        # Claims against A are filed by B; claims against B are filed by A.
        _claim_events(put, you_defend, prosecutor_side="B", round_no=round_no)
        _claim_events(put, mirror_defends, prosecutor_side="A", round_no=round_no)

        for missed in you_defend.get("missed", ()):
            cls = missed["cls"]
            put(
                2, "latent_violation", side="A", producer="referee", round=round_no,
                cls=cls, evidence=[f"evt:{missed['seq']:04d}"], weight=weight_of(cls),
            )
        for missed in mirror_defends.get("missed", ()):
            cls = missed["cls"]
            put(
                2, "latent_violation", side="B", producer="referee", round=round_no,
                cls=cls, evidence=[f"evt:{missed['seq']:04d}"], weight=weight_of(cls),
            )

        hp_you = max(0, hp_you - you_defend["damage"] - mirror_defends["recoil"])
        hp_mirror = max(0, hp_mirror - mirror_defends["damage"] - you_defend["recoil"])
        summary.append({
            "round": round_no,
            "hp_you": hp_you,
            "hp_bot": hp_mirror,
            "took": you_defend["damage"] + mirror_defends["recoil"],
            "dealt": mirror_defends["damage"] + you_defend["recoil"],
        })
        put(3, "hp", producer="referee", round=round_no, A=hp_you, B=hp_mirror)
        put(
            3, "round_end", producer="referee", round=round_no,
            hp_a=hp_you, hp_b=hp_mirror,
            zero_zero=you_defend["damage"] == 0 and mirror_defends["damage"] == 0,
        )
        if hp_you <= 0 or hp_mirror <= 0:
            break

    winner = "YOU" if hp_you > hp_mirror else ("MIRROR CHALLENGER" if hp_mirror > hp_you else "DRAW")
    put(
        3, "duel_end", producer="referee", round=len(summary), winner="A" if winner == "YOU" else "B",
        hp_a=hp_you, hp_b=hp_mirror, rounds_played=len(summary), reason="round_limit",
    )

    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "match.json").write_text(json.dumps({
        "kind": "mirror",
        "opponent": "MIRROR CHALLENGER",
        "seed": seed,
        "description": "Self-play simulation: same student Gateway, prosecutor, deck and lineup under MockBroker",
        "winner": winner,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "events.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"run: {run_dir}")
    print(f"result: YOU {hp_you} - {hp_mirror} MIRROR CHALLENGER ({len(summary)} rounds, {len(events)} events)")
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--run-name", default=None)
    args = parser.parse_args(argv)
    generate(seed=args.seed, rounds=args.rounds, run_name=args.run_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
