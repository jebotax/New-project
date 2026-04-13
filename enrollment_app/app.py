from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .certificates import build_certificate_pdf
from .db import BASE_DIR, get_connection, initialize_database


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="pbkdf2:sha256")


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))
    app.config["SECRET_KEY"] = "dev-secret-key"
    app.config["CERTIFICATE_DIR"] = BASE_DIR / "instance" / "certificates"

    initialize_database()
    seed_data()

    @app.before_request
    def load_current_user() -> None:
        user_id = session.get("user_id")
        g.user = None
        if user_id:
            with get_connection() as connection:
                g.user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    @app.after_request
    def disable_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.context_processor
    def inject_globals():
        return {"current_year": datetime.utcnow().year, "target_dashboard": target_dashboard}

    @app.route("/")
    def home():
        if g.user:
            return redirect(target_dashboard(g.user["role"]))
        return render_template("home.html", courses=get_catalog_courses(), public_view=True)

    @app.route("/client/classes")
    @login_required("client")
    def client_classes():
        return render_template("client_classes.html", courses=get_catalog_courses())

    def get_catalog_courses():
        with get_connection() as connection:
            return connection.execute(
                """
                SELECT c.*, MIN(cb.start_date) AS next_start, COUNT(cb.id) AS open_batches
                FROM courses c
                LEFT JOIN class_batches cb ON cb.course_id = c.id AND cb.status = 'open'
                WHERE c.is_active = 1
                GROUP BY c.id
                ORDER BY c.title
                """
            ).fetchall()

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(target_dashboard(g.user["role"]))
        if request.method == "POST":
            form = request.form
            with get_connection() as connection:
                existing = connection.execute("SELECT id FROM users WHERE email = ?", (form["email"].strip().lower(),)).fetchone()
                if existing:
                    flash("Email address is already registered.", "error")
                else:
                    connection.execute(
                        """
                        INSERT INTO users (full_name, email, password_hash, role, phone)
                        VALUES (?, ?, ?, 'client', ?)
                        """,
                        (
                            form["full_name"].strip(),
                            form["email"].strip().lower(),
                            hash_password(form["password"]),
                            form.get("phone", "").strip(),
                        ),
                    )
                    connection.commit()
                    flash("Account created. Please sign in.", "success")
                    return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(target_dashboard(g.user["role"]))
        if request.method == "GET":
            return redirect(url_for("home"))
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            with get_connection() as connection:
                user = connection.execute("SELECT * FROM users WHERE email = ? AND active = 1", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                flash("Signed in successfully.", "success")
                return redirect(target_dashboard(user["role"]))
            flash("Invalid credentials.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("home"))

    @app.route("/courses/<int:course_id>")
    def course_detail(course_id: int):
        with get_connection() as connection:
            course = connection.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
            if not course:
                abort(404)
            batches = connection.execute(
                """
                SELECT cb.*,
                       (
                           SELECT COUNT(*)
                           FROM enrollments e
                           WHERE e.class_batch_id = cb.id
                             AND e.status IN ('pending_payment', 'confirmed', 'completed')
                       ) AS seats_taken
                FROM class_batches cb
                WHERE cb.course_id = ?
                ORDER BY cb.start_date
                """,
                (course_id,),
            ).fetchall()
        return render_template("course_detail.html", course=course, batches=batches)

    @app.route("/client/dashboard")
    @login_required("client")
    def client_dashboard():
        with get_connection() as connection:
            enrollments = connection.execute(
                """
                SELECT e.*, c.title AS course_title, cb.batch_code, cb.start_date, cb.end_date, cb.status AS batch_status,
                       p.status AS payment_state, p.payment_method, cert.certificate_number
                FROM enrollments e
                JOIN class_batches cb ON cb.id = e.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                LEFT JOIN payments p ON p.enrollment_id = e.id
                LEFT JOIN certificates cert ON cert.enrollment_id = e.id
                WHERE e.user_id = ?
                ORDER BY e.created_at DESC
                """,
                (g.user["id"],),
            ).fetchall()
            attendance = {row["enrollment_id"]: dict(row) for row in connection.execute(attendance_summary_query(), (g.user["id"],)).fetchall()}
        return render_template("client_dashboard.html", enrollments=enrollments, attendance=attendance)

    @app.route("/enroll/<int:batch_id>", methods=["GET", "POST"])
    @login_required("client")
    def enroll(batch_id: int):
        with get_connection() as connection:
            batch = get_batch_with_course(connection, batch_id)
            if not batch:
                abort(404)
            if request.method == "POST":
                if batch["status"] != "open" or batch["seats_taken"] >= batch["seat_limit"]:
                    flash("This class batch is no longer available.", "error")
                    return redirect(url_for("course_detail", course_id=batch["course_id"]))

                existing = connection.execute(
                    "SELECT id FROM enrollments WHERE user_id = ? AND class_batch_id = ?",
                    (g.user["id"], batch_id),
                ).fetchone()
                if existing:
                    flash("You are already enrolled in this class batch.", "error")
                    return redirect(url_for("client_dashboard"))

                cursor = connection.execute(
                    """
                    INSERT INTO enrollments (
                        user_id, class_batch_id, status, approval_status, payment_status,
                        emergency_contact_name, emergency_contact_phone, notes
                    ) VALUES (?, ?, 'pending_payment', 'pending', 'pending', ?, ?, ?)
                    """,
                    (
                        g.user["id"],
                        batch_id,
                        request.form["emergency_contact_name"].strip(),
                        request.form["emergency_contact_phone"].strip(),
                        request.form.get("notes", "").strip(),
                    ),
                )
                enrollment_id = cursor.lastrowid
                create_payment_session(connection, enrollment_id, batch["price_cents"])
                connection.commit()
                return redirect(url_for("checkout", enrollment_id=enrollment_id))
        return render_template("enroll.html", batch=batch)

    @app.route("/client/enrollments/<int:enrollment_id>/edit", methods=["GET", "POST"])
    @login_required("client")
    def edit_client_enrollment(enrollment_id: int):
        with get_connection() as connection:
            enrollment = get_client_enrollment_detail(connection, enrollment_id, g.user["id"])
            if not enrollment:
                abort(404)
            if request.method == "POST":
                connection.execute(
                    """
                    UPDATE enrollments
                    SET emergency_contact_name = ?, emergency_contact_phone = ?, notes = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        request.form["emergency_contact_name"].strip(),
                        request.form["emergency_contact_phone"].strip(),
                        request.form.get("notes", "").strip(),
                        enrollment_id,
                        g.user["id"],
                    ),
                )
                connection.commit()
                flash("Enrollment details updated.", "success")
                if request.form.get("action") == "retry":
                    return redirect(url_for("checkout", enrollment_id=enrollment_id))
                return redirect(url_for("client_dashboard"))
        return render_template("client_enrollment_edit.html", enrollment=enrollment)

    @app.route("/client/enrollments/<int:enrollment_id>/attendance", methods=["GET", "POST"])
    @login_required("client")
    def client_attendance(enrollment_id: int):
        with get_connection() as connection:
            enrollment = get_client_enrollment_detail(connection, enrollment_id, g.user["id"])
            if not enrollment:
                abort(404)
            if enrollment["payment_status"] != "paid":
                flash("Attendance is available after payment confirmation.", "error")
                return redirect(url_for("client_dashboard"))
            sync_attendance_records(connection, enrollment)
            records = connection.execute(
                "SELECT * FROM attendance_records WHERE enrollment_id = ? ORDER BY session_date",
                (enrollment_id,),
            ).fetchall()
            if request.method == "POST":
                today = date.today().isoformat()
                for record in records:
                    status = "present" if request.form.get(f"attendance_{record['id']}") == "present" else "absent"
                    if record["session_date"] <= today:
                        connection.execute(
                            "UPDATE attendance_records SET status = ?, marked_by_client = 1 WHERE id = ?",
                            (status, record["id"]),
                        )
                connection.commit()
                flash("Attendance updated.", "success")
                return redirect(url_for("client_attendance", enrollment_id=enrollment_id))
            summary = get_attendance_summary(connection, enrollment_id)
        return render_template("client_attendance.html", enrollment=enrollment, records=records, summary=summary, today=date.today().isoformat())

    @app.route("/checkout/<int:enrollment_id>", methods=["GET", "POST"])
    @login_required("client")
    def checkout(enrollment_id: int):
        with get_connection() as connection:
            enrollment = get_enrollment(connection, enrollment_id, g.user["id"])
            if not enrollment:
                abort(404)
            if request.method == "POST":
                action = request.form["action"]
                if action == "gcash":
                    confirm_payment(connection, enrollment_id, "gcash")
                    flash("GCash payment successful. Enrollment confirmed.", "success")
                elif action == "otc":
                    submit_otc_payment(connection, enrollment_id)
                    flash("Over-the-counter payment submitted. Waiting for cashier approval.", "success")
                else:
                    fail_payment(connection, enrollment_id)
                    flash("Payment failed. Update your enrollment and try again.", "error")
                return redirect(url_for("client_dashboard"))
        return render_template("checkout.html", enrollment=enrollment)

    @app.route("/staff/dashboard")
    @login_required("staff", "admin")
    def staff_dashboard():
        with get_connection() as connection:
            metrics = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM courses WHERE is_active = 1) AS active_courses,
                    (SELECT COUNT(*) FROM class_batches WHERE status = 'open') AS open_batches,
                    (SELECT COUNT(*) FROM enrollments WHERE status = 'confirmed') AS confirmed_enrollments,
                    (SELECT COUNT(*) FROM certificates) AS certificates_issued
                """
            ).fetchone()
            batches = connection.execute(
                """
                SELECT cb.*, c.title AS course_title,
                       (
                           SELECT COUNT(*)
                           FROM enrollments e
                           WHERE e.class_batch_id = cb.id
                             AND e.status IN ('pending_payment', 'confirmed', 'completed')
                       ) AS seats_taken
                FROM class_batches cb
                JOIN courses c ON c.id = cb.course_id
                ORDER BY cb.start_date
                LIMIT 8
                """
            ).fetchall()
            recent_enrollments = connection.execute(
                """
                SELECT e.id, u.full_name, c.title AS course_title, cb.batch_code,
                       e.status, e.payment_status, e.certificate_eligible
                FROM enrollments e
                JOIN users u ON u.id = e.user_id
                JOIN class_batches cb ON cb.id = e.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                ORDER BY e.created_at DESC
                LIMIT 10
                """
            ).fetchall()
        return render_template("staff_dashboard.html", metrics=metrics, batches=batches, recent_enrollments=recent_enrollments)

    @app.route("/cashier/dashboard")
    @login_required("cashier")
    def cashier_dashboard():
        with get_connection() as connection:
            metrics = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM payments WHERE payment_method = 'otc' AND status = 'pending') AS pending_otc,
                    (SELECT COUNT(*) FROM payments WHERE payment_method = 'otc' AND status = 'succeeded') AS approved_otc,
                    (SELECT COUNT(*) FROM payments WHERE payment_method = 'gcash' AND status = 'succeeded') AS gcash_paid
                """
            ).fetchone()
            payments = connection.execute(
                """
                SELECT p.id, p.payment_method, p.status, p.amount_cents, p.provider_reference, p.created_at,
                       u.full_name, c.title AS course_title, cb.batch_code
                FROM payments p
                JOIN enrollments e ON e.id = p.enrollment_id
                JOIN users u ON u.id = e.user_id
                JOIN class_batches cb ON cb.id = e.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                WHERE p.payment_method = 'otc'
                ORDER BY p.created_at DESC
                """
            ).fetchall()
        return render_template("cashier_dashboard.html", metrics=metrics, payments=payments)

    @app.route("/cashier/payments/<int:payment_id>/approve", methods=["POST"])
    @login_required("cashier")
    def approve_otc_payment(payment_id: int):
        with get_connection() as connection:
            payment = connection.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if not payment:
                abort(404)
            confirm_payment(connection, payment["enrollment_id"], "otc", approved_by_user_id=g.user["id"])
        flash("Over-the-counter payment approved.", "success")
        return redirect(url_for("cashier_dashboard"))

    @app.route("/staff/enrollments")
    @login_required("staff", "admin")
    def staff_enrollments():
        with get_connection() as connection:
            enrollments = connection.execute(
                """
                SELECT e.*, u.full_name, u.email, c.title AS course_title, cb.batch_code, cb.end_date, cb.status AS batch_status,
                       p.provider_reference, p.status AS payment_state, cert.certificate_number
                FROM enrollments e
                JOIN users u ON u.id = e.user_id
                JOIN class_batches cb ON cb.id = e.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                LEFT JOIN payments p ON p.enrollment_id = e.id
                LEFT JOIN certificates cert ON cert.enrollment_id = e.id
                ORDER BY e.created_at DESC
                """
            ).fetchall()
            attendance = {row["enrollment_id"]: dict(row) for row in connection.execute(staff_attendance_summary_query()).fetchall()}
        return render_template("staff_enrollments.html", enrollments=enrollments, attendance=attendance, is_batch_finished=is_batch_finished)

    @app.route("/staff/enrollments/<int:enrollment_id>")
    @login_required("staff", "admin")
    def review_enrollment(enrollment_id: int):
        with get_connection() as connection:
            enrollment = get_staff_enrollment_detail(connection, enrollment_id)
            if not enrollment:
                abort(404)
            sync_attendance_records(connection, enrollment)
            records = connection.execute(
                "SELECT * FROM attendance_records WHERE enrollment_id = ? ORDER BY session_date",
                (enrollment_id,),
            ).fetchall()
            summary = get_attendance_summary(connection, enrollment_id)
            can_issue = can_issue_certificate(enrollment, summary)
        return render_template("staff_enrollment_review.html", enrollment=enrollment, records=records, summary=summary, can_issue=can_issue, batch_finished=is_batch_finished(enrollment))

    @app.route("/staff/enrollments/<int:enrollment_id>/certificate", methods=["POST"])
    @login_required("staff", "admin")
    def approve_certificate(enrollment_id: int):
        with get_connection() as connection:
            enrollment = get_staff_enrollment_detail(connection, enrollment_id)
            if not enrollment:
                abort(404)
            sync_attendance_records(connection, enrollment)
            summary = get_attendance_summary(connection, enrollment_id)
            if not can_issue_certificate(enrollment, summary):
                flash("Certificate cannot be issued until payment is complete, attendance is perfect, and the batch is finished.", "error")
                return redirect(url_for("review_enrollment", enrollment_id=enrollment_id))
            issue_certificate(connection, app, enrollment)
            connection.execute("UPDATE enrollments SET certificate_eligible = 1, status = 'completed' WHERE id = ?", (enrollment_id,))
            connection.commit()
        flash("Certificate generated and added to the client dashboard.", "success")
        return redirect(url_for("review_enrollment", enrollment_id=enrollment_id))

    @app.route("/certificates/<int:enrollment_id>")
    @login_required("client", "staff", "admin", "cashier")
    def download_certificate(enrollment_id: int):
        with get_connection() as connection:
            certificate = connection.execute(
                """
                SELECT cert.file_path, e.user_id
                FROM certificates cert
                JOIN enrollments e ON e.id = cert.enrollment_id
                WHERE cert.enrollment_id = ?
                """,
                (enrollment_id,),
            ).fetchone()
            if not certificate:
                abort(404)
            if g.user["role"] == "client" and certificate["user_id"] != g.user["id"]:
                abort(403)
        return send_file(certificate["file_path"], as_attachment=True)

    @app.route("/staff/courses", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def manage_courses():
        if request.method == "POST":
            with get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO courses (code, title, description, duration_days, price_cents, prerequisites, certificate_prefix)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.form["code"].strip().upper(),
                        request.form["title"].strip(),
                        request.form["description"].strip(),
                        int(request.form["duration_days"]),
                        int(float(request.form["price"]) * 100),
                        request.form.get("prerequisites", "").strip(),
                        request.form["certificate_prefix"].strip().upper(),
                    ),
                )
                connection.commit()
            flash("Course added.", "success")
            return redirect(url_for("manage_courses"))
        with get_connection() as connection:
            courses = connection.execute("SELECT * FROM courses ORDER BY title").fetchall()
        return render_template("staff_courses.html", courses=courses)

    @app.route("/staff/courses/<int:course_id>/edit", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def edit_course(course_id: int):
        with get_connection() as connection:
            course = connection.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
            if not course:
                abort(404)
            if request.method == "POST":
                connection.execute(
                    """
                    UPDATE courses
                    SET code = ?, title = ?, description = ?, duration_days = ?, price_cents = ?,
                        prerequisites = ?, certificate_prefix = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (
                        request.form["code"].strip().upper(),
                        request.form["title"].strip(),
                        request.form["description"].strip(),
                        int(request.form["duration_days"]),
                        int(float(request.form["price"]) * 100),
                        request.form.get("prerequisites", "").strip(),
                        request.form["certificate_prefix"].strip().upper(),
                        1 if request.form.get("is_active") == "1" else 0,
                        course_id,
                    ),
                )
                connection.commit()
                flash("Course updated.", "success")
                return redirect(url_for("manage_courses"))
        return render_template("course_edit.html", course=course)

    @app.route("/staff/courses/<int:course_id>/delete", methods=["POST"])
    @login_required("staff", "admin")
    def delete_course(course_id: int):
        with get_connection() as connection:
            connection.execute("DELETE FROM courses WHERE id = ?", (course_id,))
            connection.commit()
        flash("Course deleted.", "success")
        return redirect(url_for("manage_courses"))

    @app.route("/staff/batches", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def manage_batches():
        with get_connection() as connection:
            if request.method == "POST":
                connection.execute(
                    """
                    INSERT INTO class_batches (course_id, batch_code, venue, start_date, end_date, seat_limit, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(request.form["course_id"]),
                        request.form["batch_code"].strip().upper(),
                        request.form["venue"].strip(),
                        request.form["start_date"],
                        request.form["end_date"],
                        int(request.form["seat_limit"]),
                        request.form["status"],
                    ),
                )
                connection.commit()
                flash("Class batch added.", "success")
                return redirect(url_for("manage_batches"))
            courses = connection.execute("SELECT id, title FROM courses WHERE is_active = 1 ORDER BY title").fetchall()
            batches = connection.execute(
                """
                SELECT cb.*, c.title AS course_title
                FROM class_batches cb
                JOIN courses c ON c.id = cb.course_id
                ORDER BY cb.start_date DESC
                """
            ).fetchall()
        return render_template("staff_batches.html", batches=batches, courses=courses)

    @app.route("/staff/batches/<int:batch_id>/edit", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def edit_batch(batch_id: int):
        with get_connection() as connection:
            batch = connection.execute("SELECT * FROM class_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch:
                abort(404)
            courses = connection.execute("SELECT id, title FROM courses WHERE is_active = 1 ORDER BY title").fetchall()
            if request.method == "POST":
                connection.execute(
                    """
                    UPDATE class_batches
                    SET course_id = ?, batch_code = ?, venue = ?, start_date = ?, end_date = ?,
                        seat_limit = ?, status = ?
                    WHERE id = ?
                    """,
                    (
                        int(request.form["course_id"]),
                        request.form["batch_code"].strip().upper(),
                        request.form["venue"].strip(),
                        request.form["start_date"],
                        request.form["end_date"],
                        int(request.form["seat_limit"]),
                        request.form["status"],
                        batch_id,
                    ),
                )
                resync_batch_attendance(connection, batch_id)
                connection.commit()
                flash("Class batch updated.", "success")
                return redirect(url_for("manage_batches"))
        return render_template("batch_edit.html", batch=batch, courses=courses)

    @app.route("/staff/batches/<int:batch_id>/delete", methods=["POST"])
    @login_required("staff", "admin")
    def delete_batch(batch_id: int):
        with get_connection() as connection:
            connection.execute("DELETE FROM class_batches WHERE id = ?", (batch_id,))
            connection.commit()
        flash("Class batch deleted.", "success")
        return redirect(url_for("manage_batches"))

    @app.route("/staff/instructors", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def manage_instructors():
        with get_connection() as connection:
            if request.method == "POST":
                connection.execute(
                    """
                    INSERT INTO instructors (full_name, email, qualifications)
                    VALUES (?, ?, ?)
                    """,
                    (
                        request.form["full_name"].strip(),
                        request.form["email"].strip().lower(),
                        request.form["qualifications"].strip(),
                    ),
                )
                connection.commit()
                flash("Instructor added.", "success")
                return redirect(url_for("manage_instructors"))
            instructors = connection.execute("SELECT * FROM instructors ORDER BY full_name").fetchall()
            batches = connection.execute(
                """
                SELECT cb.id, cb.batch_code, c.title AS course_title
                FROM class_batches cb
                JOIN courses c ON c.id = cb.course_id
                ORDER BY cb.start_date DESC
                """
            ).fetchall()
            assignments = connection.execute(
                """
                SELECT a.id, i.full_name, cb.batch_code, c.title AS course_title, a.role
                FROM assignments a
                JOIN instructors i ON i.id = a.instructor_id
                JOIN class_batches cb ON cb.id = a.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                ORDER BY cb.start_date DESC, i.full_name
                """
            ).fetchall()
        return render_template("staff_instructors.html", instructors=instructors, batches=batches, assignments=assignments)

    @app.route("/staff/instructors/<int:instructor_id>/edit", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def edit_instructor(instructor_id: int):
        with get_connection() as connection:
            instructor = connection.execute("SELECT * FROM instructors WHERE id = ?", (instructor_id,)).fetchone()
            if not instructor:
                abort(404)
            if request.method == "POST":
                connection.execute(
                    """
                    UPDATE instructors
                    SET full_name = ?, email = ?, qualifications = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (
                        request.form["full_name"].strip(),
                        request.form["email"].strip().lower(),
                        request.form["qualifications"].strip(),
                        1 if request.form.get("is_active") == "1" else 0,
                        instructor_id,
                    ),
                )
                connection.commit()
                flash("Instructor updated.", "success")
                return redirect(url_for("manage_instructors"))
        return render_template("instructor_edit.html", instructor=instructor)

    @app.route("/staff/instructors/<int:instructor_id>/delete", methods=["POST"])
    @login_required("staff", "admin")
    def delete_instructor(instructor_id: int):
        with get_connection() as connection:
            connection.execute("DELETE FROM instructors WHERE id = ?", (instructor_id,))
            connection.commit()
        flash("Instructor deleted.", "success")
        return redirect(url_for("manage_instructors"))

    @app.route("/staff/assignments", methods=["POST"])
    @login_required("staff", "admin")
    def create_assignment():
        with get_connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO assignments (class_batch_id, instructor_id, role)
                VALUES (?, ?, ?)
                """,
                (
                    int(request.form["class_batch_id"]),
                    int(request.form["instructor_id"]),
                    request.form["role"],
                ),
            )
            connection.commit()
        flash("Instructor assigned to class batch.", "success")
        return redirect(url_for("manage_instructors"))

    @app.route("/staff/assignments/<int:assignment_id>/edit", methods=["GET", "POST"])
    @login_required("staff", "admin")
    def edit_assignment(assignment_id: int):
        with get_connection() as connection:
            assignment = connection.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if not assignment:
                abort(404)
            instructors = connection.execute("SELECT * FROM instructors WHERE is_active = 1 ORDER BY full_name").fetchall()
            batches = connection.execute(
                """
                SELECT cb.id, cb.batch_code, c.title AS course_title
                FROM class_batches cb
                JOIN courses c ON c.id = cb.course_id
                ORDER BY cb.start_date DESC
                """
            ).fetchall()
            if request.method == "POST":
                connection.execute(
                    """
                    UPDATE assignments
                    SET class_batch_id = ?, instructor_id = ?, role = ?
                    WHERE id = ?
                    """,
                    (
                        int(request.form["class_batch_id"]),
                        int(request.form["instructor_id"]),
                        request.form["role"],
                        assignment_id,
                    ),
                )
                connection.commit()
                flash("Instructor assignment updated.", "success")
                return redirect(url_for("manage_instructors"))
            assignment_view = connection.execute(
                """
                SELECT a.*, i.full_name, cb.batch_code, c.title AS course_title
                FROM assignments a
                JOIN instructors i ON i.id = a.instructor_id
                JOIN class_batches cb ON cb.id = a.class_batch_id
                JOIN courses c ON c.id = cb.course_id
                WHERE a.id = ?
                """,
                (assignment_id,),
            ).fetchone()
        return render_template("assignment_edit.html", assignment=assignment_view, instructors=instructors, batches=batches)

    @app.route("/staff/assignments/<int:assignment_id>/delete", methods=["POST"])
    @login_required("staff", "admin")
    def delete_assignment(assignment_id: int):
        with get_connection() as connection:
            connection.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
            connection.commit()
        flash("Instructor assignment deleted.", "success")
        return redirect(url_for("manage_instructors"))

    @app.route("/admin/users", methods=["GET", "POST"])
    @login_required("admin")
    def manage_users():
        with get_connection() as connection:
            if request.method == "POST":
                role = request.form["role"]
                if role not in {"staff", "cashier"}:
                    flash("Admin can only create staff or cashier accounts from this form.", "error")
                    return redirect(url_for("manage_users"))
                connection.execute(
                    """
                    INSERT INTO users (full_name, email, password_hash, role, phone)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request.form["full_name"].strip(),
                        request.form["email"].strip().lower(),
                        hash_password(request.form["password"]),
                        role,
                        request.form.get("phone", "").strip(),
                    ),
                )
                connection.commit()
                flash("Employee account created.", "success")
                return redirect(url_for("manage_users"))
            users = connection.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return render_template("admin_users.html", users=users)

    @app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
    @login_required("admin")
    def edit_user(user_id: int):
        with get_connection() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                abort(404)
            if request.method == "POST":
                password_hash = user["password_hash"]
                if request.form.get("password", "").strip():
                    password_hash = hash_password(request.form["password"])
                connection.execute(
                    """
                    UPDATE users
                    SET full_name = ?, email = ?, phone = ?, role = ?, active = ?, password_hash = ?
                    WHERE id = ?
                    """,
                    (
                        request.form["full_name"].strip(),
                        request.form["email"].strip().lower(),
                        request.form.get("phone", "").strip(),
                        request.form["role"],
                        1 if request.form.get("active") == "1" else 0,
                        password_hash,
                        user_id,
                    ),
                )
                connection.commit()
                flash("User updated.", "success")
                return redirect(url_for("manage_users"))
        return render_template("user_edit.html", user=user)

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @login_required("admin")
    def delete_user(user_id: int):
        if user_id == g.user["id"]:
            flash("You cannot delete your own active admin account.", "error")
            return redirect(url_for("manage_users"))
        with get_connection() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            connection.commit()
        flash("User deleted.", "success")
        return redirect(url_for("manage_users"))

    @app.route("/reports")
    @login_required("staff", "admin")
    def reports():
        with get_connection() as connection:
            report_rows = connection.execute(
                """
                SELECT c.title AS course_title,
                       COUNT(DISTINCT cb.id) AS batch_count,
                       COUNT(e.id) AS total_enrollments,
                       SUM(CASE WHEN e.payment_status = 'paid' THEN 1 ELSE 0 END) AS paid_enrollments,
                       SUM(CASE WHEN cert.id IS NOT NULL THEN 1 ELSE 0 END) AS certificates_issued
                FROM courses c
                LEFT JOIN class_batches cb ON cb.course_id = c.id
                LEFT JOIN enrollments e ON e.class_batch_id = cb.id
                LEFT JOIN certificates cert ON cert.enrollment_id = e.id
                GROUP BY c.id
                ORDER BY c.title
                """
            ).fetchall()
        return render_template("reports.html", report_rows=report_rows)

    @app.route("/manifest.webmanifest")
    def manifest():
        return app.send_static_file("manifest.webmanifest")

    @app.route("/sw.js")
    def sw():
        return app.send_static_file("sw.js")

    return app


def login_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                flash("Please sign in to continue.", "error")
                return redirect(url_for("login"))
            if roles and g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def target_dashboard(role: str) -> str:
    if role == "client":
        return url_for("client_dashboard")
    if role == "cashier":
        return url_for("cashier_dashboard")
    return url_for("staff_dashboard")


def seed_data() -> None:
    with get_connection() as connection:
        for user in [
            ("Port Admin", "admin@maritimeenroll.test", hash_password("admin123"), "admin", "555-1000"),
            ("Training Staff", "staff@maritimeenroll.test", hash_password("staff123"), "staff", "555-1001"),
            ("Front Cashier", "cashier@maritimeenroll.test", hash_password("cashier123"), "cashier", "555-1003"),
            ("Maria Santos", "client@maritimeenroll.test", hash_password("client123"), "client", "555-1002"),
        ]:
            connection.execute(
                """
                INSERT OR IGNORE INTO users (full_name, email, password_hash, role, phone)
                VALUES (?, ?, ?, ?, ?)
                """,
                user,
            )
        existing_courses = connection.execute("SELECT COUNT(*) AS count FROM courses").fetchone()
        if existing_courses["count"] > 0:
            connection.commit()
            return
        connection.executemany(
            """
            INSERT INTO instructors (full_name, email, qualifications)
            VALUES (?, ?, ?)
            """,
            [
                ("Capt. Daniel Reyes", "dreyes@example.com", "Basic Safety, Crowd Management"),
                ("Engr. Liza Fernandez", "lfernandez@example.com", "EFA, Engine Room Safety"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO courses (code, title, description, duration_days, price_cents, prerequisites, certificate_prefix)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("BT", "Basic Training", "Core STCW safety training for all seafarers.", 5, 180000, "Government ID, medical certificate", "BT"),
                ("EFA", "Elementary First Aid", "Emergency medical response and first aid at sea.", 2, 95000, "Valid seafarer record book", "EFA"),
                ("PPFF", "Proficiency in Personal Safety and Fire Fighting", "Hands-on shipboard fire prevention and response.", 3, 120000, "None", "PPFF"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO class_batches (course_id, batch_code, venue, start_date, end_date, seat_limit, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "BT-2026-APR-A", "Manila Training Center", "2026-04-20", "2026-04-24", 24, "open"),
                (2, "EFA-2026-MAY-A", "Cebu Simulation Room", "2026-05-05", "2026-05-06", 18, "open"),
                (3, "PPFF-2026-MAY-B", "Subic Fire Grounds", "2026-05-14", "2026-05-16", 20, "open"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO assignments (class_batch_id, instructor_id, role)
            VALUES (?, ?, ?)
            """,
            [(1, 1, "lead"), (2, 2, "lead"), (3, 1, "support")],
        )
        connection.commit()


def get_batch_with_course(connection, batch_id: int):
    return connection.execute(
        """
        SELECT cb.*, c.title AS course_title, c.description, c.price_cents, c.prerequisites,
               (
                   SELECT COUNT(*)
                   FROM enrollments e
                   WHERE e.class_batch_id = cb.id
                     AND e.status IN ('pending_payment', 'confirmed', 'completed')
               ) AS seats_taken
        FROM class_batches cb
        JOIN courses c ON c.id = cb.course_id
        WHERE cb.id = ?
        """,
        (batch_id,),
    ).fetchone()


def get_client_enrollment_detail(connection, enrollment_id: int, user_id: int):
    return connection.execute(
        """
        SELECT e.*, c.title AS course_title, cb.batch_code, cb.start_date, cb.end_date, cb.status AS batch_status,
               p.amount_cents, p.provider_reference, p.status AS payment_state, p.payment_method
        FROM enrollments e
        JOIN class_batches cb ON cb.id = e.class_batch_id
        JOIN courses c ON c.id = cb.course_id
        LEFT JOIN payments p ON p.enrollment_id = e.id
        WHERE e.id = ? AND e.user_id = ?
        """,
        (enrollment_id, user_id),
    ).fetchone()


def get_staff_enrollment_detail(connection, enrollment_id: int):
    return connection.execute(
        """
        SELECT e.*, u.full_name, u.email, c.title AS course_title, c.certificate_prefix, c.duration_days,
               cb.batch_code, cb.start_date, cb.end_date, cb.status AS batch_status,
               p.provider_reference, p.status AS payment_state, cert.certificate_number
        FROM enrollments e
        JOIN users u ON u.id = e.user_id
        JOIN class_batches cb ON cb.id = e.class_batch_id
        JOIN courses c ON c.id = cb.course_id
        LEFT JOIN payments p ON p.enrollment_id = e.id
        LEFT JOIN certificates cert ON cert.enrollment_id = e.id
        WHERE e.id = ?
        """,
        (enrollment_id,),
    ).fetchone()


def get_enrollment(connection, enrollment_id: int, user_id: int):
    return connection.execute(
        """
        SELECT e.*, c.title AS course_title, cb.batch_code, cb.start_date, cb.end_date,
               p.amount_cents, p.provider_reference, p.status AS payment_state, p.payment_method
        FROM enrollments e
        JOIN class_batches cb ON cb.id = e.class_batch_id
        JOIN courses c ON c.id = cb.course_id
        JOIN payments p ON p.enrollment_id = e.id
        WHERE e.id = ? AND e.user_id = ?
        """,
        (enrollment_id, user_id),
    ).fetchone()


def create_payment_session(connection, enrollment_id: int, amount_cents: int) -> None:
    provider_reference = f"cs_test_{secrets.token_hex(8)}"
    connection.execute(
        """
        INSERT INTO payments (enrollment_id, provider, payment_method, provider_reference, amount_cents, status)
        VALUES (?, 'gcash', 'gcash', ?, ?, 'pending')
        """,
        (enrollment_id, provider_reference, amount_cents),
    )


def confirm_payment(connection, enrollment_id: int, payment_method: str, approved_by_user_id: int | None = None) -> None:
    connection.execute(
        """
        UPDATE payments
        SET provider = ?,
            payment_method = ?,
            status = 'succeeded',
            receipt_url = CASE
                WHEN ? = 'gcash' THEN 'https://payments.example.test/receipt/' || provider_reference
                ELSE 'OTC-APPROVED'
            END,
            approved_by_user_id = ?,
            approved_at = CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE approved_at END
        WHERE enrollment_id = ?
        """,
        (payment_method, payment_method, payment_method, approved_by_user_id, approved_by_user_id, enrollment_id),
    )
    connection.execute(
        """
        UPDATE enrollments
        SET payment_status = 'paid',
            approval_status = 'approved',
            status = 'confirmed'
        WHERE id = ?
        """,
        (enrollment_id,),
    )
    enrollment = connection.execute(
        """
        SELECT e.id, cb.start_date, cb.end_date
        FROM enrollments e
        JOIN class_batches cb ON cb.id = e.class_batch_id
        WHERE e.id = ?
        """,
        (enrollment_id,),
    ).fetchone()
    sync_attendance_records(connection, enrollment)
    close_batch_if_full(connection, enrollment_id)
    connection.commit()


def fail_payment(connection, enrollment_id: int) -> None:
    connection.execute("UPDATE payments SET status = 'failed' WHERE enrollment_id = ?", (enrollment_id,))
    connection.execute("UPDATE enrollments SET payment_status = 'failed', approval_status = 'pending' WHERE id = ?", (enrollment_id,))
    connection.commit()


def submit_otc_payment(connection, enrollment_id: int) -> None:
    provider_reference = f"otc_{secrets.token_hex(6)}"
    connection.execute(
        """
        UPDATE payments
        SET provider = 'otc',
            payment_method = 'otc',
            provider_reference = ?,
            status = 'pending',
            receipt_url = NULL,
            approved_by_user_id = NULL,
            approved_at = NULL
        WHERE enrollment_id = ?
        """,
        (provider_reference, enrollment_id),
    )
    connection.execute(
        """
        UPDATE enrollments
        SET payment_status = 'pending',
            approval_status = 'pending',
            status = 'pending_payment'
        WHERE id = ?
        """,
        (enrollment_id,),
    )
    connection.commit()


def close_batch_if_full(connection, enrollment_id: int) -> None:
    enrollment = connection.execute("SELECT class_batch_id FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    seats_taken = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM enrollments
        WHERE class_batch_id = ?
          AND status IN ('pending_payment', 'confirmed', 'completed')
        """,
        (enrollment["class_batch_id"],),
    ).fetchone()["total"]
    seat_limit = connection.execute("SELECT seat_limit FROM class_batches WHERE id = ?", (enrollment["class_batch_id"],)).fetchone()["seat_limit"]
    if seats_taken >= seat_limit:
        connection.execute("UPDATE class_batches SET status = 'closed' WHERE id = ?", (enrollment["class_batch_id"],))


def generate_session_dates(start_date: str, end_date: str) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def sync_attendance_records(connection, enrollment) -> None:
    if not enrollment:
        return
    dates = generate_session_dates(enrollment["start_date"], enrollment["end_date"])
    existing = connection.execute(
        "SELECT id, session_date, status, marked_by_client FROM attendance_records WHERE enrollment_id = ?",
        (enrollment["id"],),
    ).fetchall()
    existing_dates = {row["session_date"]: row for row in existing}
    allowed = set(dates)
    for record in existing:
        if record["session_date"] not in allowed:
            connection.execute("DELETE FROM attendance_records WHERE id = ?", (record["id"],))
    for session_date in dates:
        if session_date not in existing_dates:
            connection.execute(
                """
                INSERT OR IGNORE INTO attendance_records (enrollment_id, session_date, status)
                VALUES (?, ?, 'pending')
                """,
                (enrollment["id"], session_date),
            )
    connection.commit()


def resync_batch_attendance(connection, batch_id: int) -> None:
    enrollments = connection.execute(
        """
        SELECT e.id, cb.start_date, cb.end_date
        FROM enrollments e
        JOIN class_batches cb ON cb.id = e.class_batch_id
        WHERE e.class_batch_id = ?
        """,
        (batch_id,),
    ).fetchall()
    for enrollment in enrollments:
        sync_attendance_records(connection, enrollment)


def get_attendance_summary(connection, enrollment_id: int) -> dict[str, int | bool]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total_sessions,
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) AS present_sessions,
               SUM(CASE WHEN marked_by_client = 1 THEN 1 ELSE 0 END) AS marked_sessions
        FROM attendance_records
        WHERE enrollment_id = ?
        """,
        (enrollment_id,),
    ).fetchone()
    total = row["total_sessions"] or 0
    present = row["present_sessions"] or 0
    marked = row["marked_sessions"] or 0
    return {
        "total_sessions": total,
        "present_sessions": present,
        "marked_sessions": marked,
        "perfect_attendance": total > 0 and present == total and marked == total,
    }


def is_batch_finished(batch_like) -> bool:
    today = date.today().isoformat()
    return batch_like["batch_status"] == "completed" or batch_like["end_date"] <= today


def can_issue_certificate(enrollment, summary: dict[str, int | bool]) -> bool:
    return (
        enrollment["payment_status"] == "paid"
        and is_batch_finished(enrollment)
        and bool(summary["perfect_attendance"])
    )


def issue_certificate(connection, app: Flask, enrollment) -> None:
    existing = connection.execute("SELECT id FROM certificates WHERE enrollment_id = ?", (enrollment["id"],)).fetchone()
    if existing:
        return
    cert_number = next_certificate_number(connection, enrollment["certificate_prefix"])
    issue_date = datetime.utcnow().strftime("%Y-%m-%d")
    file_path = app.config["CERTIFICATE_DIR"] / f"{cert_number}.pdf"
    build_certificate_pdf(
        file_path,
        cert_number,
        enrollment["full_name"],
        enrollment["course_title"],
        issue_date,
        enrollment["batch_code"],
    )
    connection.execute(
        """
        INSERT INTO certificates (enrollment_id, certificate_number, file_path)
        VALUES (?, ?, ?)
        """,
        (enrollment["id"], cert_number, str(file_path)),
    )


def next_certificate_number(connection, prefix: str) -> str:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM certificates WHERE certificate_number LIKE ?",
        (f"{prefix}-%",),
    ).fetchone()
    serial = row["total"] + 1
    year = datetime.utcnow().year
    return f"{prefix}-{year}-{serial:04d}"


def attendance_summary_query() -> str:
    return """
        SELECT e.id AS enrollment_id,
               COUNT(ar.id) AS total_sessions,
               SUM(CASE WHEN ar.status = 'present' THEN 1 ELSE 0 END) AS present_sessions
        FROM enrollments e
        LEFT JOIN attendance_records ar ON ar.enrollment_id = e.id
        WHERE e.user_id = ?
        GROUP BY e.id
    """


def staff_attendance_summary_query() -> str:
    return """
        SELECT e.id AS enrollment_id,
               COUNT(ar.id) AS total_sessions,
               SUM(CASE WHEN ar.status = 'present' THEN 1 ELSE 0 END) AS present_sessions
        FROM enrollments e
        LEFT JOIN attendance_records ar ON ar.enrollment_id = e.id
        GROUP BY e.id
    """
