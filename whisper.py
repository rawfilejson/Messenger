import asyncio
import base64
import hashlib
import os
import random
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from pywebio import start_server
from pywebio.input import input, input_group, actions, PASSWORD
from pywebio.output import (
    put_html, put_row, put_scrollable, output, toast,
)
from pywebio.session import defer_call, run_async, set_env


DB_PATH       = "whisper.db"
HISTORY_LIMIT = 200          # messages kept in memory per room
LOAD_ON_JOIN  = 30           # how much history a joining user sees
RATE_WINDOW   = 4.0          # seconds
RATE_LIMIT    = 5            # max messages per RATE_WINDOW
MAX_MSG_LEN   = 800
DEFAULT_ROOM  = "lobby"

# Change this in production. Same salt + same password = same key, so if you
# rotate the salt nobody can read old encrypted history. Trade-off.
APP_SALT = b"whisper-v1-change-me-in-prod"


# rooms[name] = {"key": fernet_key|None, "users": set(), "msgs": deque, "private": bool}
rooms       = {}
# rate limit log per nick
rate_log    = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
# pleasant-ish palette for nicknames
PALETTE = [
    "#ff6b6b", "#4ecdc4", "#ffd166", "#a8e6cf", "#ff8b94",
    "#c9b1ff", "#7ed6df", "#f9ca24", "#badc58", "#ff9ff3",
    "#74b9ff", "#fd79a8", "#55efc4", "#fab1a0", "#81ecec",
]

# Crypto
def derive_key(password: str) -> bytes:
    """PBKDF2-SHA256 -> 32 bytes -> urlsafe b64 (Fernet format)."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=APP_SALT,
        iterations=200_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt(text, key):
    if not key:
        return text
    return Fernet(key).encrypt(text.encode()).decode()


def decrypt(token, key):
    if not key:
        return token
    try:
        return Fernet(key).decrypt(token.encode()).decode()
    except InvalidToken:
        return "[!] cannot decrypt"

# DB
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        room      TEXT NOT NULL,
        sender    TEXT NOT NULL,
        body      TEXT NOT NULL,
        encrypted INTEGER NOT NULL DEFAULT 0,
        ts        REAL NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_room_id ON messages(room, id)")
    c.commit()
    return c


def db_save(room, sender, body, encrypted):
    c = _conn()
    c.execute(
        "INSERT INTO messages(room,sender,body,encrypted,ts) VALUES (?,?,?,?,?)",
        (room, sender, body, int(encrypted), time.time()),
    )
    c.commit()
    c.close()


def db_recent(room, limit):
    c = _conn()
    rows = c.execute(
        "SELECT sender, body, encrypted FROM messages "
        "WHERE room=? ORDER BY id DESC LIMIT ?",
        (room, limit),
    ).fetchall()
    c.close()
    return list(reversed(rows))


# helpers
def color_for(name):
    # stable color per nick - md5 because it's fine for this
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]


def now_hm():
    return datetime.now().strftime("%H:%M")


def esc(s):
    # very small html escape - we render via put_html for the color/styling
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def render(box, sender, body, kind="msg"):
    """Push a single line into a chat box. kind: msg | system | dm | me"""
    ts = now_hm()
    if kind == "system":
        box.append(put_html(
            f'<div style="opacity:.6;font-size:13px;margin:3px 0;">'
            f'<span style="color:#888">[{ts}]</span> · {esc(body)}</div>'
        ))
    elif kind == "me":
        # /me actions get italicized
        box.append(put_html(
            f'<div style="margin:3px 0;font-style:italic;opacity:.85;">'
            f'<span style="color:#888;font-size:12px;">[{ts}]</span> '
            f'* <b style="color:{color_for(sender)}">{esc(sender)}</b> {esc(body)}</div>'
        ))
    elif kind == "dm":
        # body comes in as "to_nick: body" or "from_nick: body" - sender holds the label
        box.append(put_html(
            f'<div style="margin:3px 0;background:rgba(255,200,0,.08);'
            f'padding:4px 8px;border-left:3px solid #ffb84d;border-radius:4px;">'
            f'<span style="color:#888;font-size:12px;">[{ts}]</span> '
            f'💌 <b style="color:{color_for(sender)}">{esc(sender)}</b> '
            f'<span>{esc(body)}</span></div>'
        ))
    else:
        box.append(put_html(
            f'<div style="margin:3px 0;line-height:1.45;">'
            f'<span style="color:#888;font-size:12px;">[{ts}]</span> '
            f'<b style="color:{color_for(sender)}">{esc(sender)}</b>: '
            f'<span>{esc(body)}</span></div>'
        ))


def rate_hit(nick):
    """Returns True if the user just got rate-limited."""
    now = time.time()
    log = rate_log[nick]
    while log and now - log[0] > RATE_WINDOW:
        log.popleft()
    if len(log) >= RATE_LIMIT:
        return True
    log.append(now)
    return False


def get_or_make_room(name, password):
    if name not in rooms:
        rooms[name] = {
            "key": derive_key(password) if password else None,
            "private": bool(password),
            "users": set(),
            "msgs": deque(maxlen=HISTORY_LIMIT),
        }
    return rooms[name]


def post(room_name, sender, body, *, kind="msg", target=None):
    """Append a message to a room's in-memory log + persist if real chat."""
    room = rooms[room_name]
    stored = encrypt(body, room["key"]) if kind == "msg" and room["key"] else body
    room["msgs"].append({
        "sender": sender,
        "body":   body,
        "kind":   kind,
        "target": target,        # only set for DMs
        "ts":     time.time(),
    })
    # only persist real messages, not /me actions / system noise / DMs
    if kind == "msg":
        db_save(room_name, sender, stored, bool(room["key"]))


# commands
HELP_TEXT = (
    "commands: /help · /me <action> · /users · /rooms · "
    "/pm <user> <msg> · /roll [N] · /flip · /clear · /quit"
)


def handle_command(text, *, nick, room_name, box):
    """Returns True if the command was handled (skip normal send)."""
    parts = text.strip().split(" ", 2)
    cmd = parts[0].lower()
    args = parts[1:]
    room = rooms[room_name]

    if cmd == "/help":
        render(box, None, HELP_TEXT, kind="system")

    elif cmd == "/me":
        if not args:
            render(box, None, "usage: /me <action>", kind="system")
            return True
        post(room_name, nick, " ".join(args), kind="me")
        render(box, nick, " ".join(args), kind="me")  # local echo

    elif cmd == "/users":
        users = ", ".join(sorted(room["users"])) or "(nobody?)"
        render(box, None, f"online in #{room_name}: {users}", kind="system")

    elif cmd == "/rooms":
        pub = [r for r, i in rooms.items() if not i["private"]]
        prv = [r for r, i in rooms.items() if i["private"]]
        msg = (
            f"public: {', '.join(pub) or '-'}   "
            f"private: {', '.join('🔒'+r for r in prv) or '-'}"
        )
        render(box, None, msg, kind="system")

    elif cmd == "/pm":
        if len(args) < 2:
            render(box, None, "usage: /pm <user> <message>", kind="system")
            return True
        target, body = args[0], args[1]
        # is the target in any room we know about?
        if not any(target in r["users"] for r in rooms.values()):
            render(box, None, f"user '{target}' is not online", kind="system")
            return True
        # we route DMs through the sender's current room - both sides will see it
        # via the refresh loop's target check below
        post(room_name, nick, body, kind="dm", target=target)
        render(box, f"you → {target}", body, kind="dm")

    elif cmd == "/roll":
        n = 100
        if args:
            try:
                n = max(1, min(int(args[0]), 1_000_000))
            except ValueError:
                pass
        result = random.randint(1, n)
        line = f"{nick} rolled {result} (d{n})"
        post(room_name, nick, f"🎲 {line}", kind="me")
        render(box, nick, f"🎲 {line}", kind="me")

    elif cmd == "/flip":
        side = random.choice(["heads", "tails"])
        post(room_name, nick, f"🪙 flipped {side}", kind="me")
        render(box, nick, f"🪙 flipped {side}", kind="me")

    elif cmd == "/clear":
        box.reset()
        render(box, None, "screen cleared (your view only)", kind="system")

    elif cmd in ("/quit", "/leave", "/exit"):
        return "QUIT"

    else:
        render(box, None, f"unknown command: {cmd}", kind="system")

    return True


# background tasks per session
async def refresh_messages(nick, room_name, box):
    """Poll the room log and render anything new that's relevant to us."""
    room = rooms[room_name]
    last = len(room["msgs"])
    while True:
        await asyncio.sleep(0.4)
        snapshot = list(room["msgs"])
        for m in snapshot[last:]:
            # don't re-render what the sender just typed locally
            if m["sender"] == nick:
                continue
            # DMs - only render if we're the target
            if m["kind"] == "dm":
                if m["target"] == nick:
                    render(box, f"{m['sender']} → you", m["body"], kind="dm")
                continue
            render(box, m["sender"], m["body"], kind=m["kind"])
        last = len(snapshot)


async def refresh_sidebar(sidebar, room_name):
    """Re-render the user list when it changes. Cheap signature check."""
    last_sig = None
    while True:
        await asyncio.sleep(1.5)
        room = rooms[room_name]
        users = sorted(room["users"])
        sig = (tuple(users), room["private"])
        if sig == last_sig:
            continue
        last_sig = sig
        sidebar.reset()
        sidebar.append(put_html(_sidebar_html(room_name, users, room["private"])))


def _sidebar_html(room_name, users, private):
    tag = "🔒 encrypted" if private else "🌐 public"
    user_rows = "".join(
        f'<div style="font-size:13px;color:{color_for(u)};margin:2px 0;">● {esc(u)}</div>'
        for u in users
    )
    return (
        f'<div style="padding:10px 12px;background:rgba(0,0,0,.04);'
        f'border-radius:8px;font-family:system-ui,sans-serif;">'
        f'<div style="font-weight:600;font-size:15px;">#{esc(room_name)}</div>'
        f'<div style="opacity:.7;font-size:12px;margin-bottom:10px;">{tag}</div>'
        f'<div style="font-weight:600;font-size:12px;letter-spacing:.5px;'
        f'opacity:.75;margin-bottom:4px;">ONLINE ({len(users)})</div>'
        f'{user_rows}'
        f'</div>'
    )


# main session
async def main():
    set_env(title="Whisper", output_max_width="980px")

    put_html("""
    <div style="text-align:center;padding:8px 0 14px;
                font-family:system-ui,sans-serif;">
      <div style="font-size:28px;font-weight:700;letter-spacing:-0.5px;">
        🤫 Whisper
      </div>
      <div style="opacity:.65;font-size:13px;">
        encrypted chat in a single Python file ·
        type <code>/help</code> once you're in
      </div>
    </div>
    """)

    # login
    creds = await input_group("Join", [
        input("Nickname", name="nick", required=True,
              validate=lambda n: "name is taken or reserved"
                                 if n.strip() in _all_users() or n.strip().lower() == "system"
                                 else None),
        input("Room", name="room", value=DEFAULT_ROOM,
              help_text="public if no password, encrypted if you set one"),
        input("Room password (optional)", name="pw", type=PASSWORD),
    ])

    nick      = creds["nick"].strip()[:20]
    room_name = (creds["room"] or DEFAULT_ROOM).strip()[:30]
    password  = creds["pw"] or ""

    # if the room already exists with a password, the user must supply the right one
    if room_name in rooms and rooms[room_name]["private"]:
        if not password or derive_key(password) != rooms[room_name]["key"]:
            put_html('<div style="color:#e74c3c;padding:12px;">'
                     'wrong password for this room.</div>')
            return
    # and if the room exists as public, refuse a password attempt (don't let
    # people accidentally split-brain a room)
    if room_name in rooms and not rooms[room_name]["private"] and password:
        put_html('<div style="color:#e67e22;padding:12px;">'
                 'that room is public - leave password blank.</div>')
        return

    room = get_or_make_room(room_name, password or None)
    room["users"].add(nick)

    # ui layout
    msg_box = output()
    sidebar = output()
    sidebar.append(put_html(_sidebar_html(room_name, sorted(room["users"]), room["private"])))

    put_row(
        [put_scrollable(msg_box, height=460, keep_bottom=True), None, sidebar],
        size="3fr 12px 1fr",
    )

    # show recent history (decrypted on the fly)
    history = db_recent(room_name, LOAD_ON_JOIN)
    if history:
        render(msg_box, None, f"-- showing last {len(history)} messages --", kind="system")
        for sender, body, enc in history:
            plain = decrypt(body, room["key"]) if enc else body
            render(msg_box, sender, plain)

    render(msg_box, None, f"you joined #{room_name} as {nick}", kind="system")
    post(room_name, None, f"{nick} joined", kind="system")

    # start the two background tasks
    t_msgs = run_async(refresh_messages(nick, room_name, msg_box))
    t_side = run_async(refresh_sidebar(sidebar, room_name))

    @defer_call
    def cleanup():
        room["users"].discard(nick)
        rate_log.pop(nick, None)
        post(room_name, None, f"{nick} left", kind="system")

    # chat loop
    while True:
        data = await input_group("", [
            input(placeholder="message... or /help", name="msg"),
            actions(name="act", buttons=[
                {"label": "send",  "value": "send"},
                {"label": "leave", "value": "leave", "type": "cancel"},
            ]),
        ], validate=lambda d: ("msg", "type something") if d["act"] == "send" and not d["msg"] else None)

        if data is None or data["act"] == "leave":
            break

        text = data["msg"].strip()
        if not text:
            continue
        if len(text) > MAX_MSG_LEN:
            toast(f"too long (max {MAX_MSG_LEN} chars)", color="warn")
            continue
        if rate_hit(nick):
            toast("slow down 🐢", color="warn")
            continue

        # slash commands
        if text.startswith("/"):
            result = handle_command(text, nick=nick, room_name=room_name, box=msg_box)
            if result == "QUIT":
                break
            continue

        # normal message - post to log + render locally
        post(room_name, nick, text, kind="msg")
        render(msg_box, nick, text)

    # teardown
    t_msgs.close()
    t_side.close()
    toast("you left the chat")


def _all_users():
    """Flat set of every nick currently in any room."""
    out = set()
    for r in rooms.values():
        out |= r["users"]
    return out


if __name__ == "__main__":
    # cdn=False so it works offline / on a closed network.
    # debug=True is fine for a hobby project; turn it off for real deployments.
    print("Whisper running -> http://localhost:8080")
    start_server(main, debug=True, port=8080, cdn=False)
