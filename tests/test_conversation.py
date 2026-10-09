import threading
import time

import pytest

from muster import config, conversation, decisions


@pytest.fixture(autouse=True)
def topics(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_topics", True)
    monkeypatch.setitem(config.settings, "notify_chat_id", "-100123")
    monkeypatch.setitem(config.settings, "notify_user_id", "42")
    monkeypatch.setattr(conversation, "WAIT", 1)
    monkeypatch.setattr(conversation, "POLL", 0.01)
    monkeypatch.setattr(decisions, "gateway_up", lambda: True)


def test_off_makes_no_reference(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_topics", False)
    assert conversation.request("led1", "app#1 Title") is None
    assert conversation.load("led1") is None


def test_gateway_down_falls_back_at_once(monkeypatch):
    monkeypatch.setattr(decisions, "gateway_up", lambda: False)
    start = time.monotonic()
    ref = conversation.request("led1", "app#1 Title")
    assert time.monotonic() - start < 0.5
    assert ref["state"] == "fallback" and "gateway" in ref["why"]
    assert conversation.target("led1")["thread_id"] is None


def test_the_gateway_fills_it_and_the_request_returns_the_thread():
    def gateway():
        while (conversation.load("led1") or {}).get("state") != "pending":
            time.sleep(0.01)
        conversation.swap("led1", ("pending",), state="open", thread_id="77")
    threading.Thread(target=gateway).start()
    ref = conversation.request("led1", "app#1 Title")
    assert ref["state"] == "open" and ref["thread_id"] == "77"
    assert ref["chat_id"] == "-100123" and ref["user_id"] == "42" and ref["platform"] == "telegram"
    assert conversation.target("led1") == {"chat_id": "-100123", "user_id": "42", "chat_type": "group",
                                           "thread_id": "77"}


def test_a_timeout_falls_back_and_a_late_create_cannot_overwrite_it():
    ref = conversation.request("led1", "app#1 Title")
    assert ref["state"] == "fallback" and "no topic" in ref["why"]
    _, ok = conversation.swap("led1", ("pending", "creating"), state="open", thread_id="9")
    assert not ok and conversation.load("led1")["state"] == "fallback"


def test_an_existing_reference_is_returned_unchanged():
    conversation.update("led1", state="open", thread_id="5", cards=["led1"], name="n")
    assert conversation.request("led1", "other name")["thread_id"] == "5"
    assert conversation.load("led1")["cards"] == ["led1"]


def test_request_never_raises(monkeypatch):
    def boom():
        raise OSError("disk")
    monkeypatch.setattr(decisions, "gateway_up", boom)
    assert conversation.request("led1", "n") is None


def test_target_without_a_reference_is_the_main_chat():
    assert conversation.target("nope") == {"chat_id": "-100123", "user_id": "42", "chat_type": "group",
                                           "thread_id": None}


def test_name_is_cut_to_telegrams_limit():
    assert conversation.name("o/app", "Fix it", issue=4) == "app#4 Fix it"
    assert conversation.name("o/app", "Thing", branch="feat/x") == "app feat/x: Thing"
    assert len(conversation.name("o/app", "x" * 300, issue=4)) == 128


def test_add_card_is_idempotent_and_a_no_op_without_a_reference():
    conversation.add_card("nope", "c1")
    assert conversation.load("nope") is None
    conversation.update("led1", state="open", thread_id="5", cards=[])
    conversation.add_card("led1", "c1")
    conversation.add_card("led1", "c1")
    assert conversation.load("led1")["cards"] == ["c1"]


def test_finish_closes_only_an_open_topic():
    conversation.update("led1", state="open", thread_id="5")
    conversation.finish("led1")
    assert conversation.load("led1")["state"] == "closing"
    conversation.update("led2", state="fallback")
    conversation.finish("led2")
    assert conversation.load("led2")["state"] == "fallback"
    conversation.finish("nope")
    assert conversation.load("nope") is None


def test_active_skips_closed_and_settled_fallbacks():
    conversation.update("a", state="open", thread_id="1")
    conversation.update("b", state="closed", thread_id="2")
    conversation.update("c", state="fallback", noticed=True, previous=[])
    conversation.update("d", state="fallback", noticed=False)
    conversation.update("e", state="fallback", noticed=True, previous=["3"], repaired="main")
    conversation.update("f", state="fallback", noticed=True, previous=["3"])
    (config.data_dir() / "runs" / "g").mkdir(parents=True)
    (config.data_dir() / "runs" / "g" / "conversation.json").write_text("{not json")
    assert sorted(r["ledger"] for r in conversation.active()) == ["a", "d", "f"]
