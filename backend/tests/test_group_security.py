"""Group authorization: only the administrator controls groups and membership."""

import pytest

from apps.conversations.services import private_conversation, send_message
from tests.conftest import authed, client_id


@pytest.mark.django_db
def test_member_cannot_modify_group(group_thread, member_a):
    assert authed(member_a).patch(f"/api/groups/{group_thread.id}/", {"name": "Hacked"}, format="json").status_code == 403


@pytest.mark.django_db
def test_member_cannot_add_members_to_group(group_thread, member_a, member_b):
    response = authed(member_a).post(
        f"/api/groups/{group_thread.id}/members/", {"member_ids": [str(member_b.id)]}, format="json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_member_cannot_see_group_they_do_not_belong_to(group_thread, member_b):
    listing = authed(member_b).get("/api/groups/").json()["data"]["results"]
    assert listing == []
    assert authed(member_b).get(f"/api/groups/{group_thread.id}/").status_code == 404


@pytest.mark.django_db
def test_member_cannot_read_group_conversation_they_are_not_in(group_thread, member_b):
    response = authed(member_b).get(f"/api/conversations/{group_thread.conversation_id}/messages/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_reply_target_must_belong_to_the_same_conversation(admin, member_a, member_b, group_thread):
    private, _ = private_conversation(admin, member_b)
    other, _ = send_message(user=admin, conversation=private, client_id=client_id(), text="private")
    with pytest.raises(Exception):
        send_message(
            user=member_a,
            conversation=group_thread.conversation,
            client_id=client_id(),
            text="bad reply",
            reply_to=other,
        )


@pytest.mark.django_db
def test_member_cannot_react_when_group_forbids_it(group_thread, admin, member_a):
    group_thread.members_can_react = False
    group_thread.save(update_fields=["members_can_react"])
    message, _ = send_message(
        user=admin, conversation=group_thread.conversation, client_id=client_id(), text="hello"
    )
    response = authed(member_a).post(
        f"/api/messages/{message.id}/reactions/", {"reaction": "LIKE"}, format="json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_member_cannot_send_when_group_is_read_only(group_thread, member_a):
    group_thread.members_can_send = False
    group_thread.save(update_fields=["members_can_send"])
    response = authed(member_a).post(
        f"/api/conversations/{group_thread.conversation_id}/messages/",
        {"client_id": client_id(), "text": "hi"},
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_creates_group_and_members_receive_it(admin, member_a):
    response = authed(admin).post(
        "/api/groups/", {"name": "Ops", "member_ids": [str(member_a.id)]}, format="json"
    )
    assert response.status_code == 201, response.data
    group_id = response.json()["data"]["id"]

    listing = authed(member_a).get("/api/groups/").json()["data"]["results"]
    assert [g["id"] for g in listing] == [group_id]
