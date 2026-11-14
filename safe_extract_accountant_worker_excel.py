"""Offline, Stage14-pinned extraction. No ERP imports or database access."""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
CHECKPOINT = ROOT / 'checkpoints' / 'DonControl_Refactor_Checkpoint_stage14_20261106_095545'
APP_SHA256 = '7ee8ba5f6fe6a76405c7e750f4f5d2bbea10254048c6be2f166740768f643207'
NAME = 'accountant_worker_payments_excel'
MODULE = 'accountant_worker_excel_export.py'
EXPORT = 'export_accountant_worker_payments_excel'
DEPENDENCIES = ('PAYMENT_METHODS', 'abort', 'current_user', 'customer_display', 'customer_identity_context', 'db')
ANCHOR = 'from legacy_excel_export import export_legacy_excel_report\n'
IMPORT = 'from accountant_worker_excel_export import export_accountant_worker_payments_excel\n'


def plan(raw):
    if hashlib.sha256(raw).hexdigest() != APP_SHA256:
        raise RuntimeError('STOP: app.py differs from verified Stage14')
    if raw.count(b'\n') != raw.count(b'\r\n') or b'\r' in raw.replace(b'\r\n', b''):
        raise RuntimeError('STOP: expected uniform CRLF')
    source = raw.decode('utf-8').replace('\r\n', '\n')
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == NAME)
    assert node.end_lineno - node.lineno + 1 == 84
    assert source.count(ANCHOR) == 1
    lines = source.splitlines(keepends=True)
    body = ''.join(lines[node.lineno:node.end_lineno])
    module = ('"""Accountant worker XLSX export; original Stage14 body preserved."""\n\n\n'
              f'def {EXPORT}(_app_dependencies):\n' +
              ''.join(f'    {name} = _app_dependencies[{name!r}]\n' for name in DEPENDENCIES) + body)
    wrapper = (f'def {NAME}():\n    return {EXPORT}({{\n' +
               ''.join(f'        {name!r}: {name},\n' for name in DEPENDENCIES) + '    })\n')
    changed = (''.join(lines[:node.lineno-1]) + wrapper + ''.join(lines[node.end_lineno:])).replace(ANCHOR, ANCHOR + IMPORT, 1)
    emitted = next(n for n in ast.parse(module).body if isinstance(n, ast.FunctionDef))
    assert [ast.dump(n) for n in node.body] == [ast.dump(n) for n in emitted.body[len(DEPENDENCIES):]]
    # Remove precisely the new import and restore the old function AST: all
    # other functions, policy constants and decorators must remain identical.
    new_tree = ast.parse(changed)
    new_tree.body = [n for n in new_tree.body if not (isinstance(n, ast.ImportFrom) and n.module == MODULE[:-3])]
    for index, n in enumerate(new_tree.body):
        if isinstance(n, ast.FunctionDef) and n.name == NAME:
            assert [ast.dump(d) for d in n.decorator_list] == [ast.dump(d) for d in node.decorator_list]
            new_tree.body[index] = node
    assert ast.dump(new_tree) == ast.dump(tree)
    for filename, text in [('app.py', changed), (MODULE, module)]:
        compile(text, filename, 'exec')
    return changed.encode('utf-8').replace(b'\n', b'\r\n'), module.encode('utf-8').replace(b'\n', b'\r\n')


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def prepare():
    assert Path(git('rev-parse', '--show-toplevel')).resolve() == ROOT
    assert git('branch', '--show-current') == 'refactor/app-modules-20261105'
    assert git('rev-parse', 'HEAD') == '77e4ed108987c31e179c84bb24e0949eb20caa65'
    assert not git('diff', '--cached', '--name-only')
    assert git('diff', '--name-only') == 'app.py'
    assert not (ROOT / MODULE).exists(), 'STOP: target already exists; never reapply'
    for line in (CHECKPOINT / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        expected, name = line.split('  ', 1)
        assert hashlib.sha256((CHECKPOINT / name).read_bytes()).hexdigest() == expected, name
    for path in (CHECKPOINT / 'project').rglob('*'):
        if path.is_file():
            assert path.read_bytes() == (ROOT / path.relative_to(CHECKPOINT / 'project')).read_bytes(), str(path)
    raw = (ROOT / 'app.py').read_bytes()
    return raw, plan(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    original, (app, module) = prepare()
    print('CHECK: OK; Stage14 hashes, Git, unchanged AST, original export body and syntax verified.')
    if args.apply:
        temporary = None
        target = ROOT / MODULE
        created = False
        try:
            assert (ROOT / 'app.py').read_bytes() == original
            with target.open('xb') as handle:
                created = True
                handle.write(module)
            with tempfile.NamedTemporaryFile(dir=ROOT, prefix='.dc15_', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(app)
            assert (ROOT / 'app.py').read_bytes() == original
            os.replace(temporary, ROOT / 'app.py')
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if created and (ROOT / 'app.py').read_bytes() == original:
                target.unlink(missing_ok=True)
        assert (ROOT / 'app.py').read_bytes() == app
        assert target.read_bytes() == module
        print('APPLY: OK; only app.py and accountant_worker_excel_export.py written.')


if __name__ == '__main__':
    main()
