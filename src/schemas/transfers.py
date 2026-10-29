"""Environment ownership transfer contracts (Phase 7C-3, D035), routed by
src/api/routes/transfers.py. Ids and states only: never credentials or their status."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_user_id: uuid.UUID


class TransferRead(BaseModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    environment_name: str
    organization_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    status: Literal["pending", "accepted", "declined", "cancelled"]
    # The caller's side: incoming (they may accept or decline) or outgoing (they may cancel).
    direction: Literal["incoming", "outgoing"]
    created_at: datetime
    resolved_at: datetime | None
