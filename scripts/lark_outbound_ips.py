"""Print the IP addresses Lark sends event callbacks from, for the SRE's inbound access list.

    python -m scripts.lark_outbound_ips

Needs LARK_APP_ID / LARK_APP_SECRET in .env and the permission `event:ip_list`
(Developer Console → your app → Permissions & Scopes → search "event:ip_list" → add → publish).
Lark says the list can change and will announce changes in advance; re-run this when it does.
"""
import requests

from app import config, lark_client

ips, token = [], ""
while True:
    params = {"page_size": 50}
    if token:
        params["page_token"] = token
    r = requests.get(f"{config.LARK_OPEN_API_BASE}/event/v1/outbound_ip",
                     headers={"Authorization": f"Bearer {lark_client.get_tenant_token()}"},
                     params=params, timeout=config.HTTP_TIMEOUT)
    data = lark_client._check(r, "outbound_ip")
    ips += data.get("ip_list") or []
    token = data.get("page_token") or ""
    if not data.get("has_more") or not token:
        break

print("\n".join(ips) if ips else "(empty list: check the event:ip_list permission)")
