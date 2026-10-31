"""Administrative commands: no public registration or default credentials."""
import argparse
import getpass
import os

from sqlalchemy import select

from .db import User, database
from .security import hasher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create-user', 'init-db'])
    parser.add_argument('--email')
    parser.add_argument('--role', choices=['uploader', 'reviewer', 'admin'], default='uploader')
    args = parser.parse_args()
    engine, sessions = database()
    if args.command == 'create-user':
        if not args.email or '@' not in args.email:
            parser.error('--email is required')
        password = os.getenv('BOOTSTRAP_PASSWORD') or getpass.getpass('Password (12+ characters): ')
        if len(password) < 12:
            parser.error('Password must contain at least 12 characters')
        with sessions() as s:
            if s.scalar(select(User).where(User.email == args.email.lower())):
                parser.error('User already exists; no password or role was changed')
            s.add(User(email=args.email.lower(), password_hash=hasher.hash(password), role=args.role))
            s.commit()
        print('User created')
    else:
        print('Database schema initialized')
    engine.dispose()


if __name__ == '__main__':
    main()
