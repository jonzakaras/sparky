import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import pii
from .agent import Session, SessionPool
from .config import Settings
from .mock import find_transcript, load_transcripts, replay
from .modes import MODES, get_mode

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
settings = Settings()
sessions: dict[str, Session] = {}
pools: dict[str, SessionPool] = {}


def get_pool(mode: str) -> SessionPool:
    """One warm-session pool per arm, so switching arms does not pay a cold start."""
    mode = get_mode(mode).id
    if mode not in pools:
        pools[mode] = SessionPool(factory=lambda: Session(settings, mode))
    return pools[mode]


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.mock_mode:
        get_pool(settings.mode).warm()  # connect in the background; startup is not blocked by a first-time login
    yield
    for pool in pools.values():
        await pool.close()
    for s in sessions.values():
        await s.close()


app = FastAPI(title="AL", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str
    mode: str | None = None
    mock: bool = False


class AnswerIn(BaseModel):
    session_id: str
    question_id: str
    answer: str


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/modes")
async def modes():
    return {
        "default": settings.mode,
        "mock": settings.mock_mode,
        "modes": [{"id": m.id, "label": m.label, "short": m.short} for m in MODES.values()],
    }


@app.post("/warm")
async def warm(mode: str | None = None):
    if not settings.mock_mode:
        get_pool(mode or settings.mode).warm()
    return {"ok": True}


@app.post("/chat")
async def chat(body: ChatIn):
    try:
        mode = get_mode(body.mode or settings.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sid = body.session_id or uuid.uuid4().hex
    mock = body.mock or settings.mock_mode
    transcript = find_transcript(load_transcripts(settings.transcripts_path), mode.id, body.message)
    # Whether the client's session_id still points at a live agent. The UI stores chats in the browser
    # and continues them after a reload; if the server restarted meanwhile it gets a fresh agent under
    # the same id, with no memory of the chat, and this flag lets the UI say so.
    resumed = bool(body.session_id) and sid in sessions

    session = None
    if not mock:
        session = sessions.get(sid)
        if session is not None and session.mode.id != mode.id:
            raise HTTPException(409, "Session belongs to a different mode; start a new conversation")
        if session is None:
            session = sessions[sid] = await get_pool(mode.id).take()

    def sse(events):
        return [{"event": ev["type"], "data": json.dumps(ev)} for ev in events]

    async def stream():
        guard = pii.EventGuard()  # every answer event passes through it, live or replayed
        yield {"event": "session", "data": json.dumps(
            {"type": "session", "session_id": sid, "mode": mode.id, "resumed": resumed})}
        fallback = mock
        if not mock:
            agen = session.ask(body.message).__aiter__()
            first, shown = True, False  # shown: any answer content reached the user
            try:
                while True:
                    try:
                        nxt = agen.__anext__()
                        ev = await (asyncio.wait_for(nxt, settings.live_timeout_s) if first else nxt)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if transcript:
                            fallback = True
                        else:
                            yield {"event": "error", "data": json.dumps(
                                {"type": "error", "message": "Timed out waiting for the model"})}
                            yield {"event": "done", "data": json.dumps({"type": "done"})}
                        break
                    first = False
                    if ev["type"] == "error" and transcript and not shown:
                        fallback = True
                        break
                    if ev["type"] in ("text", "text_delta", "result_table"):
                        shown = True
                    for out in sse(guard(ev)):
                        yield out
            finally:
                await agen.aclose()
            for out in sse(guard.close()):
                yield out
        if fallback:
            if transcript is None:
                msg = "No recorded transcript for this arm and question"
                yield {"event": "error", "data": json.dumps({"type": "error", "message": msg})}
                yield {"event": "done", "data": json.dumps({"type": "done"})}
                return
            async for ev in replay(transcript):
                for out in sse(guard(ev)):
                    yield out
            for out in sse(guard.close()):
                yield out

    return EventSourceResponse(stream())


@app.post("/answer")
async def answer(body: AnswerIn):
    session = sessions.get(body.session_id)
    if session is None or not session.broker.answer(body.question_id, body.answer):
        raise HTTPException(404, "No pending question")
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(WEB_DIR / "assets" / "al.png", media_type="image/png")
