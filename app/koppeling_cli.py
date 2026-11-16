"""Koppelsleutels beheren zonder webinterface: python -m app.koppeling_cli aanmaken|lijst|intrekken."""
import argparse

from fastapi import HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .koppeling import Koppelsleutel, maak_sleutel, sleutel_dict, trek_in
from .main import migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.koppeling_cli', description='Koppelsleutels voor de Bedrijfskoppeling API beheren')
    sub = parser.add_subparsers(dest='actie', required=True)
    sub.add_parser('aanmaken', help='nieuwe sleutel maken (eenmalig getoond)').add_argument('naam')
    sub.add_parser('lijst', help='sleutels tonen zonder geheime waarde')
    sub.add_parser('intrekken', help='sleutel intrekken').add_argument('id', type=int)
    args = parser.parse_args(argv)
    migrate()
    with SessionLocal() as db:
        if args.actie == 'aanmaken':
            rij, sleutel = maak_sleutel(db, args.naam, None)
            print(f'Koppelsleutel #{rij.id} ({rij.naam}) aangemaakt. Bewaar deze waarde nu; zij wordt niet opnieuw getoond:')
            print(sleutel)
        elif args.actie == 'lijst':
            for r in db.scalars(select(Koppelsleutel).order_by(Koppelsleutel.id)):
                d = sleutel_dict(db, r)
                print(f"#{d['id']}\t{d['voorvoegsel']}…\t{d['naam']}\t{'actief' if d['actief'] else 'ingetrokken'}\tlaatst gebruikt: {d['laatst_gebruikt_op'] or '-'}")
        else:
            try:
                trek_in(db, args.id, None)
            except HTTPException as error:
                print(error.detail)
                return 1
            print(f'Koppelsleutel #{args.id} ingetrokken.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
