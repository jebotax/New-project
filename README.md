# Maritime Enrollment App

Responsive enrollment platform for maritime training providers. This MVP includes:

- Client registration and login
- Course catalog and scheduled class batches
- Enrollment flow with Stripe-style checkout simulation
- Auto-confirmation for paid enrollments
- Staff and admin dashboards
- Instructor management and batch assignment
- Certificate issuance with generated PDF downloads
- PWA manifest and service worker

## Run

```bash
python3 app.py
```

Open `http://127.0.0.1:5000`.

## Demo Accounts

- `admin@maritimeenroll.test` / `admin123`
- `staff@maritimeenroll.test` / `staff123`
- `client@maritimeenroll.test` / `client123`

## Notes

- Payments are implemented as a Stripe-compatible demo flow in this MVP so the project runs without external secrets.
- Data is stored in SQLite at `instance/enrollment_app.db`.
