import pytest

from app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("enrollment_app.db.DB_PATH", test_db)
    monkeypatch.setattr("enrollment_app.db.INSTANCE_DIR", tmp_path)
    app.config.update(TESTING=True, SECRET_KEY="test")
    with app.test_client() as client:
        from enrollment_app.app import initialize_database, seed_data

        initialize_database()
        seed_data()
        yield client


def login(client, email, password):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


def create_paid_enrollment(client, batch_id=1):
    login(client, "client@maritimeenroll.test", "client123")
    enroll = client.post(
        f"/enroll/{batch_id}",
        data={
            "emergency_contact_name": "Ana Santos",
            "emergency_contact_phone": "0917-000-0000",
            "notes": "None",
        },
        follow_redirects=False,
    )
    client.post(enroll.headers["Location"], data={"action": "gcash"}, follow_redirects=True)
    client.get("/logout")


def test_home_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Basic Training" in response.data
    assert b"Member Access" in response.data


def test_logged_in_root_redirects_to_role_dashboard(client):
    login(client, "cashier@maritimeenroll.test", "cashier123")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/cashier/dashboard")


def test_client_has_separate_browse_classes_page(client):
    login(client, "client@maritimeenroll.test", "client123")
    response = client.get("/client/classes")
    assert response.status_code == 200
    assert b"Browse available classes" in response.data
    assert b"Basic Training" in response.data


def test_client_failed_payment_enrollment_can_be_updated(client):
    login(client, "client@maritimeenroll.test", "client123")
    enroll = client.post(
        "/enroll/1",
        data={
            "emergency_contact_name": "Ana Santos",
            "emergency_contact_phone": "0917-000-0000",
            "notes": "Original",
        },
        follow_redirects=False,
    )
    client.post(enroll.headers["Location"], data={"action": "fail"}, follow_redirects=True)
    update = client.post(
        "/client/enrollments/1/edit",
        data={
            "emergency_contact_name": "Ana Maria Santos",
            "emergency_contact_phone": "0999-111-2222",
            "notes": "Updated after failure",
            "action": "save",
        },
        follow_redirects=True,
    )
    assert b"Enrollment details updated" in update.data
    dashboard = client.get("/client/dashboard")
    assert b"Update enrollment" in dashboard.data


def test_client_cannot_access_admin_users(client):
    login(client, "client@maritimeenroll.test", "client123")
    response = client.get("/admin/users")
    assert response.status_code == 403


def test_admin_can_create_staff_user(client):
    login(client, "admin@maritimeenroll.test", "admin123")
    response = client.post(
        "/admin/users",
        data={
            "full_name": "Registrar User",
            "email": "registrar@example.com",
            "phone": "555-2222",
            "password": "secret123",
            "role": "staff",
        },
        follow_redirects=True,
    )
    assert b"Employee account created" in response.data
    page = client.get("/admin/users")
    assert b"registrar@example.com" in page.data


def test_admin_can_create_cashier_user(client):
    login(client, "admin@maritimeenroll.test", "admin123")
    response = client.post(
        "/admin/users",
        data={
            "full_name": "Window Cashier",
            "email": "window.cashier@example.com",
            "phone": "555-6767",
            "password": "cashier123",
            "role": "cashier",
        },
        follow_redirects=True,
    )
    assert b"Employee account created" in response.data
    page = client.get("/admin/users")
    assert b"window.cashier@example.com" in page.data


def test_logged_in_user_is_redirected_away_from_login(client):
    login(client, "staff@maritimeenroll.test", "staff123")
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/staff/dashboard")


def test_guest_login_page_redirects_to_welcome(client):
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_staff_and_admin_use_dedicated_edit_pages(client):
    login(client, "staff@maritimeenroll.test", "staff123")
    assert client.get("/staff/courses/1/edit").status_code == 200
    assert client.get("/staff/batches/1/edit").status_code == 200
    assert client.get("/staff/instructors/1/edit").status_code == 200
    assert client.get("/staff/assignments/1/edit").status_code == 200
    client.get("/logout")
    login(client, "admin@maritimeenroll.test", "admin123")
    assert client.get("/admin/users/2/edit").status_code == 200


def test_staff_can_update_records_from_edit_pages(client):
    login(client, "staff@maritimeenroll.test", "staff123")
    course_update = client.post(
        "/staff/courses/1/edit",
        data={
            "code": "BT",
            "title": "Basic Training Updated",
            "description": "Updated description",
            "duration_days": "6",
            "price": "1850.00",
            "prerequisites": "Passport",
            "certificate_prefix": "BT",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert b"Course updated" in course_update.data

    batch_update = client.post(
        "/staff/batches/1/edit",
        data={
            "course_id": "1",
            "batch_code": "BT-2026-APR-Z",
            "venue": "Updated Center",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "seat_limit": "30",
            "status": "completed",
        },
        follow_redirects=True,
    )
    assert b"Class batch updated" in batch_update.data

    instructor_update = client.post(
        "/staff/instructors/1/edit",
        data={
            "full_name": "Capt. Daniel Reyes Updated",
            "email": "dreyes@example.com",
            "qualifications": "Basic Safety, Crowd Management, Advanced Firefighting",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert b"Instructor updated" in instructor_update.data

    assignment_update = client.post(
        "/staff/assignments/1/edit",
        data={"instructor_id": "1", "class_batch_id": "2", "role": "support"},
        follow_redirects=True,
    )
    assert b"Instructor assignment updated" in assignment_update.data

    page = client.get("/staff/instructors")
    assert b"Capt. Daniel Reyes Updated" in page.data
    assert b"BT-2026-APR-Z" in client.get("/staff/batches").data


def test_staff_can_delete_course_batch_instructor_and_assignment(client):
    login(client, "staff@maritimeenroll.test", "staff123")

    course_create = client.post(
        "/staff/courses",
        data={
            "code": "RAD",
            "title": "Radar Observer",
            "description": "Radar course",
            "duration_days": "2",
            "price": "500.00",
            "prerequisites": "",
            "certificate_prefix": "RAD",
        },
        follow_redirects=True,
    )
    assert b"Course added" in course_create.data
    delete_course = client.post("/staff/courses/4/delete", follow_redirects=True)
    assert b"Course deleted" in delete_course.data

    instructor_create = client.post(
        "/staff/instructors",
        data={
            "full_name": "Test Instructor",
            "email": "test.instructor@example.com",
            "qualifications": "Bridge watchkeeping",
        },
        follow_redirects=True,
    )
    assert b"Instructor added" in instructor_create.data
    delete_instructor = client.post("/staff/instructors/3/delete", follow_redirects=True)
    assert b"Instructor deleted" in delete_instructor.data

    assignment_create = client.post(
        "/staff/assignments",
        data={"class_batch_id": "1", "instructor_id": "2", "role": "support"},
        follow_redirects=True,
    )
    assert b"Instructor assigned to class batch" in assignment_create.data
    delete_assignment = client.post("/staff/assignments/4/delete", follow_redirects=True)
    assert b"Instructor assignment deleted" in delete_assignment.data

    batch_create = client.post(
        "/staff/batches",
        data={
            "course_id": "1",
            "batch_code": "BT-DELETE-ME",
            "venue": "Delete Hall",
            "start_date": "2026-06-01",
            "end_date": "2026-06-02",
            "seat_limit": "10",
            "status": "draft",
        },
        follow_redirects=True,
    )
    assert b"Class batch added" in batch_create.data
    delete_batch = client.post("/staff/batches/4/delete", follow_redirects=True)
    assert b"Class batch deleted" in delete_batch.data


def test_admin_can_update_user_from_edit_page(client):
    login(client, "admin@maritimeenroll.test", "admin123")
    response = client.post(
        "/admin/users/2/edit",
        data={
            "full_name": "Training Staff Updated",
            "email": "staff@maritimeenroll.test",
            "phone": "555-4444",
            "role": "staff",
            "active": "1",
            "password": "",
        },
        follow_redirects=True,
    )
    assert b"User updated" in response.data
    page = client.get("/admin/users")
    assert b"Training Staff Updated" in page.data


def test_admin_can_delete_other_users_but_not_self(client):
    login(client, "admin@maritimeenroll.test", "admin123")
    create_user = client.post(
        "/admin/users",
        data={
            "full_name": "Delete Cashier",
            "email": "delete.cashier@example.com",
            "phone": "555-9898",
            "password": "cashier123",
            "role": "cashier",
        },
        follow_redirects=True,
    )
    assert b"Employee account created" in create_user.data
    delete_user = client.post("/admin/users/5/delete", follow_redirects=True)
    assert b"User deleted" in delete_user.data

    self_delete = client.post("/admin/users/1/delete", follow_redirects=True)
    assert b"You cannot delete your own active admin account" in self_delete.data


def test_cashier_can_approve_over_the_counter_payment(client):
    login(client, "client@maritimeenroll.test", "client123")
    enroll = client.post(
        "/enroll/1",
        data={
            "emergency_contact_name": "Ana Santos",
            "emergency_contact_phone": "0917-000-0000",
            "notes": "OTC payment",
        },
        follow_redirects=False,
    )
    client.post(enroll.headers["Location"], data={"action": "otc"}, follow_redirects=True)
    client.get("/logout")

    login(client, "cashier@maritimeenroll.test", "cashier123")
    dashboard = client.get("/cashier/dashboard")
    assert b"Approve payment" in dashboard.data
    approve = client.post("/cashier/payments/1/approve", follow_redirects=True)
    assert b"Over-the-counter payment approved" in approve.data
    client.get("/logout")

    login(client, "client@maritimeenroll.test", "client123")
    dashboard = client.get("/client/dashboard")
    assert b"OTC" in dashboard.data


def test_certificate_requires_perfect_attendance_and_finished_batch(client):
    login(client, "staff@maritimeenroll.test", "staff123")
    client.post(
        "/staff/batches/1/edit",
        data={
            "course_id": "1",
            "batch_code": "BT-2026-APR-A",
            "venue": "Manila Training Center",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "seat_limit": "24",
            "status": "open",
        },
        follow_redirects=True,
    )
    client.get("/logout")

    create_paid_enrollment(client, batch_id=1)

    login(client, "client@maritimeenroll.test", "client123")
    attendance_page = client.get("/client/enrollments/1/attendance")
    assert attendance_page.status_code == 200
    attendance_update = client.post(
        "/client/enrollments/1/attendance",
        data={
            "attendance_1": "present",
            "attendance_2": "present",
            "attendance_3": "present",
        },
        follow_redirects=True,
    )
    assert b"Attendance updated" in attendance_update.data
    client.get("/logout")

    login(client, "staff@maritimeenroll.test", "staff123")
    client.post(
        "/staff/batches/1/edit",
        data={
            "course_id": "1",
            "batch_code": "BT-2026-APR-A",
            "venue": "Manila Training Center",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "seat_limit": "24",
            "status": "completed",
        },
        follow_redirects=True,
    )
    review = client.get("/staff/enrollments/1")
    assert b"Perfect attendance" in review.data
    approve = client.post("/staff/enrollments/1/certificate", follow_redirects=True)
    assert b"Certificate generated" in approve.data
    client.get("/logout")

    login(client, "client@maritimeenroll.test", "client123")
    dashboard = client.get("/client/dashboard")
    assert b"BT-" in dashboard.data
