"""Tests for SEMANTICA_GRAPH_PATH file persistence of the Explorer graph."""

import json
import time

import pytest

pytest.importorskip("fastapi")

from semantica.explorer.app import create_app  # noqa: E402
from semantica.explorer.persistence import (  # noqa: E402
    DEFAULT_SAVE_INTERVAL,
    GraphPersistence,
    graph_path_from_env,
    save_interval_from_env,
)
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "ALLOWED_ORIGINS",
        "EXPLORER_CORS_ORIGINS",
        "SEMANTICA_GRAPH_PATH",
        "SEMANTICA_GRAPH_SAVE_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)


def _import_nodes(client, *ids):
    payload = {"nodes": [{"id": i, "type": "thing", "label": i} for i in ids], "edges": []}
    resp = client.post(
        "/api/import",
        files={"file": ("g.json", json.dumps(payload), "application/json")},
    )
    assert resp.status_code == 200, resp.text


def test_env_helpers(monkeypatch):
    assert graph_path_from_env() is None
    assert save_interval_from_env() == DEFAULT_SAVE_INTERVAL
    monkeypatch.setenv("SEMANTICA_GRAPH_PATH", "  /data/graph.json ")
    monkeypatch.setenv("SEMANTICA_GRAPH_SAVE_INTERVAL", "5")
    assert graph_path_from_env() == "/data/graph.json"
    assert save_interval_from_env() == 5.0
    monkeypatch.setenv("SEMANTICA_GRAPH_SAVE_INTERVAL", "soon")
    assert save_interval_from_env() == DEFAULT_SAVE_INTERVAL


def test_graph_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "data" / "graph.json"
    monkeypatch.setenv("SEMANTICA_GRAPH_PATH", str(path))
    # Long interval: the save must come from the shutdown hook, not the timer.
    monkeypatch.setenv("SEMANTICA_GRAPH_SAVE_INTERVAL", "3600")

    app = create_app()
    with TestClient(app) as client:
        _import_nodes(client, "a", "b")
    assert path.exists()

    restarted = create_app()
    with TestClient(restarted) as client:
        assert set(restarted.state.session.graph.nodes) == {"a", "b"}
        # The search index is rebuilt from the loaded graph too.
        resp = client.post("/api/graph/search", json={"query": "a"})
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1


def test_unset_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        _import_nodes(client, "a")
    assert list(tmp_path.iterdir()) == []


def test_explicit_session_is_not_persisted(tmp_path, monkeypatch):
    from semantica.context.context_graph import ContextGraph
    from semantica.explorer.session import GraphSession

    path = tmp_path / "graph.json"
    monkeypatch.setenv("SEMANTICA_GRAPH_PATH", str(path))
    session = GraphSession(ContextGraph(advanced_analytics=False))
    with TestClient(create_app(session=session)) as client:
        _import_nodes(client, "a")
    assert not path.exists()


def test_corrupt_file_fails_startup_and_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "graph.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SEMANTICA_GRAPH_PATH", str(path))

    with pytest.raises(RuntimeError, match="SEMANTICA_GRAPH_PATH"):
        create_app()
    assert path.read_text(encoding="utf-8") == "{not json"


def test_save_only_when_graph_changed(tmp_path):
    from semantica.context.context_graph import ContextGraph
    from semantica.explorer.runtime import install_mutation_bridge
    from semantica.explorer.session import GraphSession
    from fastapi import FastAPI

    session = GraphSession(ContextGraph(advanced_analytics=False))
    install_mutation_bridge(FastAPI(), session)
    persistence = GraphPersistence(session, str(tmp_path / "graph.json"))
    persistence.load()

    assert persistence.save() is False  # nothing changed since load
    assert not (tmp_path / "graph.json").exists()

    session.graph.add_node("a", node_type="thing", content="A")
    assert persistence.save() is True
    assert persistence.save() is False  # already saved this revision
    assert persistence.save(force=True) is True


def test_autosave_timer_writes_changes(tmp_path, monkeypatch):
    path = tmp_path / "graph.json"
    monkeypatch.setenv("SEMANTICA_GRAPH_PATH", str(path))
    monkeypatch.setenv("SEMANTICA_GRAPH_SAVE_INTERVAL", "0.05")

    with TestClient(create_app()) as client:
        _import_nodes(client, "a")
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert path.exists()  # written by the timer, while the app is still up
