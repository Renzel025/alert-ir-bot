"""The Lark Open Platform calls this bot needs: tenant token, post / reply, list
messages, decrypt events. Trimmed version of lark-ops-ai/p0_logic/lark_client.py."""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import logging
import threading
import time
from typing import Any, Dict, List
from urllib.parse import quote

import requests

from . import config

log = logging.getLogger("alert-ir")


class LarkError(RuntimeError):
    pass


_TOKEN: Dict[str, Any] = {"value": "", "exp": 0.0}
_TOKEN_LOCK = threading.Lock()
_dry_ids = itertools.count(1)


def get_tenant_token() -> str:
    now = time.time()
    with _TOKEN_LOCK:
        if _TOKEN["value"] and now < _TOKEN["exp"]:
            return str(_TOKEN["value"])
    r = requests.post(
        f"{config.LARK_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": config.LARK_APP_ID, "app_secret": config.LARK_APP_SECRET},
        timeout=config.HTTP_TIMEOUT,
    )
    data = r.json() if r.text else {}
    if data.get("code") != 0:
        raise LarkError(f"tenant token: code={data.get('code')} msg={data.get('msg')}")
    with _TOKEN_LOCK:
        _TOKEN["value"] = data["tenant_access_token"]
        _TOKEN["exp"] = now + int(data.get("expire") or 7200) - 120  # refresh 2 min early
    return str(_TOKEN["value"])


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {get_tenant_token()}"}


def _check(r: requests.Response, what: str) -> Dict[str, Any]:
    # Lark answers HTTP 200 with code != 0 for most errors (e.g. bot not in the group).
    try:
        data = r.json()
    except ValueError:
        raise LarkError(f"{what}: HTTP {r.status_code} non-JSON body: {r.text[:200]}")
    if r.status_code != 200 or data.get("code") != 0:
        raise LarkError(f"{what}: HTTP {r.status_code} code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


def mention(open_id: str, name: str = "") -> str:
    """@mention markup for text messages."""
    return f'<at user_id="{open_id}">{name or "check person"}</at>'


def post_card(chat_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """New top-level interactive card. Returns Lark's message data (message_id, ...)."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] post_card chat=%s title=%r", chat_id, ((card.get("header") or {}).get("title") or {}).get("content"))
        return {"message_id": f"om_dry_{next(_dry_ids)}"}
    payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    r = requests.post(
        f"{config.LARK_BASE}/im/v1/messages?receive_id_type=chat_id",
        headers=_headers(), json=payload, timeout=config.HTTP_TIMEOUT,
    )
    return _check(r, "post_card")


def update_card(message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """Repaint a card this app sent (PATCH). Only works on interactive messages from this app."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] update_card id=%s title=%r", message_id, ((card.get("header") or {}).get("title") or {}).get("content"))
        return {}
    r = requests.patch(
        f"{config.LARK_BASE}/im/v1/messages/{quote(message_id, safe='')}",
        headers=_headers(), json={"content": json.dumps(card, ensure_ascii=False)}, timeout=config.HTTP_TIMEOUT,
    )
    return _check(r, "update_card")


def post_text(chat_id: str, text: str) -> Dict[str, Any]:
    """New top-level message. Returns Lark's message data (message_id, thread_id, ...)."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] post_text chat=%s text=%r", chat_id, text)
        return {"message_id": f"om_dry_{next(_dry_ids)}"}
    payload = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    r = requests.post(
        f"{config.LARK_BASE}/im/v1/messages?receive_id_type=chat_id",
        headers=_headers(), json=payload, timeout=config.HTTP_TIMEOUT,
    )
    return _check(r, "post_text")


def post_rich(chat_id: str, title: str, lines: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """New top-level rich-text ("post") message. `lines` = Lark post paragraphs, each a list of
    elements like {"tag": "text", "text": "..."} or {"tag": "a", "text": "...", "href": "..."}."""
    content = {"en_us": {"title": title, "content": lines}}
    if config.DRY_RUN:
        log.info("[DRY_RUN] post_rich chat=%s title=%r", chat_id, title)
        return {"message_id": f"om_dry_{next(_dry_ids)}"}
    payload = {"receive_id": chat_id, "msg_type": "post", "content": json.dumps(content, ensure_ascii=False)}
    r = requests.post(
        f"{config.LARK_BASE}/im/v1/messages?receive_id_type=chat_id",
        headers=_headers(), json=payload, timeout=config.HTTP_TIMEOUT,
    )
    return _check(r, "post_rich")


def reply_text(message_id: str, text: str, in_thread: bool = True) -> Dict[str, Any]:
    """Reply under a message. in_thread=True opens/uses the topic thread on it."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] reply_text to=%s text=%r", message_id, text)
        return {"message_id": f"om_dry_{next(_dry_ids)}"}
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "reply_in_thread": bool(in_thread),
    }
    r = requests.post(
        f"{config.LARK_BASE}/im/v1/messages/{quote(message_id, safe='')}/reply",
        headers=_headers(), json=payload, timeout=config.HTTP_TIMEOUT,
    )
    return _check(r, "reply_text")


def create_meeting(topic: str, owner_open_id: str, hours: int = 24) -> Dict[str, Any]:
    """Reserve a Lark video meeting and return its join link ({"url", "meeting_no", "id", ...}).
    POST /vc/v1/reserves/apply, scope `vc:reserve`. With a tenant token Lark requires an owner_id,
    so the person who clicked "Create meeting" owns it. The reservation expires after `hours`."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] create_meeting topic=%r owner=%s", topic, owner_open_id)
        return {"url": "https://vc.larksuite.com/j/000000000", "meeting_no": "000000000"}
    payload = {
        "end_time": str(int(time.time()) + hours * 3600),
        "owner_id": owner_open_id,
        # meeting_initial_type 1 = multi-person meeting (same as the P0 bot); without it Lark does not
        # attach the live meeting preview (participants, timer, Ended) to the link message
        "meeting_settings": {"topic": topic[:100], "meeting_initial_type": 1},
    }
    r = requests.post(
        f"{config.LARK_BASE}/vc/v1/reserves/apply?user_id_type=open_id",
        headers=_headers(), json=payload, timeout=config.HTTP_TIMEOUT,
    )
    reserve = _check(r, "create_meeting").get("reserve") or {}
    if not reserve.get("url"):
        raise LarkError("create_meeting: Lark returned no meeting url")
    return reserve


def upload_video(path: str) -> str:
    """POST /im/v1/files (file_type mp4) → file_key. Lark caps uploads at 30 MB."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] upload_video %s", path)
        return "file_dry"
    with open(path, "rb") as f:
        r = requests.post(
            f"{config.LARK_BASE}/im/v1/files", headers=_headers(),
            data={"file_type": "mp4", "file_name": path.rsplit("/", 1)[-1]},
            files={"file": (path.rsplit("/", 1)[-1], f, "video/mp4")}, timeout=120,
        )
    return str(_check(r, "upload_video").get("file_key") or "")


def send_media(chat_id: str, file_key: str, reply_to: str = "") -> Dict[str, Any]:
    """A video message (msg_type media) in a chat, or as a reply under a message."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] send_media chat=%s file=%s", chat_id, file_key)
        return {"message_id": f"om_dry_{next(_dry_ids)}"}
    content = json.dumps({"file_key": file_key})
    if reply_to:
        r = requests.post(f"{config.LARK_BASE}/im/v1/messages/{quote(reply_to, safe='')}/reply", headers=_headers(),
                          json={"msg_type": "media", "content": content}, timeout=config.HTTP_TIMEOUT)
    else:
        r = requests.post(f"{config.LARK_BASE}/im/v1/messages?receive_id_type=chat_id", headers=_headers(),
                          json={"receive_id": chat_id, "msg_type": "media", "content": content}, timeout=config.HTTP_TIMEOUT)
    return _check(r, "send_media")


def delete_meeting(reserve_id: str) -> None:
    """Cancel a reservation made by create_meeting (DELETE /vc/v1/reserves/{id}); the join link stops
    working. Called when the incident resolves."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] delete_meeting %s", reserve_id)
        return
    r = requests.delete(f"{config.LARK_BASE}/vc/v1/reserves/{quote(reserve_id, safe='')}",
                        headers=_headers(), timeout=config.HTTP_TIMEOUT)
    _check(r, "delete_meeting")


def list_messages(chat_id: str, page_size: int = 20) -> List[Dict[str, Any]]:
    """Newest messages in a chat (polling mode). Items use msg_type / body.content / sender.id."""
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "sort_type": "ByCreateTimeDesc",
        "page_size": page_size,
    }
    r = requests.get(f"{config.LARK_BASE}/im/v1/messages", headers=_headers(), params=params, timeout=config.HTTP_TIMEOUT)
    return list(_check(r, "list_messages").get("items") or [])


def decrypt_event(encrypted: str) -> Dict[str, Any]:
    """Lark event encryption: AES-256-CBC, key = sha256(encrypt_key), IV = first 16 bytes."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # only when LARK_ENCRYPT_KEY is set

    key = hashlib.sha256(config.LARK_ENCRYPT_KEY.encode("utf-8")).digest()
    raw = base64.b64decode(encrypted)
    dec = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    plain = dec.update(raw[16:]) + dec.finalize()
    plain = plain[: -plain[-1]]  # strip PKCS7 padding
    return json.loads(plain.decode("utf-8"))
