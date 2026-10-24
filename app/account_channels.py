"""Account-channel aggregation endpoint powering the overview "free-tier channels" panel."""
from fastapi import APIRouter, Request

from .admin_api import require_admin
from .database import query
from .gateway import channel_breaker_state

router = APIRouter(prefix="/api/account-channels")

FAMILIES = {
    "opencode_free": {"name": "OpenCode \u514d\u8d39\u6c60", "vendor": "opencode.ai", "login": "none", "desc": "\u533f\u540d\u514d\u8d39\u989d\u5ea6\uff0c\u65e0\u9700\u8d26\u53f7\uff1b\u6309\u51fa\u53e3 IP \u8ba1\u91cf\u3002"},
    "lobsterai": {"name": "LobsterAI (\u6709\u9053)", "vendor": "youdao.com", "login": "oauth", "desc": "\u6709\u9053\u8d26\u53f7 OAuth \u767b\u5f55\uff0c\u4e2a\u4eba\u514d\u8d39\u989d\u5ea6\u3002"},
}


@router.get("")
def list_account_channels(request: Request):
    require_admin(request)
    rows = query("SELECT id, name, type, status, models FROM channels WHERE type IN ('opencode_free','lobsterai') ORDER BY type, id")
    cards = []
    for family, meta in FAMILIES.items():
        own = [r for r in rows if r["type"] == family]
        model_count = 0
        for r in own:
            model_count += len([m for m in str(r["models"] or "").split(",") if m.strip()])
        card = {
            "family": family,
            "name": meta["name"],
            "vendor": meta["vendor"],
            "login": meta["login"],
            "desc": meta["desc"],
            "connected": bool(own),
            "enabled": any(r["status"] == 1 for r in own),
            "accounts": len(own),
            "models": model_count,
            "channels": [{"id": r["id"], "name": r["name"], "status": r["status"],
                          "breaker": channel_breaker_state(r["id"])} for r in own],
        }
        cards.append(card)
    return {"cards": cards}
