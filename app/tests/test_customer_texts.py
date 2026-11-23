"""Customer texts in several languages (#79)."""


def test_migration_puts_existing_texts_into_the_projects_language(tmp_path, monkeypatch):
    from datetime import datetime

    from alembic import command
    from sqlalchemy import create_engine, text

    from plainboard import config, migrate

    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    config.get_config.cache_clear()
    (tmp_path / "data").mkdir()
    command.upgrade(migrate.alembic_config(), "0076")
    engine = create_engine(config.get_config().sync_database_url)
    now = datetime(2026, 11, 1)
    with engine.begin() as conn:
        for pid, language in ((1, "German"), (2, ""), (3, "Deutsch (du)"), (4, "Klingon")):
            conn.execute(
                text("INSERT INTO projects (id, name, slug, description, language, auto_wake_manager, task_counter, created_at, updated_at) VALUES (:id, :name, :name, '', :language, true, 1, :now, :now)"),
                {"id": pid, "name": f"p{pid}", "language": language, "now": now},
            )
        conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, number, title, description, customer_title, customer_description, status, position, priority, labels, user_action, created_at, updated_at) "
                "VALUES (1, 1, 1, 'Intern', '', 'Schneller laden', 'Bücher öffnen sich schneller.', 'done', 1024, 'medium', '[]', '', :now, :now)"
            ),
            {"now": now},
        )
    command.upgrade(migrate.alembic_config(), "head")
    with engine.connect() as conn:
        languages = dict(conn.execute(text("SELECT id, customer_languages FROM projects ORDER BY id")).all())
        task = conn.execute(text("SELECT customer_title, customer_description, customer_texts FROM tasks")).one()
    engine.dispose()
    config.get_config.cache_clear()
    as_list = {pid: value if isinstance(value, list) else __import__("json").loads(value) for pid, value in languages.items()}
    assert as_list == {1: ["de"], 2: ["en"], 3: ["de"], 4: ["en"]}
    assert task[0] == "Schneller laden" and task[1] == "Bücher öffnen sich schneller."  # the default language's, as before
    assert task[2] in ({}, "{}")


def test_texts_per_language_and_public_fallback(client, project):
    pid = project["id"]
    assert project["customer_languages"] == ["en"]
    assert client.patch(f"/api/projects/{pid}", json={"customer_languages": ["en", "de", "fr"]}).json()["customer_languages"] == ["en", "de", "fr"]
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "Reader", "customer_title": "Faster books", "customer_description": "Books open sooner.", "customer_texts": {"de": {"title": "Schnellere Bücher", "description": "Bücher öffnen sich schneller."}}},
    ).json()
    assert task["customer_texts"] == {"de": {"title": "Schnellere Bücher", "description": "Bücher öffnen sich schneller."}}
    # Only a language of the project; fields are merged, not replaced.
    assert client.patch(f"/api/tasks/{task['id']}", json={"customer_texts": {"es": {"title": "x"}}}).status_code == 400
    patched = client.patch(f"/api/tasks/{task['id']}", json={"customer_texts": {"de": {"description": "Neu."}, "en": {"title": "Fast books"}}}).json()
    assert patched["customer_texts"]["de"] == {"title": "Schnellere Bücher", "description": "Neu."}
    assert patched["customer_title"] == "Fast books"  # the default language is customer_title
    hidden = client.post(f"/api/projects/{pid}/tasks", json={"title": "Only German", "customer_texts": {"de": {"title": "Nur Deutsch"}}}).json()

    key = client.post(f"/api/projects/{pid}/public-api-keys", json={"label": "Web"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    def public(lang=None):
        return client.get("/api/public/tasks", params={"lang": lang} if lang else {}, headers=headers).json()

    board = public()
    assert board["languages"] == ["en", "de", "fr"] and board["default_language"] == "en"
    assert [(t["number"], t["lang"], t["title"]) for t in board["tasks"]] == [(task["number"], "en", "Fast books")]  # no default title: hidden
    assert [(t["lang"], t["title"], t["description"]) for t in public("de")["tasks"]] == [("de", "Schnellere Bücher", "Neu.")]
    assert [(t["lang"], t["title"]) for t in public("fr")["tasks"]] == [("en", "Fast books")]  # not translated: the default
    assert [(t["lang"], t["title"]) for t in public("xx")["tasks"]] == [("en", "Fast books")]  # unknown language: the default
    assert hidden["number"] not in [t["number"] for t in public("de")["tasks"]]


def test_removing_a_language_keeps_its_texts_and_switching_the_default_moves_them(client, project):
    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"customer_languages": ["en", "de"]})
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "T", "customer_title": "Hello", "customer_texts": {"de": {"title": "Hallo"}}}).json()
    assert client.get(f"/api/projects/{pid}/customer-texts").json() == {"en": 1, "de": 1}

    assert client.patch(f"/api/projects/{pid}", json={"customer_languages": []}).status_code == 422  # never none
    client.patch(f"/api/projects/{pid}", json={"customer_languages": ["en"]})
    kept = client.get(f"/api/tasks/{task['id']}").json()
    assert kept["customer_texts"]["de"]["title"] == "Hallo"  # removed, but stored
    assert client.get(f"/api/projects/{pid}/customer-texts").json()["de"] == 1
    client.patch(f"/api/projects/{pid}", json={"customer_languages": ["en", "de"]})
    assert client.get(f"/api/tasks/{task['id']}").json()["customer_texts"]["de"]["title"] == "Hallo"  # back with the language

    # German becomes the default: its texts are customer_title now, the English ones go to customer_texts.
    client.patch(f"/api/projects/{pid}", json={"customer_languages": ["de", "en"]})
    switched = client.get(f"/api/tasks/{task['id']}").json()
    assert switched["customer_title"] == "Hallo" and switched["customer_texts"] == {"en": {"title": "Hello", "description": ""}}
