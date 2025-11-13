"""
Database Schemas for Rummy Multiplayer

Each Pydantic model represents a collection in MongoDB. Collection name is the lowercase of the class name
(e.g., Table -> "table").
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Literal
from datetime import datetime

# User/player profile attached to Google account
class Player(BaseModel):
    player_id: str = Field(..., description="Client-generated stable id (e.g., Google sub)")
    name: str
    photo_url: Optional[str] = None
    is_host: bool = False
    is_muted: bool = False
    connected: bool = True
    total_score: int = 0  # accumulated across rounds

class Table(BaseModel):
    code: str
    host_id: str
    max_players: int = Field(..., ge=2, le=6)
    target_score: int = Field(200, ge=200, le=600)
    ace_value: Literal[1, 10] = 10
    status: Literal["lobby", "active", "finished"] = "lobby"
    decks: int = 1
    players: List[Player] = []
    current_round_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Round(BaseModel):
    table_code: str
    round_number: int
    deck: List[str] = []  # remaining deck (face down)
    discard_pile: List[str] = []
    hands: Dict[str, List[str]] = {}  # player_id -> cards in hand
    melds: Dict[str, List[List[str]]] = {}  # player_id -> 4 meld slots
    has_picked: Dict[str, bool] = {}  # player_id -> picked this turn (must discard before declare)
    dropped: Dict[str, bool] = {}
    declared_by: Optional[str] = None
    winner_id: Optional[str] = None
    scores: Dict[str, int] = {}
    turn_order: List[str] = []
    current_turn: int = 0  # index in turn_order
    created_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None

class Message(BaseModel):
    table_code: str
    sender_id: str
    recipient_id: Optional[str] = None  # None => group
    text: str
    type: Literal["chat", "system"] = "chat"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class History(BaseModel):
    table_code: str
    round_id: str
    round_number: int
    scores: Dict[str, int]
    winner_id: Optional[str] = None
    declared_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
