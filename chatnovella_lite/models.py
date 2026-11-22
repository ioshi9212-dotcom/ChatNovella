"""Small technical input contracts; no literary scoring or scene planning."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Actor(Model):
    position: str
    clothing: str
    footwear: str
    items: list[str]
    physical: str
    unfinished: list[str]

class State(Model):
    turn: int = Field(ge=0)
    datetime: str | None
    location: str = Field(min_length=1)
    pov: Actor
    present: dict[str, Actor]
    remote: dict[str, str]
    objects: list[str]
    unfinished: list[str]

class Relationships(Model):
    rules: str
    npc_to_pov: dict[str, dict[str, int]]
    npc_to_npc: dict[str, dict[str, str]]

class RelationChange(Model):
    source: str
    target: str
    metric: str | None = None
    value: int | str | None
    reason: str = Field(min_length=1)
    significant: bool = False

class Event(Model):
    date: str | None
    participants: list[str]
    event: str = Field(min_length=1, max_length=350)
    consequence: str = Field(min_length=1, max_length=220)
    # World timeline is for durable milestones, not every NPC observation.
    significance: Literal['turning_point', 'revelation', 'irreversible_change', 'commitment', 'major_consequence']
    significance_reason: str = Field(min_length=12, max_length=240)

class Knowledge(Model):
    npc_id: str
    fact: str = Field(min_length=1)
    status: Literal['knows', 'suspects', 'mistaken', 'refuted']
    source_kind: Literal['witnessed', 'told', 'phone', 'message', 'inference', 'offscreen']
    source_ids: list[str]
    source: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    event_index: int | None = None
    supersedes: int | None = None

class Turn(Model):
    request_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    packet_id: str
    chunk_hashes: list[str]
    reader_text: str = Field(min_length=1)
    narration: str = Field(min_length=1)
    participants: list[str]
    state_patch: dict
    relations: list[RelationChange] = []
    # Every active NPC (and each offscreen knowledge owner) must be reviewed
    # explicitly. 'recorded' requires a personal-memory entry for that owner.
    knowledge_review: dict[str, Literal['recorded', 'none']]
    knowledge: list[Knowledge] = []
    events: list[Event] = []
    no_event_reason: str = ''
