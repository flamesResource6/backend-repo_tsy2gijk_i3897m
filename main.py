import os
import random
import string
from typing import List, Dict, Optional, Literal
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime

from database import db, create_document, get_documents
from schemas import Table, Player, Round, History, Message

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------- Helpers -----------------------
SUITS = ["h", "d", "c", "s"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
JOKERS_PER_DECK = 2  # standard printed jokers; impure uses wilds but we keep simple printed jokers as 0


def generate_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def build_single_deck() -> List[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    deck += ["JOKER"] * JOKERS_PER_DECK
    return deck


def build_decks(n: int) -> List[str]:
    cards = []
    for _ in range(n):
        cards += build_single_deck()
    random.shuffle(cards)
    return cards


def card_value(rank: str, ace_value: int) -> int:
    if rank in ["J", "Q", "K"]:
        return 10
    if rank == "A":
        return ace_value
    try:
        return int(rank)
    except Exception:
        return 0


def split_card(card: str):
    if card == "JOKER":
        return ("JOKER", None)
    # ranks can be 10 which is 2 chars
    rank = card[:-1]
    suit = card[-1]
    return rank, suit


def is_sequence(cards: List[str], pure: bool = True) -> bool:
    if len(cards) < 3:
        return False
    # all same suit (except jokers allowed if not pure)
    ranks, suits = zip(*(split_card(c) for c in cards))
    base_suit = suits[0]
    if pure:
        if any(r == "JOKER" for r in ranks):
            return False
        if len(set(suits)) != 1:
            return False
    else:
        # allow jokers, but non-joker should share same suit
        nz = [s for r, s in zip(ranks, suits) if r != "JOKER"]
        if len(set(nz)) > 1:
            return False
        base_suit = nz[0] if nz else base_suit
    # convert to ordered positions A,2..K (A low only for simplicity)
    order = {r: i for i, r in enumerate(["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"], start=1)}
    nums = []
    jokers = 0
    for r, s in zip(ranks, suits):
        if r == "JOKER":
            jokers += 1
        else:
            if s != base_suit:
                return False
            nums.append(order[r])
    nums.sort()
    # Check if with jokers we can fill gaps to become consecutive
    gaps = 0
    for i in range(1, len(nums)):
        d = nums[i] - nums[i-1]
        if d == 0:
            return False
        gaps += (d - 1)
    return gaps <= jokers


def is_set(cards: List[str]) -> bool:
    if len(cards) < 3:
        return False
    ranks, suits = zip(*(split_card(c) for c in cards))
    if any(r == "JOKER" for r in ranks):
        # allow jokers to substitute
        base = [r for r in ranks if r != "JOKER"]
        return len(set(base)) == 1
    return len(set(ranks)) == 1


def validate_melds(melds: List[List[str]]) -> Dict[str, bool]:
    # Expect 4 meld slots: 3,3,3,4 lengths (but can be empty until arranged)
    result = {"pure_seq": False, "any_seq": False, "all_valid": True}
    for group in melds:
        if not group:
            continue
        if is_sequence(group, pure=True):
            result["pure_seq"] = True
            result["any_seq"] = True
        elif is_sequence(group, pure=False):
            result["any_seq"] = True
        elif is_set(group):
            pass
        else:
            result["all_valid"] = False
    return result


def deadwood_points(hand: List[str], ace_value: int) -> int:
    total = 0
    for c in hand:
        r, _ = split_card(c)
        total += card_value(r, ace_value)
    return min(total, 80)  # common cap in many rummy variants


# ----------------------- Request Models -----------------------
class CreateTableRequest(BaseModel):
    player_id: str
    name: str
    photo_url: Optional[str] = None
    max_players: int = 6
    target_score: int = 200
    ace_value: Literal[1, 10] = 10

class JoinTableRequest(BaseModel):
    player_id: str
    name: str
    photo_url: Optional[str] = None

class StartRoundRequest(BaseModel):
    code: str

class ArrangeRequest(BaseModel):
    code: str
    player_id: str
    hand: List[str]
    melds: List[List[str]]  # length 4: 3,3,3,4 ideally

class PickRequest(BaseModel):
    code: str
    player_id: str
    source: Literal["deck", "discard"]

class DiscardRequest(BaseModel):
    code: str
    player_id: str
    card: str

class DeclareRequest(BaseModel):
    code: str
    player_id: str

class DropRequest(BaseModel):
    code: str
    player_id: str

class LeaveRequest(BaseModel):
    code: str
    player_id: str

class ChatRequest(BaseModel):
    code: str
    sender_id: str
    text: str
    recipient_id: Optional[str] = None

class MuteRequest(BaseModel):
    code: str
    host_id: str
    target_id: str
    mute: bool

# ----------------------- Endpoints -----------------------
@app.get("/")
def root():
    return {"message": "Rummy Multiplayer Backend running"}


@app.post("/table/create")
def create_table(req: CreateTableRequest):
    code = generate_code()
    player = Player(player_id=req.player_id, name=req.name, photo_url=req.photo_url, is_host=True)
    table = Table(
        code=code,
        host_id=req.player_id,
        max_players=max(2, min(6, req.max_players)),
        target_score=max(200, min(600, req.target_score)),
        ace_value=req.ace_value,
        decks=1,
        players=[player],
    ).model_dump()

    # decks based on players will be recalculated on start
    db["table"].insert_one(table)
    return {"code": code, "table": table}


@app.post("/table/{code}/join")
def join_table(code: str, req: JoinTableRequest):
    table = db["table"].find_one({"code": code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    if table["status"] != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")
    if any(p["player_id"] == req.player_id for p in table["players"]):
        return {"table": table}
    if len(table["players"]) >= table["max_players"]:
        raise HTTPException(status_code=400, detail="Table full")
    player = Player(player_id=req.player_id, name=req.name, photo_url=req.photo_url).model_dump()
    db["table"].update_one({"code": code}, {"$push": {"players": player}})
    table = db["table"].find_one({"code": code})
    return {"table": table}


@app.get("/table/{code}")
def get_table(code: str):
    table = db["table"].find_one({"code": code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    round_doc = None
    if table.get("current_round_id"):
        round_doc = db["round"].find_one({"_id": table["current_round_id"]})
    return {"table": table, "round": round_doc}


@app.post("/round/start")
def start_round(req: StartRoundRequest):
    table = db["table"].find_one({"code": req.code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    if table["status"] == "active":
        raise HTTPException(status_code=400, detail="Round already active")
    players = [p for p in table["players"] if p.get("total_score", 0) < table["target_score"]]
    if len(players) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 players to start")

    n = len(players)
    decks = 1 if n == 2 else (2 if n in [3, 4] else 3)
    cards = build_decks(decks)

    # deal 13 each
    hands: Dict[str, List[str]] = {}
    for p in players:
        hand = []
        for _ in range(13):
            hand.append(cards.pop())
        hands[p["player_id"]] = hand

    discard_pile = [cards.pop()]

    melds = {p["player_id"]: [[], [], [], []] for p in players}
    has_picked = {p["player_id"]: False for p in players}
    dropped = {p["player_id"]: False for p in players}

    order = [p["player_id"] for p in players]

    round_doc = Round(
        table_code=req.code,
        round_number=(db["history"].count_documents({"table_code": req.code}) + 1),
        deck=cards,
        discard_pile=discard_pile,
        hands=hands,
        melds=melds,
        has_picked=has_picked,
        dropped=dropped,
        scores={p["player_id"]: 0 for p in players},
        turn_order=order,
        current_turn=0,
    ).model_dump()

    ins = db["round"].insert_one(round_doc)
    db["table"].update_one({"code": req.code}, {"$set": {"status": "active", "current_round_id": ins.inserted_id, "decks": decks}})

    return {"round_id": str(ins.inserted_id)}


@app.post("/round/arrange")
def arrange(req: ArrangeRequest):
    table = db["table"].find_one({"code": req.code})
    if not table or not table.get("current_round_id"):
        raise HTTPException(status_code=404, detail="Round not found")
    rnd = db["round"].find_one({"_id": table["current_round_id"]})
    if req.player_id not in rnd["hands"]:
        raise HTTPException(status_code=400, detail="Player not in round")

    # enforce 4 meld slots structure persists
    melds = req.melds
    if len(melds) != 4:
        raise HTTPException(status_code=400, detail="Melds must have 4 groups")
    # persist
    db["round"].update_one({"_id": rnd["_id"]}, {"$set": {f"hands.{req.player_id}": req.hand, f"melds.{req.player_id}": melds}})
    return {"ok": True}


@app.post("/round/pick")
def pick(req: PickRequest):
    table = db["table"].find_one({"code": req.code})
    if not table or not table.get("current_round_id"):
        raise HTTPException(status_code=404, detail="Round not found")
    rnd = db["round"].find_one({"_id": table["current_round_id"]})
    turn_player = rnd["turn_order"][rnd["current_turn"]]
    if turn_player != req.player_id:
        raise HTTPException(status_code=400, detail="Not your turn")
    if rnd["has_picked"].get(req.player_id):
        raise HTTPException(status_code=400, detail="Already picked. Discard first.")

    if req.source == "deck":
        if not rnd["deck"]:
            raise HTTPException(status_code=400, detail="Deck empty")
        card = rnd["deck"].pop()
    else:
        if not rnd["discard_pile"]:
            raise HTTPException(status_code=400, detail="Discard empty")
        card = rnd["discard_pile"].pop()

    rnd["hands"][req.player_id].append(card)
    rnd["has_picked"][req.player_id] = True
    db["round"].update_one({"_id": rnd["_id"]}, {"$set": {"deck": rnd["deck"], "discard_pile": rnd["discard_pile"], f"hands.{req.player_id}": rnd["hands"][req.player_id], f"has_picked.{req.player_id}": True}})
    return {"card": card}


@app.post("/round/discard")
def discard(req: DiscardRequest):
    table = db["table"].find_one({"code": req.code})
    if not table or not table.get("current_round_id"):
        raise HTTPException(status_code=404, detail="Round not found")
    rnd = db["round"].find_one({"_id": table["current_round_id"]})
    turn_player = rnd["turn_order"][rnd["current_turn"]]
    if turn_player != req.player_id:
        raise HTTPException(status_code=400, detail="Not your turn")
    if not rnd["has_picked"].get(req.player_id):
        raise HTTPException(status_code=400, detail="Pick before discard")

    if req.card not in rnd["hands"][req.player_id]:
        raise HTTPException(status_code=400, detail="Card not in hand")

    rnd["hands"][req.player_id].remove(req.card)
    rnd["discard_pile"].append(req.card)
    rnd["has_picked"][req.player_id] = False
    # advance turn skipping dropped players
    next_turn = (rnd["current_turn"] + 1) % len(rnd["turn_order"])
    for _ in range(len(rnd["turn_order"])):
        pid = rnd["turn_order"][next_turn]
        if not rnd["dropped"].get(pid, False):
            break
        next_turn = (next_turn + 1) % len(rnd["turn_order"])
    rnd["current_turn"] = next_turn

    db["round"].update_one({"_id": rnd["_id"]}, {"$set": {"discard_pile": rnd["discard_pile"], f"hands.{req.player_id}": rnd["hands"][req.player_id], f"has_picked.{req.player_id}": False, "current_turn": rnd["current_turn"]}})
    return {"ok": True}


@app.post("/round/declare")
def declare_round(req: DeclareRequest):
    table = db["table"].find_one({"code": req.code})
    if not table or not table.get("current_round_id"):
        raise HTTPException(status_code=404, detail="Round not found")
    rnd = db["round"].find_one({"_id": table["current_round_id"]})

    # cannot declare if player has picked and not discarded yet
    if rnd["has_picked"].get(req.player_id):
        raise HTTPException(status_code=400, detail="You must discard after picking before declaring")

    melds = rnd["melds"].get(req.player_id, [[], [], [], []])
    flags = validate_melds(melds)

    if not (flags["pure_seq"] and flags["any_seq"] and flags["all_valid"]):
        # invalid declaration: +80, others 0
        scores = {pid: 0 for pid in rnd["turn_order"]}
        scores[req.player_id] = 80
        # add 20 for players who dropped
        for pid, dropped in rnd.get("dropped", {}).items():
            if dropped and pid != req.player_id:
                scores[pid] = scores.get(pid, 0) + 20
        return finalize_round(table, rnd, scores, winner=None, declared_by=req.player_id)

    # valid declaration: winner 0, others deadwood of remaining hand
    scores = {}
    ace_val = table.get("ace_value", 10)
    for pid in rnd["turn_order"]:
        if pid == req.player_id:
            scores[pid] = 0
        else:
            # dropped players get fixed 20 regardless of hand
            if rnd.get("dropped", {}).get(pid):
                scores[pid] = 20
            else:
                points = deadwood_points(rnd["hands"][pid], ace_val)
                scores[pid] = points
    return finalize_round(table, rnd, scores, winner=req.player_id, declared_by=req.player_id)


def finalize_round(table, rnd, scores: Dict[str, int], winner: Optional[str], declared_by: Optional[str]):
    # update player totals and elimination
    for p in table["players"]:
        pid = p["player_id"]
        add = scores.get(pid, 0)
        p["total_score"] = p.get("total_score", 0) + add
    db["table"].update_one({"code": table["code"]}, {"$set": {"players": table["players"], "status": "lobby", "current_round_id": None}})

    hist = History(
        table_code=table["code"],
        round_id=str(rnd["_id"]),
        round_number=rnd["round_number"],
        scores=scores,
        winner_id=winner,
        declared_by=declared_by,
    ).model_dump()
    db["history"].insert_one(hist)

    # mark round ended
    db["round"].update_one({"_id": rnd["_id"]}, {"$set": {"ended_at": datetime.utcnow(), "scores": scores, "winner_id": winner, "declared_by": declared_by}})

    # check disqualifications
    target = table.get("target_score", 200)
    eliminated = [p["player_id"] for p in table["players"] if p.get("total_score", 0) >= target]

    return {"scores": scores, "winner": winner, "eliminated": eliminated}


@app.post("/round/drop")
def drop(req: DropRequest):
    table = db["table"].find_one({"code": req.code})
    if not table or not table.get("current_round_id"):
        raise HTTPException(status_code=404, detail="Round not found")
    rnd = db["round"].find_one({"_id": table["current_round_id"]})

    # only if more than 2 players and before first pick of that player
    if len(rnd["turn_order"]) <= 2:
        raise HTTPException(status_code=400, detail="Drop available only if players > 2")
    if rnd["has_picked"].get(req.player_id):
        raise HTTPException(status_code=400, detail="Cannot drop after picking")

    rnd["dropped"][req.player_id] = True
    db["round"].update_one({"_id": rnd["_id"]}, {"$set": {f"dropped.{req.player_id}": True}})

    return {"ok": True}


@app.post("/player/leave")
def leave(req: LeaveRequest):
    table = db["table"].find_one({"code": req.code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")

    # remove from table players if in lobby
    if table.get("status") == "lobby":
        db["table"].update_one({"code": req.code}, {"$pull": {"players": {"player_id": req.player_id}}})
        return {"ok": True}

    # in active round: apply 60 penalty and remove from turn rotation
    if table.get("current_round_id"):
        rnd = db["round"].find_one({"_id": table["current_round_id"]})
        if req.player_id in rnd["turn_order"]:
            rnd["turn_order"].remove(req.player_id)
            rnd["dropped"][req.player_id] = True
            db["round"].update_one({"_id": rnd["_id"]}, {"$set": {"turn_order": rnd["turn_order"], f"dropped.{req.player_id}": True}})
        # add 60 to player's total immediately and record in current round scores state
        for p in table["players"]:
            if p["player_id"] == req.player_id:
                p["total_score"] = p.get("total_score", 0) + 60
        db["table"].update_one({"code": req.code}, {"$set": {"players": table["players"]}})
    return {"ok": True}


@app.post("/chat/send")
def send_chat(req: ChatRequest):
    table = db["table"].find_one({"code": req.code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")

    # check mute
    target = None
    if req.recipient_id:
        target = next((p for p in table["players"] if p["player_id"] == req.recipient_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Recipient not found")
    sender = next((p for p in table["players"] if p["player_id"] == req.sender_id), None)
    if not sender:
        raise HTTPException(status_code=404, detail="Sender not found")
    if sender.get("is_muted"):
        raise HTTPException(status_code=403, detail="Sender is muted")

    msg = Message(table_code=req.code, sender_id=req.sender_id, recipient_id=req.recipient_id, text=req.text).model_dump()
    db["message"].insert_one(msg)
    return {"ok": True}


@app.post("/host/mute")
def mute_player(req: MuteRequest):
    table = db["table"].find_one({"code": req.code})
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    if req.host_id != table["host_id"]:
        raise HTTPException(status_code=403, detail="Only host can mute")

    for p in table["players"]:
        if p["player_id"] == req.target_id:
            p["is_muted"] = req.mute
    db["table"].update_one({"code": req.code}, {"$set": {"players": table["players"]}})
    return {"ok": True}


@app.get("/history/{code}")
def history(code: str):
    docs = list(db["history"].find({"table_code": code}).sort("created_at", 1))
    return {"history": docs}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response


# Minimal built-in UI while the frontend sandbox is unavailable
@app.get("/ui", response_class=HTMLResponse)
def minimal_ui():
    return """
<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width, initial-scale=1'/>
  <title>Rummy (Fallback UI)</title>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1020;color:#e8ecf3;margin:0}
    .container{max-width:900px;margin:0 auto;padding:16px}
    .card{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:14px;margin:12px 0}
    input,button{padding:8px 10px;border-radius:8px;border:1px solid #344;outline:none;background:#141a33;color:#e8ecf3}
    button{cursor:pointer}button:hover{background:#1b2344}
    .row{display:flex;gap:8px;flex-wrap:wrap}
    .badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#263255;border:1px solid #344;color:#cdd7ff;font-size:12px}
    .scroll{max-height:240px;overflow:auto}
  </style>
</head>
<body>
  <div class='container'>
    <h1>Rummy (Fallback UI)</h1>
    <div class='card'>
      <div class='row'>
        <input id='name' placeholder='Your name'/>
        <button onclick='createTable()'>Create Table</button>
      </div>
      <div class='row' style='margin-top:8px;'>
        <input id='code' placeholder='6-digit code'/>
        <button onclick='joinTable()'>Join</button>
        <button onclick='startRound()'>Start</button>
      </div>
      <div id='status' style='margin-top:8px' class='badge'></div>
    </div>

    <div class='card'>
      <h3>Table</h3>
      <pre id='table' class='scroll'></pre>
    </div>

    <div class='card'>
      <h3>Chat</h3>
      <div class='row'>
        <input id='chat' placeholder='Type a message' style='flex:1'/>
        <button onclick='sendChat()'>Send</button>
      </div>
      <pre id='history' class='scroll'></pre>
    </div>
  </div>
  <script>
    const pidKey = 'pid';
    const pid = localStorage.getItem(pidKey) || (()=>{const v=Math.random().toString(36).slice(2);localStorage.setItem(pidKey,v);return v})()
    const backend = ''
    let pollTimer;

    function setStatus(t){document.getElementById('status').textContent=t}
    function getCode(){return document.getElementById('code').value.trim()}
    function getName(){return document.getElementById('name').value.trim()}

    async function createTable(){
      const name = getName(); if(!name){setStatus('Enter name first');return}
      const res = await fetch(`${backend}/table/create`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_id:pid,name, max_players:4, target_score:200, ace_value:10})})
      const json = await res.json(); document.getElementById('code').value=json.code; setStatus('Table created. Code '+json.code)
      startPolling()
    }

    async function joinTable(){
      const code=getCode(), name=getName(); if(!code||!name){setStatus('Enter code and name');return}
      const res = await fetch(`${backend}/table/${code}/join`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_id:pid,name})})
      const json = await res.json(); setStatus('Joined table'); startPolling()
    }

    async function startRound(){
      const code=getCode(); if(!code){return}
      await fetch(`${backend}/round/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})
    }

    async function sendChat(){
      const code=getCode(); const text=document.getElementById('chat').value; if(!text||!code) return
      await fetch(`${backend}/chat/send`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code, sender_id: pid, text})})
      document.getElementById('chat').value=''
    }

    async function poll(){
      const code=getCode(); if(!code) return
      try {
        const t = await (await fetch(`${backend}/table/${code}`)).json()
        document.getElementById('table').textContent = JSON.stringify(t,null,2)
        const h = await (await fetch(`${backend}/history/${code}`)).json()
        document.getElementById('history').textContent = JSON.stringify(h,null,2)
      } catch(e) {}
    }

    function startPolling(){
      if (pollTimer) clearInterval(pollTimer)
      pollTimer = setInterval(poll, 400)
    }
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
