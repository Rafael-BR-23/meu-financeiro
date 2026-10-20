import errno
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
import deploy


class SFTPDisco:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, remote):
        return self.root / remote.lstrip('/')

    def stat(self, remote):
        return self.path(remote).stat()

    def mkdir(self, remote):
        self.path(remote).mkdir()

    def put(self, local, remote):
        self.path(remote).write_bytes(Path(local).read_bytes())

    def rename(self, source, target):
        if self.path(target).exists():
            raise FileExistsError(target)
        self.path(source).rename(self.path(target))

    def remove(self, remote):
        self.path(remote).unlink()


class DeployTest(unittest.TestCase):
    def test_envio_recursivo_e_preservacao_anexos(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'local'
            (source / 'css' / 'mobile').mkdir(parents=True)
            (source / 'css' / 'mobile' / 'app.css').write_text('novo')
            (source / 'sw.js').write_text('worker')
            sftp = SFTPDisco(root / 'remoto')
            sftp.root.mkdir()
            deploy.enviar_pasta(sftp, source, '/app/static')
            self.assertEqual(sftp.path('/app/static/css/mobile/app.css').read_text(), 'novo')
            sftp.path('/app/static/sw.js').write_text('preservado')
            deploy.enviar_pasta(sftp, source, '/app/static', somente_novos=True)
            self.assertEqual(sftp.path('/app/static/sw.js').read_text(), 'preservado')

    def test_snapshot_inclui_wal_e_recusa_substituir_banco(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'local.db'
            sftp = SFTPDisco(root / 'remoto')
            sftp.root.mkdir()
            with closing(sqlite3.connect(source)) as connection:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE exemplo (valor INTEGER)')
                connection.execute('INSERT INTO exemplo VALUES (42)')
                connection.commit()
                deploy.enviar_banco_novo(sftp, source, '/app/instance/local.db', root)
            with closing(sqlite3.connect(sftp.path('/app/instance/local.db'))) as snapshot:
                self.assertEqual(snapshot.execute('SELECT valor FROM exemplo').fetchone()[0], 42)
            original = sftp.path('/app/instance/local.db').read_bytes()
            with self.assertRaises(RuntimeError):
                deploy.enviar_banco_novo(sftp, source, '/app/instance/local.db', root)
            self.assertEqual(sftp.path('/app/instance/local.db').read_bytes(), original)

    def test_migracao_recusa_contas_existentes_e_erros_de_permissao(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'instance').mkdir()
            (root / 'instance' / 'usuarios.db').write_bytes(b'local')
            sftp = SFTPDisco(root / 'remoto')
            sftp.root.mkdir()
            deploy.criar_pasta_remota(sftp, '/app/instance')
            sftp.path('/app/instance/usuarios.db').write_bytes(b'preservar')
            with patch.object(deploy, 'LOCAL_DIR', root), self.assertRaises(RuntimeError):
                deploy.migrar_contas(sftp, '/app')
            self.assertEqual(sftp.path('/app/instance/usuarios.db').read_bytes(), b'preservar')
            with patch.object(sftp, 'stat', side_effect=PermissionError(errno.EACCES, 'Negado')):
                with self.assertRaises(PermissionError):
                    deploy.existe_remoto(sftp, '/app/instance')

    def test_migracao_leva_cadastro_bancos_e_comprovantes_sem_trocar_historico(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'instance' / 'contas').mkdir(parents=True)
            (root / 'uploads' / 'contas' / 'usuario').mkdir(parents=True)
            for relative in ('instance/usuarios.db', 'instance/financeiro.db', 'instance/contas/usuario.db'):
                with closing(sqlite3.connect(root / relative)) as connection:
                    connection.execute('CREATE TABLE exemplo (valor INTEGER)')
                    connection.execute('INSERT INTO exemplo VALUES (42)')
                    connection.commit()
            (root / 'uploads' / 'contas' / 'usuario' / 'recibo.png').write_bytes(b'comprovante')
            (root / 'uploads' / 'antigo.png').write_bytes(b'local')
            sftp = SFTPDisco(root / 'remoto')
            sftp.root.mkdir()
            deploy.criar_pasta_remota(sftp, '/app/instance')
            deploy.criar_pasta_remota(sftp, '/app/uploads')
            sftp.path('/app/instance/financeiro.db').write_bytes(b'historico-servidor')
            sftp.path('/app/uploads/antigo.png').write_bytes(b'anexo-servidor')
            with patch.object(deploy, 'LOCAL_DIR', root):
                deploy.migrar_contas(sftp, '/app')
            self.assertEqual(sftp.path('/app/instance/financeiro.db').read_bytes(), b'historico-servidor')
            self.assertEqual(sftp.path('/app/uploads/antigo.png').read_bytes(), b'anexo-servidor')
            self.assertEqual(sftp.path('/app/uploads/contas/usuario/recibo.png').read_bytes(), b'comprovante')
            for relative in ('usuarios.db', 'contas/usuario.db'):
                with closing(sqlite3.connect(sftp.path('/app/instance/' + relative))) as connection:
                    self.assertEqual(connection.execute('SELECT valor FROM exemplo').fetchone()[0], 42)

    def test_docker_context_is_explicit_and_runtime_is_non_root(self):
        root = Path(__file__).resolve().parent.parent
        ignore = (root / '.dockerignore').read_text(encoding='utf-8')
        dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
        for rule in ('.env', '*.db', 'instance/', 'uploads/', '*.csv', '*.pdf', 'backups/'):
            self.assertIn(rule, ignore)
        self.assertNotIn('COPY . .', dockerfile)
        for source in ('app.py', 'templates/', 'static/'):
            self.assertIn(source, dockerfile)
        self.assertIn('USER app', dockerfile)
        self.assertIn('/app/instance', dockerfile)
        self.assertIn('/app/uploads', dockerfile)

    def test_ssh_requires_operator_known_hosts_and_rejects_unknown_host(self):
        fake = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict('sys.modules', {'paramiko': fake}):
            known_hosts = Path(folder) / 'known_hosts'
            known_hosts.write_text('host ssh-ed25519 AAAATEST\n', encoding='ascii')
            client = deploy.criar_cliente_ssh(known_hosts)
            client.load_host_keys.assert_called_once_with(str(known_hosts))
            client.set_missing_host_key_policy.assert_called_once_with(fake.RejectPolicy.return_value)
        with self.assertRaises(RuntimeError):
            deploy.criar_cliente_ssh(Path('arquivo-inexistente-known-hosts'))

    def test_direct_dependencies_are_exactly_pinned(self):
        root = Path(__file__).resolve().parent.parent
        reqs = (root / 'requirements.txt').read_text(encoding='utf-8').splitlines()
        deploy_reqs = (root / 'requirements-deploy.txt').read_text(encoding='utf-8').splitlines()
        for line in reqs:
            line = line.strip()
            if line and not line.startswith('#'):
                self.assertRegex(line, r'^[A-Za-z0-9_.\-]+==[0-9A-Za-z_.\-]+$')
        for line in deploy_reqs:
            line = line.strip()
            if line and not line.startswith('#'):
                self.assertRegex(line, r'^[A-Za-z0-9_.\-]+==[0-9A-Za-z_.\-]+$')

    def test_complete_dependency_lock_is_constrained(self):
        root = Path(__file__).resolve().parent.parent
        lock_path = root / 'requirements-lock.txt'
        self.assertTrue(lock_path.is_file())
        self.assertIn('requirements-lock.txt', deploy.ROOT_FILES)
        compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('SUPABASE_TOKEN_ENCRYPTION_KEY=${SUPABASE_TOKEN_ENCRYPTION_KEY:-}', compose)
        self.assertIn('RATELIMIT_STORAGE_URI=${RATELIMIT_STORAGE_URI:-redis://redis:6379/0}', compose)
        lock_lines = [l.strip() for l in lock_path.read_text(encoding='utf-8').splitlines() if l.strip() and not l.strip().startswith('#')]
        packages = {}
        for line in lock_lines:
            self.assertRegex(line, r'^[A-Za-z0-9_.\-]+==[0-9A-Za-z_.\-]+$')
            pkg, ver = line.split('==', 1)
            norm = pkg.lower().replace('_', '-')
            self.assertNotIn(norm, packages, f'Pacote duplicado no lock: {pkg}')
            packages[norm] = ver

        for manifest in ('requirements.txt', 'requirements-deploy.txt'):
            lines = (root / manifest).read_text(encoding='utf-8').splitlines()
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    pkg, ver = line.split('==', 1)
                    norm = pkg.lower().replace('_', '-')
                    self.assertIn(norm, packages, f'Dependencia direta ausente no lock: {pkg}')
                    self.assertEqual(packages[norm].lower(), ver.lower(), f'Versao divergente para {pkg}')

        dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('COPY requirements.txt requirements-lock.txt', dockerfile)
        self.assertIn('pip install --no-cache-dir -c requirements-lock.txt -r requirements.txt', dockerfile)


    def test_validation_guide_requires_integrity_and_vulnerability_audit(self):
        root = Path(__file__).resolve().parent.parent
        plan = (root / 'docs/validacao.md').read_text(encoding='utf-8')
        self.assertIn('.\\venv\\Scripts\\python.exe -m pip check', plan)
        self.assertIn('.\\venv\\Scripts\\python.exe -m pip_audit --no-deps --disable-pip -r requirements-lock.txt', plan)
        for field in ('Impacto:', 'Mitigação:', 'Decisão:'):
            self.assertIn(field, plan)


if __name__ == '__main__':
    unittest.main()
