"""Noodbeheer zonder webinterface.

python -m app.beheer_cli mfa-uit <gebruikersnaam>   # tweestapsverificatie resetten (telefoon kwijt)
"""

import argparse

from sqlalchemy import func, select

from .db import SessionLocal
from .main import migrate
from .mfa import reset_mfa
from .models import User


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.beheer_cli", description="Noodbeheer voor accounts")
    sub = parser.add_subparsers(dest="actie", required=True)
    sub.add_parser("mfa-uit", help="tweestapsverificatie voor een account uitzetten").add_argument("gebruikersnaam")
    args = parser.parse_args(argv)
    migrate()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == args.gebruikersnaam.strip().lower()))
        if user is None:
            print("Gebruiker niet gevonden.")
            return 1
        reset_mfa(db, user, None)
        db.commit()
        print(f"Tweestapsverificatie voor {user.username} is uitgezet. Laat de gebruiker deze opnieuw instellen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
