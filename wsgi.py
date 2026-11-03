import os
import shutil
from pathlib import Path
from app import app, init_db

# Bazani serverda avtomatik tayyorlash
base_dir = Path(__file__).resolve().parent
db_path = getattr(app, 'DB_PATH', None) or os.environ.get('DATABASE_PATH') or str(base_dir / 'tortkuldon.db')

if not os.path.isfile(db_path):
    clean_template = base_dir / 'data' / 'tortkuldon_clean.sqlite'
    if clean_template.is_file():
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        shutil.copy2(clean_template, db_path)
    init_db()

application = app

if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '5051'))
    app.run(host=host, port=port)
