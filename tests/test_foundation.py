from datetime import date, timedelta
import json
import re

from conftest import login_as

from app import (
    ActivityLog,
    EmailVerificationToken,
    Membership,
    MembershipApplication,
    Organization,
    OrganizationEvent,
    OrganizationOffer,
    Plan,
    SimulatedPayment,
    SimulationState,
    User,
    db,
)


def test_requires_secret_key():
    from app import create_app
    import pytest
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"SECRET_KEY": None})


def test_pwa_manifest_icons_and_root_scoped_service_worker_are_available(client):
    home = client.get("/")
    assert b'rel="manifest"' in home.data
    manifest_response = client.get("/static/manifest.webmanifest")
    manifest = json.loads(manifest_response.data)
    assert manifest["name"] == "Socio Membership"
    assert manifest["display"] == "standalone"
    assert client.get("/static/icons/socio-192.png").status_code == 200
    assert client.get("/static/icons/socio-512.png").status_code == 200
    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    assert worker.headers["Cache-Control"] == "no-cache"
    assert worker.headers["Service-Worker-Allowed"] == "/"
    assert b"/static/" in worker.data
    assert b"request.method !== \"GET\"" in worker.data


def test_login_and_session_cookie_security(client):
    response = client.post("/login", data={"email": "a-admin@example.test", "password": "correct horse battery"})
    assert response.status_code == 302
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_logout_revokes_existing_session(app, client):
    client.post("/login", data={"email":"a-admin@example.test", "password":"correct horse battery"})
    with client.session_transaction() as session:
        stolen_values = dict(session)
    assert client.post("/logout").status_code == 302
    replay = app.test_client()
    with replay.session_transaction() as session:
        session.update(stolen_values)
    assert replay.get("/organization/plans").status_code == 302


def test_bad_login_does_not_disclose_account(client):
    a = client.post("/login", data={"email": "missing@example.test", "password": "wrong"})
    b = client.post("/login", data={"email": "a-admin@example.test", "password": "wrong"})
    assert b"Email or password is incorrect." in a.data
    assert b"Email or password is incorrect." in b.data


def test_duplicate_member_email_is_scoped_by_organization(app, client):
    with app.app_context():
        org_b = Organization.query.filter_by(email="b@example.test").one()
        db.session.add(User(email="member@example.test", password_hash=User.query.filter_by(email="member@example.test").one().password_hash,
                            role="member", organization_id=org_b.id))
        db.session.commit()
    ambiguous = client.post("/login", data={"email":"member@example.test", "password":"correct horse battery"})
    assert b"Email or password is incorrect." in ambiguous.data
    selected = client.post("/login", data={"email":"member@example.test", "organization_email":"b@example.test", "password":"correct horse battery"})
    assert selected.status_code == 302
    with client.session_transaction() as session:
        member_id = session["user_id"]
    with app.app_context():
        assert db.session.get(User, member_id).organization.email == "b@example.test"


def test_member_cannot_open_platform_admin(client):
    login_as(client, "member@example.test")
    assert client.get("/admin/organizations").status_code == 403


def test_organization_admin_cannot_open_platform_admin(client):
    login_as(client, "a-admin@example.test")
    assert client.get("/admin/organizations").status_code == 403


def test_platform_admin_can_suspend_and_action_is_logged(app, client):
    login_as(client, "platform@example.test")
    response = client.post("/admin/organizations/1/suspend")
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Organization, 1).status == "Suspended"
        log = ActivityLog.query.filter_by(action="organization_suspend").one()
        assert log.actor_user_id == User.query.filter_by(email="platform@example.test").one().id


def test_platform_admin_can_approve_pending_organization(app, client):
    with app.app_context():
        org = Organization(name="Pending Society", email="awaiting@example.test", status="Pending")
        db.session.add(org)
        db.session.commit()
        org_id = org.id
    login_as(client, "platform@example.test")
    assert client.post(f"/admin/organizations/{org_id}/approve").status_code == 302
    with app.app_context():
        assert db.session.get(Organization, org_id).status == "Approved"


def test_invalid_organization_status_transition_is_rejected(app, client):
    login_as(client, "platform@example.test")
    assert client.post("/admin/organizations/1/reinstate").status_code == 409
    with app.app_context():
        assert db.session.get(Organization, 1).status == "Approved"


def test_organization_admin_sees_only_own_plans(client):
    login_as(client, "a-admin@example.test")
    response = client.get("/organization/plans")
    assert response.status_code == 200
    assert b"B Plan" not in response.data


def test_organization_admin_cannot_archive_another_organizations_plan(client):
    login_as(client, "a-admin@example.test")
    assert client.post("/organization/plans/1/archive").status_code == 404


def test_unapproved_organization_cannot_manage_plans(app, client):
    with app.app_context():
        org = Organization(name="Pending Society", email="pending@example.test", status="Pending")
        db.session.add(org)
        db.session.flush()
        user = User(email="pending-admin@example.test", password_hash="x", role="organization_admin", organization_id=org.id)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    login_as(client, "pending-admin@example.test")
    assert client.get("/organization/plans").status_code == 200
    assert client.post("/organization/plans", data={"name":"Blocked","price_minor":"100","billing_period":"monthly"}).status_code == 403


def test_plan_input_rejects_invalid_amount_and_period(client):
    login_as(client, "a-admin@example.test")
    response = client.post("/organization/plans", data={"name":"Plan", "price_minor":"1.5", "billing_period":"monthly"})
    assert response.status_code == 200
    assert b"Price must be a non-negative whole number" in response.data
    response = client.post("/organization/plans", data={"name":"Plan", "price_minor":"100", "billing_period":"weekly"})
    assert b"valid plan name" in response.data


def test_csrf_rejects_state_change_without_token(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    login_as(client, "platform@example.test")
    assert client.post("/admin/organizations/1/approve").status_code == 400


def test_archiving_keeps_record_and_marks_it_unavailable(app, client):
    login_as(client, "a-admin@example.test")
    client.post("/organization/plans", data={"name":"Annual", "price_minor":"10000", "billing_period":"yearly"})
    with app.app_context():
        plan = Plan.query.filter_by(name="Annual").one()
        plan_id = plan.id
    client.post(f"/organization/plans/{plan_id}/archive")
    with app.app_context():
        assert db.session.get(Plan, plan_id).archived is True


def test_registration_email_confirmation_is_hashed_and_single_use(app, client, caplog):
    response = client.post("/organizations/register", data={
        "name":"New Society", "organization_email":"new-org@example.test",
        "admin_name":"New Admin", "email":"new-admin@example.test",
        "password":"a very long test password",
    })
    assert response.status_code == 200
    assert b"Local confirmation link ready" in response.data
    with app.app_context():
        user = User.query.filter_by(email="new-admin@example.test").one()
        assert user.password_hash != "a very long test password"
        assert user.role == "organization_admin"
        assert user.email_verified is False
        verification = EmailVerificationToken.query.filter_by(user_id=user.id).one()
        assert len(verification.token_hash) == 64
        assert ActivityLog.query.filter_by(action="organization_registered").count() == 1
    raw_token = re.search(r"http://127\.0\.0\.1:5001/email/confirm/([A-Za-z0-9_-]+)", caplog.text)
    assert raw_token
    confirmation_url = f"/email/confirm/{raw_token.group(1)}"
    assert client.get(confirmation_url).status_code == 200
    assert client.post(confirmation_url).status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="new-admin@example.test").one()
        verification = EmailVerificationToken.query.filter_by(user_id=user.id).one()
        assert user.email_verified is True
        assert verification.used_at is not None
    assert client.post(confirmation_url).status_code == 302


def test_member_application_review_and_simulated_payment_are_persisted(app, client):
    with app.app_context():
        organization = Organization.query.filter_by(email="a@example.test").one()
        plan = Plan(organization_id=organization.id, name="Community", price_minor=120000,
                    billing_period="monthly", benefits="Monthly club access")
        db.session.add(plan)
        db.session.commit()
        organization_id, plan_id = organization.id, plan.id

    login_as(client, "member@example.test")
    response = client.post(f"/join/{organization_id}", data={
        "plan_id": str(plan_id), "recommendation_text": "I am a current community supporter."
    })
    assert response.status_code == 302
    with app.app_context():
        membership = Membership.query.filter_by(organization_id=organization_id).one()
        application = MembershipApplication.query.filter_by(membership_id=membership.id).one()
        assert membership.status == "Pending"
        assert application.status == "Pending"
        membership_id, application_id = membership.id, application.id

    organization_admin = app.test_client()
    login_as(organization_admin, "a-admin@example.test")
    assert organization_admin.post(
        f"/organization/applications/{application_id}/approve",
        data={"review_note": "Approved for the demo."},
    ).status_code == 302
    with app.app_context():
        payment = SimulatedPayment.query.filter_by(membership_id=membership_id).one()
        assert payment.status == "Pending"
        payment_id = payment.id

    review = client.get(f"/payments/{payment_id}/review")
    assert review.status_code == 200
    assert client.post(f"/payments/{payment_id}/review", data={"confirm_terms": "yes"}).status_code == 302
    assert client.get(f"/simulated-bank/{payment_id}").status_code == 200
    assert client.post(f"/simulated-bank/{payment_id}/success").status_code == 302
    with app.app_context():
        membership = db.session.get(Membership, membership_id)
        payment = db.session.get(SimulatedPayment, payment_id)
        assert membership.status == "Active"
        assert membership.end_day == 30
        assert payment.status == "Paid"


def test_organization_can_publish_offer_and_event_and_members_can_open_details(app, client):
    login_as(client, "a-admin@example.test")
    assert client.post("/organization/offers", data={
        "title": "Open training session", "description": "A complete offer description for members.",
        "offer_type": "Extra benefit", "start_day": "0", "end_day": "14", "plan_id": "",
    }).status_code == 302
    assert client.post("/organization/events", data={
        "title": "Community open day", "description": "Full details about the upcoming open day.",
        "venue": "Main club hall", "event_date": (date.today() + timedelta(days=2)).isoformat(),
    }).status_code == 302
    with app.app_context():
        organization = Organization.query.filter_by(email="a@example.test").one()
        offer = OrganizationOffer.query.filter_by(organization_id=organization.id).one()
        event = OrganizationEvent.query.filter_by(organization_id=organization.id).one()
        offer_id, event_id, organization_id = offer.id, event.id, organization.id

    visitor = app.test_client()
    offer_page = visitor.get(f"/organizations/{organization_id}/offers/{offer_id}")
    event_page = visitor.get(f"/organizations/{organization_id}/events/{event_id}")
    assert offer_page.status_code == 200
    assert b"A complete offer description for members." in offer_page.data
    assert event_page.status_code == 200
    assert b"Full details about the upcoming open day." in event_page.data
    gallery = visitor.get("/members")
    assert b"View offer details" in gallery.data
    assert b"View event details" in gallery.data

    assert client.post(f"/organization/offers/{offer_id}/archive").status_code == 302
    assert client.post(f"/organization/events/{event_id}/archive").status_code == 302
    assert visitor.get(f"/organizations/{organization_id}/offers/{offer_id}").status_code == 404
    assert visitor.get(f"/organizations/{organization_id}/events/{event_id}").status_code == 404


def test_monthly_renewals_preserve_tenure_and_trigger_loyalty_gift(app):
    with app.app_context():
        organization = Organization.query.filter_by(email="a@example.test").one()
        organization.loyalty_reward_enabled = True
        organization.loyalty_reward_after_days = 60
        organization.loyalty_reward_days = 7
        member = User.query.filter_by(email="member@example.test").one()
        plan = Plan(organization_id=organization.id, name="Monthly Supporter", price_minor=10000,
                    billing_period="monthly")
        db.session.add(plan)
        db.session.flush()
        membership = Membership(organization_id=organization.id, user_id=member.id, plan_id=plan.id,
                                full_name="Demo Member", status="Active", start_day=0, end_day=30)
        db.session.add_all([membership, SimulationState(id=1, current_day=0)])
        db.session.commit()
        membership_id = membership.id

    platform_admin = app.test_client()
    login_as(platform_admin, "platform@example.test")
    for _ in range(2):
        assert platform_admin.post("/admin/simulation/advance", data={"days": "15"}).status_code == 302
    assert platform_admin.post("/admin/simulation/advance", data={"days": "2"}).status_code == 400
    with app.app_context():
        membership = db.session.get(Membership, membership_id)
        renewal = SimulatedPayment.query.filter_by(membership_id=membership_id, status="Pending").one()
        assert membership.start_day == 0
        renewal_id = renewal.id

    member_client = app.test_client()
    login_as(member_client, "member@example.test")
    assert member_client.post(f"/member/{membership_id}/retry").status_code == 302
    assert member_client.post(f"/payments/{renewal_id}/review", data={"confirm_terms": "yes"}).status_code == 302
    assert member_client.post(f"/simulated-bank/{renewal_id}/success").status_code == 302
    with app.app_context():
        membership = db.session.get(Membership, membership_id)
        assert membership.start_day == 0
        assert membership.end_day == 60

    for _ in range(2):
        platform_admin.post("/admin/simulation/advance", data={"days": "15"})
    with app.app_context():
        membership = db.session.get(Membership, membership_id)
        assert membership.start_day == 0
        assert membership.end_day == 67
        assert membership.loyalty_reward_days == 7
        assert membership.loyalty_rewarded_day == 60
        assert SimulatedPayment.query.filter_by(membership_id=membership_id, status="Pending").count() == 0
