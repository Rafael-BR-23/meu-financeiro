"""Deploy para ZimaOS. Use --migrar-contas apenas na primeira transferência de contas locais."""
import argparse
import errno
import os
import posixpath
import shlex
import sqlite3
import sys
import tempfile
import uuid
import select
from contextlib import closing
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parent

# Configuração local; credenciais SSH nunca são enviadas como arquivo ao servidor.
env_file = LOCAL_DIR / '.env'
if env_file.exists():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

REMOTE_HOST = os.environ.get('ZIMAOS_HOST', '')
REMOTE_USER = os.environ.get('ZIMAOS_USER', '')
REMOTE_PASS = os.environ.get('ZIMAOS_PASS')
REMOTE_DIR = os.environ.get('ZIMAOS_DIR', '/DATA/AppData/Financeiro')
KNOWN_HOSTS = Path(os.environ.get('ZIMAOS_KNOWN_HOSTS', Path.home() / '.ssh' / 'known_hosts')).expanduser()


def criar_cliente_ssh(known_hosts):
    if not Path(known_hosts).is_file():
        raise RuntimeError(f'Arquivo known_hosts não encontrado: {known_hosts}')
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client
ROOT_FILES = (
    'app.py', 'autenticacao.py', 'autenticacao_supabase.py', 'models.py', 'importador.py', 'conciliador.py',
    'graficos.py', 'metas.py', 'leitor_comprovante.py', 'requirements.txt', 'requirements-lock.txt',
    'Dockerfile', '.dockerignore', 'docker-compose.yml', '.env.example',
)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def existe_remoto(sftp, path):
    try:
        sftp.stat(path)
        return True
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise


def criar_pasta_remota(sftp, path):
    if path in ('', '/') or existe_remoto(sftp, path):
        return
    criar_pasta_remota(sftp, posixpath.dirname(path))
    sftp.mkdir(path)


def enviar_pasta(sftp, local, remote, somente_novos=False):
    criar_pasta_remota(sftp, remote)
    for source in sorted(Path(local).rglob('*')):
        if source.is_file():
            relative = source.relative_to(local).as_posix()
            target = posixpath.join(remote, relative)
            criar_pasta_remota(sftp, posixpath.dirname(target))
            if somente_novos and existe_remoto(sftp, target):
                continue
            print(f'  -> {target}')
            sftp.put(str(source), target)


def enviar_banco_novo(sftp, local, remote, temp_dir):
    """Snapshot SQLite inclui WAL; envio exclusivo nunca substitui banco remoto."""
    if existe_remoto(sftp, remote):
        raise RuntimeError(f'O destino já existe; banco preservado: {remote}')
    snapshot = Path(temp_dir) / (Path(local).stem + '-snapshot.db')
    with closing(sqlite3.connect(Path(local).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
    criar_pasta_remota(sftp, posixpath.dirname(remote))
    # rename SFTP comum recusa destino existente; só publica após envio completo.
    temporary = remote + '.' + uuid.uuid4().hex + '.tmp'
    try:
        sftp.put(str(snapshot), temporary)
        sftp.rename(temporary, remote)
    finally:
        if existe_remoto(sftp, temporary):
            sftp.remove(temporary)
    print(f'  -> banco novo: {remote}')


def migrar_contas(sftp, remote_dir):
    usuarios = LOCAL_DIR / 'instance' / 'usuarios.db'
    remote_users = posixpath.join(remote_dir, 'instance', 'usuarios.db')
    if not usuarios.is_file():
        raise RuntimeError('Não existe cadastro local em instance/usuarios.db.')
    if existe_remoto(sftp, remote_users):
        raise RuntimeError('O servidor já possui usuarios.db. Migração recusada para preservar suas contas.')
    with tempfile.TemporaryDirectory() as temp_dir:
        financeiro = LOCAL_DIR / 'instance' / 'financeiro.db'
        remote_financeiro = posixpath.join(remote_dir, 'instance', 'financeiro.db')
        if financeiro.exists() and not existe_remoto(sftp, remote_financeiro):
            enviar_banco_novo(sftp, financeiro, remote_financeiro, temp_dir)
        for account in sorted((LOCAL_DIR / 'instance' / 'contas').glob('*.db')):
            enviar_banco_novo(sftp, account, posixpath.join(remote_dir, 'instance', 'contas', account.name), temp_dir)
        # Preserva anexos já existentes no servidor e envia os que ainda faltam.
        enviar_pasta(sftp, LOCAL_DIR / 'uploads', posixpath.join(remote_dir, 'uploads'), somente_novos=True)
        # Cadastro por último: só fica disponível após os bancos e anexos.
        enviar_banco_novo(sftp, usuarios, remote_users, temp_dir)
    print('[+] Contas locais transferidas. O banco financeiro existente do servidor foi preservado.')


def deploy(migrar=False):
    if not all((REMOTE_HOST, REMOTE_USER, REMOTE_PASS)):
        print('[!] Defina ZIMAOS_HOST, ZIMAOS_USER e ZIMAOS_PASS no .env ou no ambiente.')
        return 1
    if not posixpath.isabs(REMOTE_DIR) or REMOTE_DIR == '/':
        print('[!] ZIMAOS_DIR deve ser um diretório absoluto específico do servidor.')
        return 1
    missing = [name for name in ROOT_FILES if not (LOCAL_DIR / name).is_file()]
    missing += [name for name in ('templates', 'static') if not (LOCAL_DIR / name).is_dir()]
    if missing:
        print('[!] Arquivos/pastas locais ausentes: ' + ', '.join(missing))
        return 1
    try:
        import paramiko
    except ImportError:
        print('[!] Instale a dependência local: python -m pip install -r requirements-deploy.txt')
        return 1

    ssh = criar_cliente_ssh(KNOWN_HOSTS)
    try:
        print(f'[*] Conectando a {REMOTE_HOST}...')
        ssh.connect(REMOTE_HOST, username=REMOTE_USER, password=REMOTE_PASS, timeout=15)
        with ssh.open_sftp() as sftp:
            # Verificação antes de modificar qualquer arquivo do servidor.
            if migrar and existe_remoto(sftp, posixpath.join(REMOTE_DIR, 'instance', 'usuarios.db')):
                raise RuntimeError('O servidor já tem contas. Não use --migrar-contas nesse servidor.')
            criar_pasta_remota(sftp, REMOTE_DIR)
            for folder in ('instance', 'instance/contas', 'uploads', 'uploads/contas'):
                criar_pasta_remota(sftp, posixpath.join(REMOTE_DIR, folder))
            for name in ROOT_FILES:
                print(f'  -> {name}')
                sftp.put(str(LOCAL_DIR / name), posixpath.join(REMOTE_DIR, name))
            for folder in ('templates', 'static'):
                enviar_pasta(sftp, LOCAL_DIR / folder, posixpath.join(REMOTE_DIR, folder))
            if migrar:
                migrar_contas(sftp, REMOTE_DIR)
            else:
                print('[*] Contas, bancos e comprovantes remotos preservados; contas locais não são enviadas.')
            if not existe_remoto(sftp, posixpath.join(REMOTE_DIR, '.env')):
                print('[!] Sem .env remoto: OAuth Google não será habilitado. Configure-o usando .env.example.')

        print('[*] Reconstruindo e reiniciando o contêiner...')
        command = (f'cd {shlex.quote(REMOTE_DIR)} && '
                   "sudo -S -p '' env DOCKER_CONFIG=/DATA/.docker docker compose up -d --build")
        stdin, stdout, stderr = ssh.exec_command(command)
        stdin.write(REMOTE_PASS + '\n')
        stdin.flush()
        stdin.channel.shutdown_write()
        # Drena as duas saídas para não travar quando o build emitir muitos logs.
        channel = stdout.channel
        while True:
            if channel.recv_ready():
                print(channel.recv(65536).decode('utf-8', errors='replace'), end='', flush=True)
            if channel.recv_stderr_ready():
                print(channel.recv_stderr(65536).decode('utf-8', errors='replace'), end='', flush=True)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            select.select([channel], [], [], 0.2)
        if channel.recv_exit_status() != 0:
            raise RuntimeError('Docker Compose falhou. O deploy não foi concluído.')
        print(f'\n[+] Deploy concluído: http://{REMOTE_HOST}:5005')
        return 0
    except Exception as error:
        print(f'[!] Deploy interrompido: {error}')
        return 1
    finally:
        ssh.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--migrar-contas', action='store_true', help='Transfere contas locais somente se o servidor ainda não tiver usuarios.db.')
    sys.exit(deploy(migrar=parser.parse_args().migrar_contas))
