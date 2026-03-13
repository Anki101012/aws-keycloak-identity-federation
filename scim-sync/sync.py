#Available Automation Module Used For this sync
#!/usr/bin/env python3
import os, json, requests

# ----------------------------
# Required environment vars
# ----------------------------
KC_BASE   = os.environ["KC_BASE"].rstrip("/")
KC_REALM  = os.environ["KC_REALM"]
KC_CLIENT = os.environ["KC_CLIENT_ID"]
KC_SECRET = os.environ["KC_CLIENT_SECRET"]

AWS_SCIM  = os.environ["AWS_SCIM_BASE"].rstrip("/")
AWS_TOKEN = os.environ["AWS_SCIM_TOKEN"]

AWS_HEADERS = {
    "Authorization": f"Bearer {AWS_TOKEN}",
    "Content-Type": "application/scim+json"
}

# ----------------------------
# Keycloak helpers
# ----------------------------
def kc_token():
    url = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/token"
    r = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": KC_CLIENT,
            "client_secret": KC_SECRET
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["access_token"]

def kc_get(token, path, params=None):
    url = f"{KC_BASE}/admin/realms/{KC_REALM}{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# ----------------------------
# AWS SCIM helpers
# ----------------------------
def aws_get(path, params=None):
    r = requests.get(f"{AWS_SCIM}{path}", headers=AWS_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def aws_post(path, body):
    r = requests.post(f"{AWS_SCIM}{path}", headers=AWS_HEADERS, data=json.dumps(body), timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST {path} {r.status_code}: {r.text}")
    return r.json()

def aws_patch(path, body):
    r = requests.patch(f"{AWS_SCIM}{path}", headers=AWS_HEADERS, data=json.dumps(body), timeout=30)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"PATCH {path} {r.status_code}: {r.text}")

# ----------------------------
# Ensure group/user exists
# ----------------------------
def ensure_group(name: str) -> str:
    q = aws_get("/Groups", params={"filter": f'displayName eq "{name}"'})
    if q.get("Resources"):
        return q["Resources"][0]["id"]
    g = aws_post("/Groups", {"displayName": name})
    return g["id"]

def ensure_user(user_name: str, given: str = "", family: str = "") -> str:
    """
    AWS SCIM uses userName as unique identifier.
    We use email if present, else fallback to Keycloak username.
    """
    q = aws_get("/Users", params={"filter": f'userName eq "{user_name}"'})
    if q.get("Resources"):
        return q["Resources"][0]["id"]

    body = {
        "userName": user_name,
        "displayName": user_name,
        "name": {"givenName": given or "", "familyName": family or ""},
        "active": True
    }

    # If user_name looks like an email, populate emails[] too (nice-to-have)
    if "@" in user_name:
        body["emails"] = [{"value": user_name, "primary": True}]

    u = aws_post("/Users", body)
    return u["id"]

# ----------------------------
# Group membership sync
# ----------------------------
def aws_get_group(group_id: str):
    r = requests.get(f"{AWS_SCIM}/Groups/{group_id}", headers=AWS_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def sync_group_members(group_id: str, desired_member_ids):
    g = aws_get_group(group_id)

    current = set()
    for m in g.get("members", []) or []:
        if "value" in m:
            current.add(m["value"])

    desired = set(desired_member_ids)

    to_add = sorted(list(desired - current))
    to_remove = sorted(list(current - desired))

    ops = []
    if to_remove:
        for mid in to_remove:
            ops.append({"op": "Remove", "path": f'members[value eq "{mid}"]'})
    if to_add:
        ops.append({"op": "Add", "path": "members", "value": [{"value": mid} for mid in to_add]})

    if not ops:
        return

    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": ops
    }
    aws_patch(f"/Groups/{group_id}", body)

# ----------------------------
# Main
# ----------------------------
def main():
    token = kc_token()
    groups = kc_get(token, "/groups")

    for g in groups:
        gname, gid = g["name"], g["id"]

        aws_gid = ensure_group(gname)

        # IMPORTANT: ask Keycloak for full user representation; otherwise email/firstName/lastName may be missing
        members = kc_get(
            token,
            f"/groups/{gid}/members",
            params={"max": 1000, "briefRepresentation": "false"}
        )

        aws_members = []
        for m in members:
            # Fallback to username if email is missing (common when LDAP mail isn't mapped)
            user_name = m.get("email") or m.get("username")
            if not user_name:
                continue

            aws_uid = ensure_user(
                user_name,
                m.get("firstName", "") or "",
                m.get("lastName", "") or ""
            )
            aws_members.append(aws_uid)

        sync_group_members(aws_gid, aws_members)
        print(f"Synced group={gname} users={len(aws_members)}")

if __name__ == "__main__":
    main()
