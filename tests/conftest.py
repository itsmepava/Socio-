import pytest

from app import ActivityLog, Organization, Plan, User, create_app, db, password_hasher


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-only-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "WTF_CSRF_ENABLED": False,
        "RATELIMIT_ENABLED": False,
        "PUBLIC_BASE_URL": "http://127.0.0.1:5001",
        "DEV_EMAIL_PREVIEW": True,
    })
    with app.app_context():
        db.create_all()
        org_a = Organization(name="A Society", email="a@example.test", status="Approved")
        org_b = Organization(name="B Society", email="b@example.test", status="Approved")
        db.session.add_all([org_a, org_b])
        db.session.flush()
        users = [
            User(email="a-admin@example.test", password_hash=password_hasher.hash("correct horse battery"), role="organization_admin", organization_id=org_a.id),
            User(email="b-admin@example.test", password_hash=password_hasher.hash("correct horse battery"), role="organization_admin", organization_id=org_b.id),
            User(email="platform@example.test", password_hash=password_hasher.hash("correct horse battery"), role="platform_admin"),
            User(email="member@example.test", password_hash=password_hasher.hash("correct horse battery"), role="member", organization_id=org_a.id),
        ]
        db.session.add_all(users)
        db.session.flush()
        db.session.add(Plan(organization_id=org_b.id, name="B Plan", price_minor=50000, billing_period="monthly"))
        db.session.commit()
    yield app
    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login_as(client, email):
    with client.application.app_context():
        user = User.query.filter_by(email=email).first()
        user_id = user.id
        if not user.session_nonce:
            user.session_nonce = "test-session-nonce"
            db.session.commit()
        nonce = user.session_nonce
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_nonce"] = nonce
