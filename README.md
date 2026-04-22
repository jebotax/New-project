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

## Local Run

```bash
python3 app.py
```

Open `http://127.0.0.1:5000`.

## Local Demo Accounts

- `admin@maritimeenroll.test` / `admin123`
- `staff@maritimeenroll.test` / `staff123`
- `client@maritimeenroll.test` / `client123`
- `cashier@maritimeenroll.test` / `cashier123`

## GitHub

Create a new GitHub repo, then run:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

If this repo is already initialized locally, skip `git init` and just add the remote/push.

## Live Deploy

This app is ready to run with `gunicorn`:

```bash
gunicorn app:app
```

Set these environment variables in your host:

- `APP_ENV=production`
- `APP_SECRET_KEY=<long-random-secret>`
- `APP_DATABASE_PATH=/absolute/path/to/enrollment_app.db`
- `APP_CERTIFICATE_DIR=/absolute/path/to/certificates`
- `APP_BOOTSTRAP_ADMIN_EMAIL=<your-admin-email>`
- `APP_BOOTSTRAP_ADMIN_PASSWORD=<your-admin-password>`
- `APP_BOOTSTRAP_ADMIN_NAME=<your-admin-name>`
- `APP_SEED_DEMO_DATA=0`

Use a persistent disk/path for `APP_DATABASE_PATH` and `APP_CERTIFICATE_DIR`.

## Notes

- Payments are still a demo flow unless you connect a real payment provider.
- In development, the app uses local demo data.
- In production, the app can start with only your bootstrap admin account and an empty training catalog.
