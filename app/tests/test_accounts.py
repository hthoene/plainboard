from conftest import agents_by_slug, idle, wait_for
from plainboard_protocol import LimitWindow
from test_orchestration import chat, make_dev, runner


def accounts(client) -> dict[str, dict]:
    return {a["label"]: a for a in client.get("/api/accounts").json()}


def limits_of(client, account_id: int) -> dict:
    return next(a for a in client.get("/api/limits").json()["accounts"] if a["account_id"] == account_id)


def turn(client, pid: int, agent_id: int, text: str = "SAY hi"):
    chat(client, agent_id, text)
    wait_for(lambda: idle(client, pid))
    return runner(client).specs[-1].credentials


def test_existing_sign_ins_become_accounts(client):
    found = accounts(client)
    assert (found["Claude"]["provider"], found["Claude"]["auth_mode"]) == ("claude_code", "runner_login")
    assert (found["Codex"]["provider"], found["Codex"]["auth_mode"]) == ("codex", "chatgpt")
    assert client.post("/api/accounts", json={"provider": "claude_code", "auth_mode": "chatgpt"}).status_code == 400
    added = client.post("/api/accounts", json={"provider": "claude_code", "auth_mode": "api_key", "secret": "sk-x"}).json()
    assert added["label"] == "Claude 2" and added["secret_set"] and "sk-x" not in client.get("/api/accounts").text


def test_runs_report_plan_limits_per_account(client, project):
    pid = project["id"]
    claude = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{claude['id']}", json={"auth_mode": "oauth_token", "secret": "tok-a"})
    dev = make_dev(client, pid)
    assert limits_of(client, claude["id"])["windows"] == []
    creds = turn(client, pid, dev["id"], "LIMITS five_hour 0.9 3600\nLIMITS seven_day_opus 0.4 3600\nLIMITS seven_day 0.7 -10")
    assert (creds.claude_auth_mode, creds.claude_oauth_token) == ("oauth_token", "tok-a")
    body = limits_of(client, claude["id"])
    # The weekly window has reset; Claude's old 5-hour window is not shown any more.
    assert {w["name"]: w["utilization"] for w in body["windows"]} == {"seven_day_opus": 0.4, "seven_day": 0.0} and body["updated_at"]
    client.patch(f"/api/accounts/{claude['id']}", json={"auth_mode": "api_key"})
    body = limits_of(client, claude["id"])
    assert body["windows"] == [] and body["available"] is False


def test_limits_are_read_for_every_subscription(client, project):
    found = accounts(client)
    client.patch(f"/api/accounts/{found['Claude']['id']}", json={"auth_mode": "oauth_token"})
    key_account = client.post("/api/accounts", json={"provider": "codex", "auth_mode": "api_key", "secret": "sk-o"}).json()
    runner(client).limit_windows = [LimitWindow(name="seven_day", utilization=0.2), LimitWindow(name="codex_seven_day", utilization=0.5)]
    wait_for(lambda: limits_of(client, found["Codex"]["id"])["windows"])
    assert [w["name"] for w in limits_of(client, found["Claude"]["id"])["windows"]] == ["seven_day"]
    assert [w["name"] for w in limits_of(client, found["Codex"]["id"])["windows"]] == ["codex_seven_day"]
    assert all(creds.codex_account == "default" for provider, creds in runner(client).limit_calls if provider == "codex")  # the API key has no plan
    assert limits_of(client, key_account["id"])["available"] is False


def test_turns_go_to_the_subscription_with_room(client, project):
    pid = project["id"]
    first = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{first['id']}", json={"auth_mode": "oauth_token", "secret": "tok-a"})
    second = client.post("/api/accounts", json={"provider": "claude_code", "label": "Second", "auth_mode": "oauth_token", "secret": "tok-b"}).json()
    dev = make_dev(client, pid)

    assert turn(client, pid, dev["id"], "LIMITS seven_day 0.97 3600").claude_oauth_token == "tok-a"
    assert turn(client, pid, dev["id"], "LIMITS seven_day 0.1 3600").claude_oauth_token == "tok-b"  # the first one is nearly full
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-b"  # stays for the prompt cache

    paid = client.post("/api/accounts", json={"provider": "claude_code", "label": "Paid", "auth_mode": "api_key", "secret": "sk-paid"}).json()
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-b"  # subscriptions come before API keys
    assert turn(client, pid, dev["id"], "LIMITS seven_day 1.0 3600").claude_oauth_token == "tok-b"
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-a"  # the rest of the first one comes before API keys
    assert turn(client, pid, dev["id"], "LIMITS seven_day 1.0 3600").claude_oauth_token == "tok-a"  # and stays
    creds = turn(client, pid, dev["id"])
    assert (creds.claude_auth_mode, creds.anthropic_api_key) == ("api_key", "sk-paid")  # every subscription is used up

    client.patch(f"/api/accounts/{second['id']}", json={"enabled": True})
    client.patch(f"/api/agents/{dev['id']}", json={"account_id": paid["id"]})
    assert turn(client, pid, dev["id"]).anthropic_api_key == "sk-paid"  # pinned
    client.delete(f"/api/accounts/{paid['id']}")
    assert agents_by_slug(client, pid)["dev"]["account_id"] is None


def test_codex_accounts_have_their_own_sign_in(client, project):
    pid = project["id"]
    claude = accounts(client)["Claude"]
    other = client.post("/api/accounts", json={"provider": "codex", "label": "Team", "auth_mode": "chatgpt"}).json()
    dev = make_dev(client, pid, provider="codex")
    assert turn(client, pid, dev["id"]).codex_account == "default"
    assert client.patch(f"/api/agents/{dev['id']}", json={"account_id": claude["id"]}).status_code == 400
    client.patch(f"/api/agents/{dev['id']}", json={"account_id": other["id"]})
    creds = turn(client, pid, dev["id"])
    assert creds.codex_account not in ("default", "") and creds.codex_auth_mode == "chatgpt"
    moved = client.patch(f"/api/agents/{dev['id']}", json={"provider": "claude_code"}).json()
    assert moved["account_id"] is None  # the pin belonged to Codex
