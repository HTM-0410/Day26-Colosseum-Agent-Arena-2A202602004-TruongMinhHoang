"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE IMPLEMENTED CONTROL PLANE
----------------------------------------------------------------------------
`decide()` applies four ordered jobs: ROUTE, ADMIT, AUTHORIZE, and BUDGET.
It fails closed on unknown tools, body-carried routes, invalid identity or
delegation evidence, poisoned arguments, unusable leases, unsafe writes,
rate-window exhaustion, and unaffordable calls.  Safe calls are narrowed
to explicit masks and deprecated tools are rewritten to their successor.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

try:
    from kit.mcp.specs import TOOL_SPECS, cost as _tool_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - arena collaborator
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _tool_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        return 6

from agent.strategy import CATALOG_TRAP_TOOLS, successor_of

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes are duel-local enforcement memory: budget/rate
    windows, fresh provenance, idempotency keys, and admitted Agent Cards.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory, unused by the naive starter below ---------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()

        # Security and protocol state.  All of it is duel-local; none of it
        # performs I/O.  The arena may feed successful observations through
        # note_result/note_provenance/note_card below.
        self._round = -1
        self._spent_this_round = 0
        self._rate_rounds: dict[tuple[str, str], list[int]] = {}
        self._provenance: dict[str, str] = {}
        self._provenance_round: dict[str, int] = {}
        self._used_idempotency_keys: set[str] = set()
        self._admitted_cards: dict[str, dict[str, Any]] = {}

    _A2A_SERVERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})
    _WRITE_TOOLS = frozenset({
        ("progress", "record_mastery"),
        ("content", "flag_stale_slide"),
    })
    _SAFE_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
        ("slides", "query"): ("title",),
        ("slides", "get_frame"): ("body", "meta", "title"),
        ("slides", "whatlinkshere"): ("targets",),
        ("glossary", "define"): ("definition", "sense", "source_term"),
        ("glossary", "list_terms"): ("term",),
        ("registry", "provenance"): ("etag", "rev"),
        ("registry", "list_servers"): ("name",),
        ("research", "cite_source"): ("anchor", "url"),
        ("labs", "get_exercise"): ("summary",),
        ("progress", "record_mastery"): ("receipt_id",),
        ("content", "flag_stale_slide"): ("receipt_id",),
        ("curriculum-analyst", "which_days_cover"): ("anchor", "course_day", "track"),
        ("citation-checker", "verify_source"): ("anchor", "confidence", "url"),
        ("roster", "lookup_learner"): ("act", "scopes"),
    }
    _ROUND_ALLOWANCE: Mapping[int, int] = {
        1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12,
    }
    _INJECTION_MARKERS = (
        "ignore previous instruction", "ignore all previous", "disregard the above",
        "system override", "you must now", "reveal the", "print the",
        "bỏ qua mọi hướng dẫn", "bỏ qua hướng dẫn", "tiết lộ",
        "also record this for", "instead, record",
    )

    @staticmethod
    def _normalise_identity(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw.startswith("learner:"):
            return raw
        if raw.startswith("learner/"):
            return "learner:" + raw.split("/", 1)[1]
        return raw

    @staticmethod
    def _lower_headers(headers: Mapping[str, Any]) -> dict[str, str]:
        return {str(k).strip().lower(): str(v) for k, v in headers.items()}

    def _emit_safely(self, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self._telemetry, method)(*args, **kwargs)
        except Exception:
            # Own telemetry is explicitly non-authoritative and must never
            # turn a safe gateway decision into an integrity failure.
            return

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The implementation is fail-closed: an internal error is converted
        to a valid zero-cost denial rather than escaping this method."""
        self._emit_safely("decision_seen", cmd)
        try:
            return self._decide(cmd)
        except Exception as exc:
            # Fail closed on our own bug while still returning a valid
            # Decision.  Never leak input content or exception detail.
            return self.deny(cmd, f"gateway fail-closed: {type(exc).__name__}")

    def _decide(self, cmd: Command) -> Decision:
        key = (cmd.server, cmd.tool)
        if _SPECS_AVAILABLE and key not in TOOL_SPECS:
            return self.deny(cmd, "unknown server/tool is not on the arena allowlist")

        round_no = int(getattr(self.ctx, "round", 0) or 0)
        if round_no != self._round:
            self._round = round_no
            self._spent_this_round = 0

        headers = self._lower_headers(cmd.headers)
        args = dict(cmd.args)
        server, tool = key
        rewritten = False

        # ROUTE: body-carried routing is never authoritative.  These keys
        # are attack surface, not harmless aliases.
        if any(args.get(k) not in (None, "") for k in ("route", "_route", "replica")):
            return self.deny(cmd, "route/replica must be carried by a trusted header, not the body")
        if headers.get("x-server-fingerprint") in {"unvouched", "invalid", "forged"}:
            return self.deny(cmd, "server identity is not vouched for by the registry")
        if headers.get("x-card-signature") in {"unvouched", "invalid", "forged"}:
            return self.deny(cmd, "agent card signature is invalid")

        aud = headers.get("aud")
        if aud is not None:
            if server not in self._A2A_SERVERS and "-" not in server:
                return self.deny(cmd, "mcp command must not carry an audience header")
            if aud != server:
                return self.deny(cmd, "delegation audience does not match the target peer")


        successor = successor_of(server, tool)
        if successor is not None:
            server, tool = successor
            key = successor
            rewritten = True

        if server == "slides":
            replica = headers.get("mcp-replica", "w").lower()
            if replica not in {"w", "c"}:
                return self.deny(cmd, "invalid MCP replica header")
            headers["mcp-replica"] = replica

        # ADMIT: reject calls that are guaranteed to fail or which clearly
        # carry instruction-shaped content from a poisoned observation.
        if key == ("slides", "get_frame"):
            live_leases = tuple(getattr(self.ctx, "leases", ()) or ())
            if not cmd.lease_id or cmd.lease_id not in live_leases:
                return self.deny(cmd, "get_frame requires a live lease from this round")

        blob = " ".join(str(v) for v in args.values()).lower()
        if any(marker in blob for marker in self._INJECTION_MARKERS):
            decision = self.deny(cmd, "instruction-shaped retrieved content was quarantined")
            return Decision(verdict="deny", reason=decision.reason, quarantine=True)
        if "peer_unverified" in args or args.get("verified") is False:
            return self.deny(cmd, "unverified peer result requires an independent source")
        if any(len(str(v)) > 1024 for v in args.values()):
            return self.deny(cmd, "oversized argument refused before context/cost amplification")

        # AUTHORIZE: authority derives from act, never sub.
        scopes = frozenset(str(s).lower() for s in (getattr(self.ctx, "scopes", ()) or ()))
        if key in self._WRITE_TOOLS:
            required_scope = f"wiki.write:{server}"
            if required_scope not in scopes:
                return self.deny(cmd, f"missing required scope {required_scope}")
            act = self._normalise_identity(getattr(self.ctx, "act", ""))
            target = next((args.get(k) for k in ("learner", "learner_id", "target", "subject") if args.get(k)), None)
            if target is not None and self._normalise_identity(target) != act:
                return self.deny(cmd, "write target is not owned by ctx.act")
            anchor = str(args.get("anchor") or "")
            if not anchor:
                return self.deny(cmd, "write requires the freshly pinned anchor in args.anchor")
            if self._provenance_round.get(anchor) != round_no:
                return self.deny(cmd, "write requires provenance read in the current round")
            expected_etag = self._provenance.get(anchor)
            if not expected_etag or headers.get("if-match") != expected_etag:
                return self.deny(cmd, "If-Match does not equal the latest observed provenance etag")
            idem = headers.get("idempotency-key", "")
            if not idem or idem in self._used_idempotency_keys:
                return self.deny(cmd, "missing or replayed Idempotency-Key")

        if server in self._A2A_SERVERS:
            card = self._admitted_cards.get(server)
            if not card or not card.get("verified"):
                return self.deny(cmd, "peer Agent Card has not been verified")
            if tool not in set(card.get("skills") or ()):
                return self.deny(cmd, "requested skill is not declared by the verified Agent Card")
            aud = headers.get("aud", "")
            if aud != server:
                return self.deny(cmd, "delegation audience does not match the target peer")
            delegated_act = args.get("act") or args.get("learner") or args.get("learner_id")
            if delegated_act and self._normalise_identity(delegated_act) != self._normalise_identity(getattr(self.ctx, "act", "")):
                return self.deny(cmd, "delegated act does not match ctx.act")

        # BUDGET: validate/narrow masks and keep enough credits for later
        # rounds.  The estimate assumes one returned row, which is the
        # conservative useful-call baseline used by the kit cost tests.
        requested = tuple(cmd.fields)
        safe = tuple(self._SAFE_MASKS.get(key, requested))
        if _SPECS_AVAILABLE:
            allowed = frozenset(TOOL_SPECS[key].all_fields)
            if any(f != "*" and f not in allowed for f in requested):
                return self.deny(cmd, "field mask contains a field not exposed by this tool")
            safe = tuple(f for f in safe if f in allowed)
        if not requested or requested == ("*",) or key in CATALOG_TRAP_TOOLS:
            fields = safe
            rewritten = True
        else:
            fields = requested

        estimate = _tool_cost(server, tool, fields=fields, n_rows=1)
        credits = int(getattr(self.ctx, "credits", 0) or 0)
        allowance = self._ROUND_ALLOWANCE.get(round_no, 9)
        if estimate > credits:
            return self.deny(cmd, "insufficient duel credits for this call")
        if self._spent_this_round + estimate > allowance:
            return self.deny(cmd, "round allowance exhausted; preserving later-round budget")

        if _SPECS_AVAILABLE and TOOL_SPECS[key].rate_limit:
            allowed_calls, window = TOOL_SPECS[key].rate_limit
            used = self._rate_rounds.setdefault(key, [])
            floor = round_no - window + 1
            used[:] = [r for r in used if r >= floor]
            if len(used) >= allowed_calls:
                return self.deny(cmd, "local rate window is exhausted")
            used.append(round_no)

        call = ToolCall(
            server=server,
            tool=tool,
            args=args,
            fields=fields,
            headers=headers,
            lease_id=cmd.lease_id,
            call_index=cmd.call_index,
        ) if _TOOLCALL_AVAILABLE else self._to_tool_call(cmd)

        self._spent_this_round += estimate
        self._credits_authorised += estimate
        if key in self._WRITE_TOOLS:
            self._used_idempotency_keys.add(headers["idempotency-key"])
        decision = Decision(
            verdict="rewrite" if rewritten or server != cmd.server or tool != cmd.tool or headers != cmd.headers else "forward",
            call=call,
            note=f"estimated_cost={estimate};round_spend={self._spent_this_round}",
        )
        self._emit_safely("decision_made", cmd, decision)
        self._emit_safely(
            "budget_snapshot", round=round_no, credits_left=credits,
            spent_this_round=self._spent_this_round,
        )
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Build a valid, zero-cost denial and record it in own telemetry."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._emit_safely("decision_made", cmd, decision)
        return decision

    def note_provenance(self, anchor: str, etag: str) -> None:
        """Record a successful registry.provenance observation."""
        if isinstance(anchor, str) and anchor and isinstance(etag, str) and etag:
            self._provenance[anchor] = etag
            self._provenance_round[anchor] = int(getattr(self.ctx, "round", 0) or 0)

    def note_result(self, anchor: str, etag: str) -> None:
        """Compatibility alias used by the practice harness/bots."""
        self.note_provenance(anchor, etag)

    def note_card(self, server: str, card: Mapping[str, Any]) -> None:
        """Cache only a registry-verified, server-matching Agent Card."""
        if not isinstance(server, str) or server not in self._A2A_SERVERS:
            return
        if not isinstance(card, Mapping) or card.get("verified") is not True:
            return
        name = str(card.get("name") or server)
        if name != server:
            return
        skills = tuple(str(s) for s in (card.get("skills") or ()))
        self._admitted_cards[server] = {"verified": True, "skills": skills}

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — enforcement smoke checks ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    clean = gw.decide(demo_commands[0])
    print(f"  clean registry.provenance -> {clean.verdict!r}")
    assert clean.verdict == "forward" and clean.call is not None

    routed = Command(
        cmd_id="cmd:route", kind="mcp", raw="slides.query route=c",
        server="slides", tool="query", args={"q": "mcp", "route": "c"},
        fields=("title",), headers={}, lease_id=None, call_index=4,
    )
    blocked = gw.decide(routed)
    print(f"  body-carried replica route -> {blocked.verdict!r}")
    assert blocked.verdict == "deny"

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= 5

    print("\nAll agent/gateway.py demos passed.")
