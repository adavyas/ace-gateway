import threading

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, get_current_user


router = APIRouter()
_notes_store_lock = threading.Lock()
_notes_store: list[dict[str, str]] = []


class MeResponse(BaseModel):
    user_id: str
    email: str | None = None


class NoteCreateRequest(BaseModel):
    body: str = Field(min_length=1)


class NoteCreateResponse(BaseModel):
    owner_id: str
    body: str


@router.get("/me", response_model=MeResponse)
def get_me(current_user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    return MeResponse(user_id=current_user.user_id, email=current_user.email)


@router.post("/notes", response_model=NoteCreateResponse)
def create_note(
    payload: NoteCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NoteCreateResponse:
    note = {"owner_id": current_user.user_id, "body": payload.body}
    with _notes_store_lock:
        _notes_store.append(note)
    return NoteCreateResponse(**note)

