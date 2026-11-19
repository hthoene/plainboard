from conftest import agents_by_slug, idle, wait_for
from plainboard.orchestration.limits_watcher import LimitsWatcher
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
    # The weekly window has reset.
    assert {w["name"]: w["utilization"] for w in body["windows"]} == {"five_hour": 0.9, "seven_day_opus": 0.4, "seven_day": 0.0} and body["updated_at"]
    # A report that lists all windows forgets the ones it leaves out: a plan that dropped a limit.
    turn(client, pid, dev["id"], "LIMITS seven_day 0.1 3600 ALL")
    assert [w["name"] for w in limits_of(client, claude["id"])["windows"]] == ["seven_day"]
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


def test_limits_are_read_without_anyone_looking(client, project):
    """A subscription that no agent uses, with the UI closed: the watcher reads its limits anyway."""
    claude = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{claude['id']}", json={"auth_mode": "oauth_token"})
    runner(client).limit_windows = [LimitWindow(name="seven_day", utilization=0.3)]
    watcher = LimitsWatcher(client.app_ref.state.runs, 0.05)
    client.portal.call(watcher.start)
    try:
        wait_for(lambda: runner(client).limit_calls)  # no request for the limits, no turn
    finally:
        client.portal.call(watcher.stop)
    wait_for(lambda: limits_of(client, claude["id"])["windows"])
    assert limits_of(client, claude["id"])["windows"][0]["utilization"] == 0.3


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


def test_a_turn_turned_away_by_a_used_up_limit_runs_on_another_subscription(client, project):
    pid = project["id"]
    first = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{first['id']}", json={"auth_mode": "oauth_token", "secret": "tok-a"})
    client.post("/api/accounts", json={"provider": "claude_code", "label": "Second", "auth_mode": "oauth_token", "secret": "tok-b"})
    dev = make_dev(client, pid)
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-a"

    # The first one hits its 5-hour limit: the same turn runs again on the second one, and succeeds.
    specs = len(runner(client).specs)
    assert turn(client, pid, dev["id"], "REJECT tok-a five_hour 3600\nSAY done").claude_oauth_token == "tok-b"
    assert [s.credentials.claude_oauth_token for s in runner(client).specs[specs:]] == ["tok-a", "tok-b"]
    last = client.get(f"/api/projects/{pid}/runs?limit=1").json()[0]
    assert last["status"] == "succeeded" and "done" in last["result_text"]
    assert next(w for w in limits_of(client, first["id"])["windows"] if w["name"] == "five_hour")["status"] == "rejected"
    # The following turns stay off it until the reset.
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-b"

    # Both used up: the turn fails with the provider's message, as before.
    specs = len(runner(client).specs)
    turn(client, pid, dev["id"], "REJECT tok-b five_hour 3600")
    assert len(runner(client).specs) == specs + 1
    assert client.get(f"/api/projects/{pid}/runs?limit=1").json()[0]["status"] == "failed"


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


def test_only_a_subscription_without_a_fresh_report_is_asked(client, project):
    """A subscription in use reports its limits with every turn. One that PlainBoard has stopped
    using (nearly full) reports nothing any more: that one has to be asked, or its numbers stay."""
    import time

    from plainboard.db import sessionmaker
    from plainboard.models import Account
    from plainboard.services import limits as limits_service
    from sqlalchemy import select

    client.patch(f"/api/accounts/{accounts(client)['Claude']['id']}", json={"auth_mode": "oauth_token", "secret": "tok-in-use"})
    idle_account = client.post("/api/accounts", json={"provider": "claude_code", "auth_mode": "oauth_token", "secret": "tok-idle", "label": "Idle"}).json()
    runs = client.app_ref.state.runs

    async def report(label: str, age: float) -> None:
        async with sessionmaker()() as s:
            key = await s.scalar(select(Account.key).where(Account.label == label))
            await limits_service.record(s, key, [LimitWindow(name="seven_day", utilization=0.9)])
            row, stored = await limits_service._stored(s)
            stored[key]["updated_at"] = int(time.time() - age)
            row.value = __import__("json").dumps({"accounts": stored})
            await s.commit()

    client.portal.call(report, "Claude", 30)  # a turn reported it half a minute ago
    client.portal.call(report, "Idle", 36 * 3600)  # its last turn was a day and a half ago
    wait_for(lambda: not (runs._limits_task and not runs._limits_task.done()))
    runner(client).limit_calls.clear()
    runner(client).limit_windows = [LimitWindow(name="seven_day", utilization=0.6)]
    client.portal.call(runs._refresh_limits)
    asked = [creds.claude_oauth_token for provider, creds in runner(client).limit_calls if provider == "claude_code"]
    assert asked == ["tok-idle"]
    assert limits_of(client, idle_account["id"])["windows"][0]["utilization"] == 0.6


def test_a_window_that_is_no_longer_reported_stops_counting(client, project, monkeypatch):
    """A window some requests report and others don't (a limit for certain models), or a plan that
    was reset early: its last number would stay for ever, and an account avoided because of it
    would never get the request that corrects it."""
    from plainboard.db import sessionmaker
    from plainboard.models import Account
    from plainboard.services import limits as limits_service
    from sqlalchemy import select

    claude = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{claude['id']}", json={"auth_mode": "oauth_token", "secret": "tok"})
    clock = [float(int(limits_service.time.time()))]  # from now on: the sign-in is checked against the same clock
    monkeypatch.setattr(limits_service.time, "time", lambda: clock[0])

    async def report(**used: float) -> dict[str, float]:
        async with sessionmaker()() as s:
            key = await s.scalar(select(Account.key).where(Account.id == claude["id"]))
            await limits_service.record(s, key, [LimitWindow(name=name, utilization=value, resets_at=int(clock[0]) + 5 * 86400) for name, value in used.items()])
            return {key: value for key, value in (await limits_service.usage(s)).items()}

    key = client.portal.call(lambda: report(seven_day=0.89, seven_day_extra=0.95))
    assert list(key.values()) == [0.95]
    # Later reports bring only one of the two windows (they come from requests that don't touch the other).
    clock[0] += 3600
    assert list(client.portal.call(lambda: report(seven_day=0.6)).values()) == [0.95]  # an hour is nothing
    clock[0] += 3 * 3600
    assert list(client.portal.call(lambda: report(seven_day=0.57)).values()) == [0.57]
    assert [w["name"] for w in limits_of(client, claude["id"])["windows"]] == ["seven_day"]
    # A request that touches it reports it again: it counts again, with its new number.
    clock[0] += 60
    assert list(client.portal.call(lambda: report(seven_day_extra=0.33)).values()) == [0.57]
    assert {w["name"]: w["utilization"] for w in limits_of(client, claude["id"])["windows"]} == {"seven_day": 0.57, "seven_day_extra": 0.33}


def test_a_project_runs_on_the_subscription_it_names(client, project):
    """Two projects, two subscriptions: each uses up its own quota. An agent's own choice still wins."""
    pid = project["id"]
    first = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{first['id']}", json={"auth_mode": "oauth_token", "secret": "tok-a"})
    second = client.post("/api/accounts", json={"provider": "claude_code", "label": "Second", "auth_mode": "oauth_token", "secret": "tok-b"}).json()
    dev = make_dev(client, pid)
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-a"  # automatic

    assert client.patch(f"/api/projects/{pid}", json={"account_id": second["id"]}).json()["account_id"] == second["id"]
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-b"
    client.patch(f"/api/agents/{dev['id']}", json={"account_id": first["id"]})
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-a"  # the agent's own
    client.patch(f"/api/agents/{dev['id']}", json={"account_id": None})

    assert client.patch(f"/api/projects/{pid}", json={"account_id": 999}).status_code == 404
    client.delete(f"/api/accounts/{second['id']}")  # gone: the project is back on automatic
    assert client.get(f"/api/projects/{pid}").json()["account_id"] is None
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-a"


def test_a_new_installation_is_not_set_up_until_its_admin_says_so(client):
    assert client.get("/api/settings").json()["values"]["setup_done"] is False
    client.put("/api/settings", json={"values": {"setup_done": True}})
    assert client.get("/api/settings").json()["values"]["setup_done"] is True


def test_a_model_server_of_one_s_own_is_an_account_with_its_address_and_models(client, project):
    """Providers that are an address (a model server, a model host): which ones exist is the runner's
    list. An agent can be given one once an account names a model, and a turn goes to the account
    that offers the agent's model."""
    pid = project["id"]
    meta = client.get("/api/meta").json()
    assert [p["name"] for p in meta["providers"]] == ["claude_code", "codex"]
    assert [p["name"] for p in meta["endpoints"]] == ["ollama", "gemini"] and meta["endpoints"][0]["endpoint"]["key"] is False
    assert client.post("/api/accounts", json={"provider": "nowhere", "auth_mode": "api_key"}).status_code == 400
    assert client.post("/api/accounts", json={"provider": "ollama", "auth_mode": "chatgpt"}).status_code == 400

    home = client.post("/api/accounts", json={"provider": "ollama", "auth_mode": "api_key"}).json()
    assert (home["label"], home["endpoint_url"], home["models"], home["context_tokens"]) == ("Ollama", "", [], 0)
    # No model named yet: nothing an agent could run on.
    assert [p["name"] for p in client.get("/api/meta").json()["providers"]] == ["claude_code", "codex"]
    assert client.post(f"/api/accounts/{home['id']}/models").json() == {"models": ["a-model", "b-model"]}
    assert runner(client).models_asked[0] == "ollama"

    changed = client.patch(f"/api/accounts/{home['id']}", json={"endpoint_url": " http://models.lan:11434 ", "models": ["coder", " coder ", "small-one"], "context_tokens": 64000}).json()
    assert (changed["endpoint_url"], changed["models"], changed["context_tokens"]) == ("http://models.lan:11434", ["coder", "small-one"], 64000)
    offered = {p["name"]: [m["id"] for m in p["models"]] for p in client.get("/api/meta").json()["providers"]}
    assert offered["ollama"] == ["coder", "small-one"] and "gemini" not in offered

    # An agent moves to it: without a model named for it, it gets one the provider has, for its summaries too.
    dev = make_dev(client, pid)
    moved = client.patch(f"/api/agents/{dev['id']}", json={"provider": "ollama"}).json()
    assert (moved["provider"], moved["model"], moved["summary_model"]) == ("ollama", "coder", "coder")
    creds = turn(client, pid, dev["id"])
    spec = runner(client).specs[-1]
    assert (spec.provider, spec.model) == ("ollama", "coder")
    assert (creds.endpoint_url, creds.endpoint_key, creds.endpoint_context) == ("http://models.lan:11434", None, 64000)

    # A second server of the same provider with other models: the turn goes where the agent's model is.
    client.post("/api/accounts", json={"provider": "ollama", "auth_mode": "api_key", "label": "Big box", "secret": "k-big", "endpoint_url": "http://big.lan:11434", "models": ["large-one"]})
    assert [p["models"] for p in client.get("/api/meta").json()["providers"] if p["name"] == "ollama"][0][2]["id"] == "large-one"
    client.patch(f"/api/agents/{dev['id']}", json={"model": "large-one"})
    creds = turn(client, pid, dev["id"])
    assert (creds.endpoint_url, creds.endpoint_key) == ("http://big.lan:11434", "k-big")
    client.patch(f"/api/agents/{dev['id']}", json={"model": "coder"})
    assert turn(client, pid, dev["id"]).endpoint_url == "http://models.lan:11434"  # not the account of its last turn

    # The manager's list of models names it, and not the providers nobody set up.
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], "TOOL list_models {}")
    wait_for(lambda: idle(client, pid))
    messages = client.get(f"/api/agents/{manager['id']}/conversation").json()["messages"]
    listed = next(m["meta"]["result"] for m in messages if m["kind"] == "tool" and m["content"] == "list_models")
    assert "ollama: coder (medium), small-one (medium), large-one (medium)" in listed and "gemini" not in listed


def test_a_project_can_be_limited_to_some_subscriptions(client, project):
    """A project shared with others must not draw on a subscription that is meant for something else:
    an admin names the ones it may use, and no choice inside the project gets around that."""
    from test_users import as_admin, new_user, sign_in

    pid = project["id"]
    own = accounts(client)["Claude"]
    client.patch(f"/api/accounts/{own['id']}", json={"auth_mode": "oauth_token", "secret": "tok-own"})
    shared = client.post("/api/accounts", json={"provider": "claude_code", "label": "Shared", "auth_mode": "oauth_token", "secret": "tok-shared"}).json()
    dev = make_dev(client, pid)
    assert client.get(f"/api/projects/{pid}").json()["account_ids"] == []  # any, as before
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-own"

    # Limited to the shared one. An agent that was pinned to the other one is automatic again.
    client.patch(f"/api/agents/{dev['id']}", json={"account_id": own["id"]})
    assert client.put(f"/api/projects/{pid}/accounts", json={"account_ids": [shared["id"]]}).json()["account_ids"] == [shared["id"]]
    assert agents_by_slug(client, pid)["dev"]["account_id"] is None
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-shared"
    # Used up or not: the other subscription does not step in.
    turn(client, pid, dev["id"], "LIMITS seven_day 1.0 3600")
    assert turn(client, pid, dev["id"]).claude_oauth_token == "tok-shared"
    # Nobody chooses another one, for an agent or for the project.
    assert client.patch(f"/api/agents/{dev['id']}", json={"account_id": own["id"]}).status_code == 400
    assert client.patch(f"/api/projects/{pid}", json={"account_id": own["id"]}).status_code == 400
    assert client.patch(f"/api/projects/{pid}", json={"account_id": shared["id"]}).status_code == 200
    assert client.put(f"/api/projects/{pid}/accounts", json={"account_ids": [999]}).status_code == 404

    # A provider the project has no subscription for: the turn fails and says why, instead of using another one.
    client.patch(f"/api/agents/{dev['id']}", json={"provider": "codex"})
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    failed = client.get(f"/api/projects/{pid}/runs?limit=1").json()[0]
    assert failed["status"] == "failed" and "may only use some of the installation's subscriptions, and none of them is for codex" in failed["error"]
    # Another project is not touched by any of this.
    other = client.post("/api/projects", json={"name": "Mine"}).json()["id"]
    mine = make_dev(client, other)
    assert turn(client, other, mine["id"]).claude_oauth_token == "tok-own"

    # It is the admin's call: the project's owner cannot lift it.
    friend = new_user(client, "frieda")
    assert client.put(f"/api/projects/{pid}/members/{friend['id']}", json={"role": "owner"}).status_code in (200, 201, 204)
    sign_in(client, "frieda", "frieda-password")
    assert client.put(f"/api/projects/{pid}/accounts", json={"account_ids": []}).status_code == 403
    assert client.patch(f"/api/projects/{pid}", json={"account_id": own["id"]}).status_code == 400
    assert client.get(f"/api/projects/{pid}").json()["account_ids"] == [shared["id"]]
    as_admin(client)
    assert client.put(f"/api/projects/{pid}/accounts", json={"account_ids": []}).json()["account_ids"] == []
