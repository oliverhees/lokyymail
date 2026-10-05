"""Kommandozeile: lokyymail <befehl>."""

from __future__ import annotations

import argparse
import getpass
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lokyymail", description="LokyyMail – Mail-Gateway mit menschlicher Freigabe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("generate-key", help="Neuen Hauptschlüssel (LOKYY_MASTER_KEY) erzeugen")
    sub.add_parser("init-db", help="Datenbank-Tabellen anlegen")

    p_admin = sub.add_parser("create-admin", help="Ersten Administrator anlegen")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--name", default="")
    p_admin.add_argument("--password-stdin", action="store_true", help="Passwort von stdin lesen")

    p_serve = sub.add_parser("serve", help="Webserver starten")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("worker", help="Hintergrund-Worker starten")

    args = parser.parse_args(argv)

    if args.cmd == "generate-key":
        from .security import generate_master_key

        print(generate_master_key())
        return 0

    if args.cmd == "init-db":
        from .db import create_all, init_engine

        init_engine()
        create_all()
        print("Datenbank bereit.")
        return 0

    if args.cmd == "create-admin":
        from sqlalchemy import select

        from . import audit
        from .db import create_all, init_engine, session_scope
        from .models import User
        from .security import hash_password, password_problems

        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Passwort: ")
        problems = password_problems(password)
        if problems:
            print("Passwort zu schwach: " + " ".join(problems), file=sys.stderr)
            return 2
        init_engine()
        create_all()
        with session_scope() as db:
            email = args.email.strip().lower()
            if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
                print("Diese E-Mail gibt es schon.", file=sys.stderr)
                return 2
            user = User(email=email, display_name=args.name, role="admin", password_hash=hash_password(password))
            db.add(user)
            db.flush()
            audit.log(db, "user.created", actor_type="system", role="admin")
        print(f"Administrator {email} angelegt. Beim ersten Login wird Zwei-Faktor eingerichtet.")
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*", server_header=False)
        return 0

    if args.cmd == "worker":
        from .worker import run_forever

        run_forever()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
