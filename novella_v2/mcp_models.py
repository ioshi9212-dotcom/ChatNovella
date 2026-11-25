"""Typed V2 MCP payloads. No Lite models, IDs, routes or credentials."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SessionId = Annotated[str, Field(pattern=r"^NV2_[0-9a-f]{24}$")]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RequestId = Annotated[str, Field(min_length=1, max_length=100)]
Version = Annotated[int, Field(ge=1)]
ShortText = Annotated[str, Field(min_length=1, max_length=240)]
Status = Literal["unaware", "aware", "acquainted"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnchorBlock(Model):
    anchor: Annotated[str, Field(min_length=1)]
    target: str


class RangeBlock(Model):
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=1)]
    target: str


class Relationships(Model):
    """Initial author-approved links; the engine supplies its immutable rule."""
    familiarity: dict[str, dict[str, Status]]
    npc_to_pov: dict[str, dict[str, Annotated[int, Field(ge=0, le=100)]]]
    npc_to_npc: dict[str, dict[str, ShortText]]


class ThreadUpdate(Model):
    id: str
    title: str
    status: Literal["not_started", "active", "closed"]
    note: str
    source: Annotated[str, Field(min_length=1)]


class Actor(Model):
    position: Annotated[str, Field(max_length=240)]
    clothing: Annotated[str, Field(max_length=240)]
    footwear: Annotated[str, Field(max_length=240)]
    items: list[ShortText]
    physical: Annotated[str, Field(max_length=240)]
    unfinished: list[ShortText]


class State(Model):
    turn: Annotated[int, Field(ge=0)]
    datetime: str | None
    location: ShortText
    pov: Actor
    present: dict[str, Actor]
    remote: dict[str, Annotated[str, Field(min_length=1, max_length=120)]]
    objects: list[ShortText]
    unfinished: list[ShortText]


class Knowledge(Model):
    owner: str
    fact: Annotated[str, Field(min_length=1, max_length=350)]
    source: Annotated[str, Field(min_length=3, max_length=400)]


class Event(Model):
    text: Annotated[str, Field(min_length=1, max_length=350)]
    participants: list[str]


class FamiliarityChange(Model):
    source: str
    target: str
    status: Status
    reason: Annotated[str, Field(min_length=1, max_length=180)]


class FeelingChange(Model):
    source: str
    target: str
    metric: str | None
    value: int | str
    reason: Annotated[str, Field(min_length=1, max_length=180)]
    significant: bool = False
    evidence: Annotated[str, Field(min_length=8, max_length=260)]


class SceneAccess(Model):
    character: str
    channel: Literal["physical", "remote", "ability"]
    source: str


class ReviewCheck(Model):
    section: str
    result: Literal["pass", "not_applicable", "fail"]
    note: Annotated[str, Field(min_length=12, max_length=1200)]


class Turn(Model):
    request_id: RequestId
    expected_version: Version
    packet_id: str
    chunk_hashes: list[Checksum]
    reader_text: str
    narration: Annotated[str, Field(min_length=1, max_length=120_000)]
    participants: list[str]
    state: State
    knowledge: list[Knowledge]
    events: list[Event]
    relations: list[FamiliarityChange | FeelingChange]
    thread_updates: list[ThreadUpdate] = Field(default_factory=list)
    scene_access: list[SceneAccess] = Field(default_factory=list)


class AuditReport(Model):
    continuity: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    knowledge: Annotated[str, Field(min_length=1)]
    timeline: Annotated[str, Field(min_length=1)]
    relationships: Annotated[str, Field(min_length=1)]
    long_term: Annotated[str, Field(min_length=1)] | None = None


class Correction(Model):
    before_sha256: Checksum
    after: str
    reason: Annotated[str, Field(min_length=1)]
    evidence: Annotated[str, Field(min_length=8, max_length=260)] | None = None
