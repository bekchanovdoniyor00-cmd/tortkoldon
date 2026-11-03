import json
import os
import secrets
import tempfile
import gc
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix='tortkuldon_waitress_5052_') as test_root:
        database_path = Path(test_root) / 'waitress_test.sqlite'
        os.environ['SECRET_KEY'] = secrets.token_hex(32)

        import app as erp

        erp.DB_PATH = str(database_path)
        erp.init_db()

        test_passwords = {
            'buxgalter': secrets.token_urlsafe(18),
            'rahbar': secrets.token_urlsafe(18),
            'umid': secrets.token_urlsafe(18),
            'maxmud': secrets.token_urlsafe(18),
        }
        with erp.db() as connection:
            for role, password in test_passwords.items():
                connection.execute(
                    'INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)',
                    (
                        f'test_{role}',
                        erp.generate_password_hash(password),
                        role,
                        '',
                    ),
                )

        credentials_path = Path(test_root) / 'test_credentials.json'
        credentials_path.write_text(
            json.dumps(
                {
                    f'test_{role}': {'role': role, 'password': password}
                    for role, password in test_passwords.items()
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        from waitress import serve

        print(f'TEMP_DATABASE={database_path}', flush=True)
        print(f'TEMP_CREDENTIALS={credentials_path}', flush=True)
        print(f'TEST_PROFILES={",".join(test_passwords)}', flush=True)
        print('WAITRESS_LISTENING=http://127.0.0.1:5052', flush=True)
        try:
            serve(erp.app, host='127.0.0.1', port=5052, threads=8)
        except KeyboardInterrupt:
            print('WAITRESS_STOPPED', flush=True)
        finally:
            gc.collect()


if __name__ == '__main__':
    main()
