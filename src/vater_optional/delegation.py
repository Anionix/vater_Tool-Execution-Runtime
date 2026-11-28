"""Agent-to-agent delegation of authority and cost (task.optional_more_protocols.007).

What a delegating agent may hand to another, independent agent, decided before any
Agent2Agent transport exists. A delegated grant is always strictly bounded by its parent:

  - tools: a non-empty, explicit subset of the parent's tools (no wildcard);
  - budget: a positive amount taken out of the parent's remaining budget (held, not copied,
    so the two can never spend more than the parent had together);
  - time: it ends no later than the parent, and within one hour;
  - depth: the delegate may pass authority on only if the parent allowed it and the chain is
    shorter than MAX_DEPTH;
  - approvals never travel: a tool that needs human approval must be approved again for the
    delegate by the root principal, whatever the delegating agent or a message claims.

Every refusal raises DelegationRefused with a stable code.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

MAX_DEPTH = 2
MAX_DELEGATION_SECONDS = 3600
WILDCARDS = {"*", "all", ""}


class DelegationRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Grant:
    principal: str
    root: str                      # the human principal the chain started from
    tools: frozenset[str]
    budget_cents: int
    expires_at: float
    may_delegate: bool = False
    depth: int = 0
    approved_tools: frozenset[str] = frozenset()  # human approvals given to this principal only


def delegate(parent: Grant, to: str, tools: set[str] | frozenset[str], budget_cents: int, expires_at: float,
             now: float, may_delegate: bool = False) -> tuple[Grant, Grant]:
    """Return (the delegate's grant, the parent's grant with the delegated budget held back)."""
    if now >= parent.expires_at:
        raise DelegationRefused("PARENT_EXPIRED")
    if not parent.may_delegate:
        raise DelegationRefused("DELEGATION_NOT_ALLOWED")
    if not isinstance(to, str) or not to or to in {parent.principal, parent.root}:
        raise DelegationRefused("INVALID_DELEGATE")
    tools = frozenset(tools)
    if not tools or tools & WILDCARDS:
        raise DelegationRefused("TOOLS_NOT_EXPLICIT")
    if not tools <= parent.tools:
        raise DelegationRefused("TOOLS_EXCEED_PARENT")
    if type(budget_cents) is not int or budget_cents <= 0:
        raise DelegationRefused("BUDGET_NOT_BOUNDED")
    if budget_cents > parent.budget_cents:
        raise DelegationRefused("BUDGET_EXCEEDS_PARENT")
    if expires_at > parent.expires_at or expires_at - now > MAX_DELEGATION_SECONDS or expires_at <= now:
        raise DelegationRefused("EXPIRY_NOT_BOUNDED")
    if may_delegate and parent.depth + 1 >= MAX_DEPTH:
        raise DelegationRefused("CHAIN_TOO_LONG")
    child = Grant(to, parent.root, tools, budget_cents, expires_at, may_delegate, parent.depth + 1, frozenset())
    return child, replace(parent, budget_cents=parent.budget_cents - budget_cents)


def spend(grant: Grant, tool: str, cost_cents: int, now: float, needs_approval: bool) -> Grant:
    """Charge one call to a grant; refuse anything outside it."""
    if now >= grant.expires_at:
        raise DelegationRefused("GRANT_EXPIRED")
    if tool not in grant.tools:
        raise DelegationRefused("TOOL_NOT_GRANTED")
    if type(cost_cents) is not int or cost_cents < 0:
        raise DelegationRefused("INVALID_COST")
    if cost_cents > grant.budget_cents:
        raise DelegationRefused("BUDGET_EXHAUSTED")
    if needs_approval and tool not in grant.approved_tools:
        raise DelegationRefused("APPROVAL_REQUIRED")
    return replace(grant, budget_cents=grant.budget_cents - cost_cents)


def approve(grant: Grant, tool: str, approver: str) -> Grant:
    """Only the chain's root (human) principal can approve a tool for a grant."""
    if approver != grant.root:
        raise DelegationRefused("APPROVER_NOT_ROOT")
    if tool not in grant.tools:
        raise DelegationRefused("TOOL_NOT_GRANTED")
    return replace(grant, approved_tools=grant.approved_tools | {tool})
