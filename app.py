import sys
import ipaddress
import time as _time
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import re
import uuid
import calendar
import secrets
import hashlib
from collections.abc import Mapping
from datetime import datetime, date, timedelta
from flask import Flask, g, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, session
from flask_wtf import CSRFProtect
from werkzeug.utils import secure_filename
from sqlalchemy import event
from models import db, Pendencia, ExtratoNubank, Conciliacao, Meta, AporteMeta, FechamentoMes, Categoria, CartaoCredito
from importador import (DocumentValidationError, ProcessingTimeout, executar_cancelavel,
                        extrair_fechamento_pdf, identificar_documento, importar_csv,
                        importar_pdf, validar_csv, validar_imagem, validar_pdf)
from conciliador import executar_conciliacao_automatica
from metas import atualizar_status_metas, calcular_progresso, calcular_sugestao_aporte, obter_cor_prioridade, obter_cor_status
from graficos import obter_evolucao_saldo, obter_distribuicao_gastos, obter_progresso_metas, obter_gastos_por_categoria
from leitor_comprovante import extrair_dados_comprovante

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


# Carregamento automático de variáveis de ambiente do .env
env_file = os.path.join(BASE_DIR, '.env')
if os.path.exists(env_file):
    with open(env_file, 'r', encoding='utf-8') as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                if _k not in os.environ:
                    os.environ[_k] = _v

# Reject fingerprints of historical placeholder keys without publishing their values.
_PROVISIONAL_SECRET_HASHES = {
    'cf71f43ae64fd83be1a3487e0ca1fbd60126b2785b572d866981ac8bdc3ae62c',
    'a748df58e9144fc008498125ac83399edfc429fdc199ad8d31d172898f5d1c45',
    '1ddf1b83b5f4259b24ff5d7f7f45572771de8aef2f0138753ec85912f8d491e2',
}

def production_config_from_env(environ: Mapping[str, str]) -> dict[str, object]:
    production = environ.get('APP_ENV', 'development').lower() == 'production'
    secret = environ.get('SECRET_KEY', '')
    public_url = environ.get('APP_PUBLIC_URL', 'http://localhost:5000').rstrip('/')
    secure = environ.get('SESSION_COOKIE_SECURE', '0') == '1'
    if production and (len(secret) < 32 or hashlib.sha256(secret.encode()).hexdigest() in _PROVISIONAL_SECRET_HASHES):
        raise RuntimeError('SECRET_KEY de produção ausente ou provisória.')
    if production and (not public_url.startswith('https://') or not secure):
        raise RuntimeError('Produção exige APP_PUBLIC_URL HTTPS e SESSION_COOKIE_SECURE=1.')
    google_redirect = environ.get('GOOGLE_REDIRECT_URI', '')
    if production and google_redirect and not google_redirect.startswith('https://'):
        raise RuntimeError('GOOGLE_REDIRECT_URI de produção deve usar HTTPS.')
    trusted_proxy = environ.get('TRUSTED_PROXY', '')
    trusted_networks = tuple(item.strip() for item in
        environ.get('TRUSTED_PROXY_NETWORKS', '').split(',') if item.strip())
    if production and trusted_proxy and not trusted_networks:
        raise RuntimeError('TRUSTED_PROXY exige TRUSTED_PROXY_NETWORKS com IP/CIDR explícito.')
    return {'IS_PRODUCTION': production, 'SECRET_KEY': secret,
            'APP_PUBLIC_URL': public_url, 'SESSION_COOKIE_SECURE': secure,
            'TRUSTED_PROXY': trusted_proxy, 'TRUSTED_PROXY_NETWORKS': trusted_networks,
            'ENABLE_HSTS': environ.get('ENABLE_HSTS', '0') == '1'}

app = Flask(__name__)

def registrar_erro_operacional(message):
    correlation_id = secrets.token_hex(6)
    exc_info = sys.exc_info()
    sanitized = (type(exc_info[1]), RuntimeError('detalhes suprimidos'), exc_info[2])
    app.logger.error('%s; correlation_id=%s', message, correlation_id,
                     exc_info=sanitized)
    return correlation_id

class TrustedProxyMiddleware:
    def __init__(self, wsgi_app, app_ref):
        self.wsgi_app = wsgi_app
        self.app = app_ref
        self.proxy_fix = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        trusted_proxy = self.app.config.get('TRUSTED_PROXY') if self.app else False
        trusted_networks = self.app.config.get('TRUSTED_PROXY_NETWORKS', ()) if self.app else ()
        
        is_trusted = False
        if trusted_proxy and trusted_networks:
            remote_addr = environ.get('REMOTE_ADDR')
            if remote_addr:
                try:
                    ip = ipaddress.ip_address(remote_addr)
                    for net in trusted_networks:
                        if ip in ipaddress.ip_network(net, strict=False):
                            is_trusted = True
                            break
                except ValueError:
                    is_trusted = False

        if is_trusted:
            return self.proxy_fix(environ, start_response)
        else:
            cleaned_environ = environ.copy()
            for header in ('HTTP_X_FORWARDED_FOR', 'HTTP_X_FORWARDED_PROTO',
                           'HTTP_X_FORWARDED_HOST', 'HTTP_X_FORWARDED_PORT',
                           'HTTP_X_FORWARDED_PREFIX'):
                cleaned_environ.pop(header, None)
            return self.wsgi_app(cleaned_environ, start_response)

app.wsgi_app = TrustedProxyMiddleware(app.wsgi_app, app)

@app.template_filter('moeda')
def formatar_moeda(valor):
    return f'{float(valor):,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')

@app.context_processor
def links_filtros():
    def url_filtro(**alteracoes):
        parametros = request.args.to_dict()
        parametros.update(alteracoes)
        return url_for(request.endpoint, **{k: v for k, v in parametros.items() if v not in (None, '')})
    return {'url_filtro': url_filtro}

prod_config = production_config_from_env(os.environ)
app.config.update(prod_config)

# Configurações do Banco de Dados e Uploads
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or f'sqlite:///{os.path.join(BASE_DIR, "instance", "financeiro.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_BINDS'] = {'auth': os.environ.get('AUTH_DATABASE_URL') or (
    'sqlite:///:memory:' if app.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite:///:memory:' else
    f'sqlite:///{os.path.join(BASE_DIR, "instance", "usuarios.db")}')}

if prod_config['IS_PRODUCTION']:
    app.config['SECRET_KEY'] = prod_config['SECRET_KEY']
else:
    _secret_key = prod_config['SECRET_KEY']
    if len(_secret_key) < 32 or hashlib.sha256(_secret_key.encode()).hexdigest() in _PROVISIONAL_SECRET_HASHES:
        # Chave persistente e privada, inclusive após reiniciar o aplicativo.
        os.makedirs(os.path.join(BASE_DIR, 'instance'), exist_ok=True)
        _key_path = os.path.join(BASE_DIR, 'instance', 'session.key')
        try:
            with open(_key_path, 'x', encoding='ascii') as _key_file:
                _key_file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        with open(_key_path, encoding='ascii') as _key_file:
            _secret_key = _key_file.read().strip()
        if len(_secret_key) < 32:
            raise RuntimeError('Chave de sessão inválida em instance/session.key')
    app.config['SECRET_KEY'] = _secret_key

app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
app.config['ALLOWED_PROOF_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'pdf', 'webp'}
app.config['ALLOWED_STATEMENT_EXTENSIONS'] = {'csv', 'pdf'}
app.config.update(
    MAX_FILE_BYTES=10 * 1024 * 1024,
    MAX_MULTIPART_OVERHEAD_BYTES=256 * 1024,
    MAX_CONTENT_LENGTH=(10 * 1024 * 1024) + (256 * 1024),
    MAX_PDF_PAGES=25,
    MAX_IMAGE_PIXELS=25_000_000,
    MAX_IMAGE_DIMENSION=10_000,
    MAX_CSV_ROWS=100_000,
    MAX_CSV_COLUMNS=100,
    DOCUMENT_TIMEOUT_SECONDS=30,
    SHARE_OCR_TIMEOUT_SECONDS=15,
    SHARE_ACCOUNT_LIMIT='10 per 10 minutes',
    SHARE_IP_LIMIT='30 per hour',
    MAX_ACCOUNT_TEMP_BYTES=50 * 1024 * 1024,
    TEMP_MAX_AGE_SECONDS=3600,
)

# Políticas de Segurança de Sessão e Cookies
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = prod_config['SESSION_COOKIE_SECURE']
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# Garante que os diretórios necessários existam
os.makedirs(os.path.join(BASE_DIR, 'instance'), exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Inicializa o banco de dados
db.init_app(app)

# Ativa WAL mode no SQLite para suportar leituras/escritas concorrentes no Gunicorn
with app.app_context():
    @event.listens_for(db.engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

# Proteção CSRF global (token exigido em todo POST, exceto nas rotas isentas)
csrf = CSRFProtect(app)

from autenticacao import configurar_autenticacao
configurar_autenticacao(app)
from limits import parse as parse_rate_limit
from limits.strategies import FixedWindowRateLimiter
share_limiter = FixedWindowRateLimiter(app.extensions['financeiro_limiter'].storage)
share_account_limit = parse_rate_limit(app.config['SHARE_ACCOUNT_LIMIT'])
share_ip_limit = parse_rate_limit(app.config['SHARE_IP_LIMIT'])

def pasta_uploads():
    return getattr(g, 'uploads_folder', app.config['UPLOAD_FOLDER'])

def allowed_proof_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_PROOF_EXTENSIONS']

def allowed_statement_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_STATEMENT_EXTENSIONS']


def nome_comprovante_compartilhado(file):
    """Apps móveis podem compartilhar arquivos sem extensão ou com nome genérico."""
    if allowed_proof_file(file.filename):
        return file.filename
    try:
        if file.stream.read(5) == b'%PDF-':
            ext = 'pdf'
        else:
            from PIL import Image
            file.stream.seek(0)
            with Image.open(file.stream) as imagem:
                ext = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}.get(imagem.format)
                imagem.verify()
        return f'{file.filename}.{ext}' if ext else None
    except (OSError, ValueError):
        return None
    finally:
        file.stream.seek(0)

def gerar_nome_arquivo_seguro(prefixo, filename):
    """Gera nome de arquivo único com UUID e sanitização para prevenir colisões e sobreposições."""
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    safe_base = secure_filename(filename.rsplit('.', 1)[0])[:30]
    uid = uuid.uuid4().hex[:12]
    return f"{prefixo}_{uid}_{safe_base}.{ext}" if ext else f"{prefixo}_{uid}_{safe_base}"

def _caminho_seguro(caminho):
    """True se o caminho real estiver dentro da pasta de uploads (bloqueia path traversal)."""
    uploads_real = os.path.realpath(pasta_uploads())
    caminho_real = os.path.realpath(caminho)
    return caminho_real.startswith(uploads_real + os.sep)


def pasta_temporarios() -> str:
    """Retorna o diretório de arquivos temporários dentro da pasta de uploads da conta."""
    return str(os.path.join(pasta_uploads(), 'temporarios'))


def limpar_temporarios_expirados(now: float = None) -> int:
    """
    Remove arquivos em pasta_temporarios() com mtime anterior a now - TEMP_MAX_AGE_SECONDS.
    Retorna o número de arquivos removidos.
    """
    if now is None:
        import time
        now = time.time()
    max_age = app.config.get('TEMP_MAX_AGE_SECONDS', 3600)
    temp_dir = pasta_temporarios()
    if not os.path.isdir(temp_dir):
        return 0
    removed = 0
    for entry in os.scandir(temp_dir):
        if entry.is_file():
            try:
                if entry.stat().st_mtime < now - max_age:
                    os.remove(entry.path)
                    removed += 1
            except OSError:
                pass
    return removed


def reservar_temporario(upload, prefixo: str) -> str:
    """
    Salva `upload` (FileStorage) na pasta de temporários da conta.
    Verifica quota MAX_ACCOUNT_TEMP_BYTES antes de salvar.
    Registra o path em g.temporary_paths para cleanup no teardown.
    """
    from importador import DocumentValidationError
    limpar_temporarios_expirados()
    temp_dir = pasta_temporarios()
    os.makedirs(temp_dir, exist_ok=True)

    # Verifica quota atual
    max_bytes = app.config.get('MAX_ACCOUNT_TEMP_BYTES', 50 * 1024 * 1024)
    uso_atual = sum(
        entry.stat().st_size
        for entry in os.scandir(temp_dir)
        if entry.is_file()
    )

    # Lê o conteúdo para verificar tamanho antes de salvar
    conteudo = upload.stream.read(app.config['MAX_FILE_BYTES'] + 1)
    upload.stream.seek(0)
    if len(conteudo) > app.config['MAX_FILE_BYTES']:
        raise DocumentValidationError('O arquivo excede o limite de 10 MB.')

    if uso_atual + len(conteudo) > max_bytes:
        raise DocumentValidationError(
            'A conta atingiu a quota de 50 MB de arquivos temporários.'
        )

    filename = gerar_nome_arquivo_seguro(prefixo, upload.filename or 'arquivo.bin')
    path = os.path.join(temp_dir, filename)
    with open(path, 'wb') as fh:
        fh.write(conteudo)

    # Registra para cleanup automático no teardown
    if not hasattr(g, 'temporary_paths'):
        g.temporary_paths = set()
    g.temporary_paths.add(path)
    return path


def promover_temporario_pendente(path: str) -> str:
    """
    Move o arquivo para pending-{token_hex(16)}.{ext} na pasta de temporários.
    Remove do g.temporary_paths e salva pending_id na sessão.
    Retorna o pending_id opaco.
    """
    from flask import session
    descartar_compartilhamento_pendente()
    ext = os.path.splitext(path)[1].lstrip('.')
    pending_id = secrets.token_hex(16)
    pending_name = f'pending-{pending_id}.{ext}' if ext else f'pending-{pending_id}'
    temp_dir = pasta_temporarios()
    pending_path = os.path.join(temp_dir, pending_name)
    os.rename(path, pending_path)
    # Remove do cleanup automático (não deve ser deletado no teardown)
    if hasattr(g, 'temporary_paths'):
        g.temporary_paths.discard(path)
    session['pending_share_id'] = pending_id
    return pending_id


def descartar_compartilhamento_pendente():
    pending_id = session.pop('pending_share_id', None)
    if not pending_id or not re.fullmatch(r'[0-9a-f]{32}', pending_id):
        return
    temp_dir = pasta_temporarios()
    if os.path.isdir(temp_dir):
        for entry in os.scandir(temp_dir):
            if entry.name.startswith(f'pending-{pending_id}.'):
                finalizar_temporario(entry.path)


def consumir_temporario_pendente(pending_id: str) -> str:
    """
    Confere session, pop o pending_id, resolve o path confinado e move para local definitivo.
    Levanta DocumentValidationError se o ID não corresponder ou já foi consumido.
    """
    from flask import session
    from importador import DocumentValidationError
    session_id = session.get('pending_share_id')
    if not session_id or session_id != pending_id or not re.fullmatch(r'[0-9a-f]{32}', pending_id):
        raise DocumentValidationError('Comprovante pendente inválido ou já utilizado.')

    # Encontra o arquivo pelo pending_id no diretório de temporários
    temp_dir = pasta_temporarios()
    candidatos = [
        os.path.join(temp_dir, f)
        for f in os.listdir(temp_dir)
        if f.startswith(f'pending-{pending_id}.')
    ]
    if not candidatos:
        raise DocumentValidationError('Arquivo pendente não encontrado.')
    pending_path = candidatos[0]

    if not _caminho_seguro(pending_path):
        raise DocumentValidationError('Caminho de arquivo inválido.')

    ext = os.path.splitext(pending_path)[1].lstrip('.')
    final_name = gerar_nome_arquivo_seguro('comp', f'comprovante.{ext}')
    final_path = os.path.join(pasta_uploads(), final_name)
    os.rename(pending_path, final_path)
    session.pop('pending_share_id', None)
    if not hasattr(g, 'temporary_paths'):
        g.temporary_paths = set()
    g.temporary_paths.add(final_path)
    return final_name


def finalizar_temporario(path: str) -> None:
    """Remove um arquivo temporário se existir e estiver dentro de uploads."""
    try:
        if os.path.exists(path) and _caminho_seguro(path):
            os.remove(path)
    except OSError:
        pass


def guardar_anexo(upload, filename):
    path = os.path.join(pasta_uploads(), filename)
    if not hasattr(g, 'temporary_paths'):
        g.temporary_paths = set()
    g.temporary_paths.add(path)
    upload.save(path)


def confirmar_anexo(filename):
    if filename:
        getattr(g, 'temporary_paths', set()).discard(os.path.join(pasta_uploads(), filename))


def share_account_key() -> str:
    """Chave de rate limit por conta para o Share Target."""
    user_id = session.get('user', {}).get('id', 'anon')
    return f'share-account:{user_id}'


def share_ip_key() -> str:
    """Chave de rate limit por IP para o Share Target."""
    from flask_limiter.util import get_remote_address
    return f'share-ip:{get_remote_address()}'


@app.teardown_request
def _limpar_temporarios_request(exc):
    """Remove arquivos registrados em g.temporary_paths ao final de cada request."""
    paths = getattr(g, 'temporary_paths', set())
    for path in list(paths):
        finalizar_temporario(path)
    try:
        os.rmdir(pasta_temporarios())
    except OSError:
        pass


# Rate limits do Share Target aplicados via before_request para preservar
# compatibilidade com csrf.exempt já configurado.
@app.before_request
def _share_target_rate_limit():
    """Aplica rate limits de conta e IP apenas para POST /compartilhar-comprovante."""
    if request.path != '/compartilhar-comprovante' or request.method != 'POST':
        return
    if not share_limiter.hit(share_account_limit, share_account_key()):
        return jsonify(error='Limite de envios da conta atingido.'), 429
    if not share_limiter.hit(share_ip_limit, share_ip_key()):
        return jsonify(error='Limite de envios por IP atingido.'), 429


@app.before_request
def validar_comprovante_recebido():
    if request.endpoint == 'auth.logout':
        from models import Usuario
        user = db.session.get(Usuario, session.get('user', {}).get('id')) if session.get('user') else None
        if user:
            g.uploads_folder = app.config['UPLOAD_FOLDER'] if user.usa_dados_anteriores else os.path.join(app.config['UPLOAD_FOLDER'], 'contas', user.id)
            descartar_compartilhamento_pendente()
        return
    endpoints = {'compartilhar_comprovante', 'nova_pendencia_salvar', 'pagar_pendencia_salvar',
                 'quitar_fatura_cartao', 'editar_comprovante'}
    if request.method != 'POST' or request.endpoint not in endpoints:
        return
    upload = request.files.get('comprovante')
    if request.endpoint == 'compartilhar_comprovante' and (not upload or not upload.filename):
        upload = next((item for _, item in request.files.items(multi=True) if item.filename), None)
    if not upload or not upload.filename:
        return
    try:
        name = nome_comprovante_compartilhado(upload) if request.endpoint == 'compartilhar_comprovante' else upload.filename
        if not name or not allowed_proof_file(name):
            raise DocumentValidationError('Formato de comprovante inválido.')
        upload.filename = name
        path = reservar_temporario(upload, 'validacao')
        deadline = _time.monotonic() + float(app.config['SHARE_OCR_TIMEOUT_SECONDS'] if request.endpoint == 'compartilhar_comprovante' else app.config['DOCUMENT_TIMEOUT_SECONDS'])
        formato = executar_cancelavel(identificar_documento, (path, name.rsplit('.', 1)[1].lower(), dict(app.config)), deadline)
        if formato == 'pdf':
            executar_cancelavel(validar_pdf, (path, app.config['MAX_PDF_PAGES']), deadline)
        else:
            executar_cancelavel(validar_imagem, (path, app.config['MAX_IMAGE_PIXELS'], app.config['MAX_IMAGE_DIMENSION']), deadline)
        g.validated_upload = path
        g.document_deadline = deadline
    except DocumentValidationError as error:
        flash(str(error), 'danger')
        target = 'listar_historico' if request.endpoint == 'editar_comprovante' else 'nova_pendencia_form'
        return redirect(url_for(target, tipo='pago'), code=303)


# Injeta cabeçalhos de segurança HTTP em todas as respostas
@app.after_request
def add_security_headers(response):
    if request.path in ('/compartilhar-comprovante', '/pendencias/nova'):
        # Metadados apenas: nunca registrar valores, destinatários ou conteúdo do recibo.
        app.logger.warning('PWA recebimento: metodo=%s rota=%s status=%s formato=%s bytes=%s anexo_no_formulario=%s',
                           request.method, request.path, response.status_code, request.mimetype,
                           request.content_length, bool(request.args.get('comprovante_temp')))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy-Report-Only'] = (
        "default-src 'self'; img-src 'self' data:; font-src 'self' https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'self'")
    if app.config.get('ENABLE_HSTS') and request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    if not request.path.startswith('/static') and not request.path in ('/manifest.json', '/sw.js', '/icon.svg'):
        response.headers['Cache-Control'] = 'no-store'
    return response

# Rotas PWA
@app.route('/manifest.json')
def pwa_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def pwa_service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/icon.svg')
def pwa_icon():
    return send_from_directory('static', 'icon.svg', mimetype='image/svg+xml')

# Função auxiliar para atualizar o status das pendências atrasadas
def atualizar_status_atrasadas():
    hoje = date.today()
    atrasadas = Pendencia.query.filter(
        Pendencia.data_vencimento < hoje,
        Pendencia.status == 'aberto'
    ).all()
    for pendencia in atrasadas:
        pendencia.status = 'atrasado'
    if atrasadas:
        db.session.commit()

@app.before_request
def antes_da_requisicao():
    # Ignora rotas de assets estáticos e do PWA para poupar processamento e banco
    if request.blueprint == 'auth' or request.path.startswith('/static') or request.path in ('/manifest.json', '/sw.js', '/icon.svg'):
        return

    atualizar_status_atrasadas()
    atualizar_status_metas()

# Cria as tabelas do banco de dados na primeira execução
with app.app_context():
    db.create_all()
    # Adiciona colunas se não existirem
    for ddl in (
        'ALTER TABLE extrato_nubank ADD COLUMN categoria_id INTEGER',
        'ALTER TABLE pendencia ADD COLUMN cartao_id INTEGER',
        'ALTER TABLE pendencia ADD COLUMN parcela_atual INTEGER DEFAULT 1',
        'ALTER TABLE pendencia ADD COLUMN total_parcelas INTEGER DEFAULT 1',
        'ALTER TABLE pendencia ADD COLUMN is_assinatura BOOLEAN DEFAULT 0',
        'ALTER TABLE pendencia ADD COLUMN assinatura_ativa BOOLEAN DEFAULT 1',
    ):
        try:
            db.session.execute(db.text(ddl))
            db.session.commit()
        except db.exc.OperationalError:
            db.session.rollback()

# --- ROTAS ---

# GET / → Dashboard
@app.route('/')
def dashboard():
    hoje = date.today()
    amanha = hoje + timedelta(days=1)
    limite_vencimentos = hoje + timedelta(days=7)
    
    # Pendências abertas ou atrasadas
    pendencias_abertas = Pendencia.query.filter(Pendencia.status.in_(['aberto', 'atrasado'])).all()
    total_aberto = sum(p.valor for p in pendencias_abertas)
    
    # Contas pagas no mês atual
    inicio_mes = hoje.replace(day=1)
    contas_pagas_mes = Pendencia.query.filter(
        Pendencia.status == 'pago',
        Pendencia.data_pagamento >= inicio_mes
    ).all()
    total_pago_mes = sum(p.valor for p in contas_pagas_mes)

    # Contagem de tipos e valores
    total_atrasado = sum(p.valor for p in pendencias_abertas if p.status == 'atrasado')
    contas_atrasadas = len([p for p in pendencias_abertas if p.status == 'atrasado'])
    contas_abertas = len([p for p in pendencias_abertas if p.status == 'aberto'])

    # Geração de Alertas
    alertas = []
    for p in sorted(pendencias_abertas, key=lambda p: (p.data_vencimento, p.id)):
        if p.data_vencimento < hoje:
            tipo, classe = 'Atrasado', 'danger'
        elif p.data_vencimento == hoje:
            tipo, classe = 'Vence Hoje', 'warning'
        elif p.data_vencimento == amanha:
            tipo, classe = 'Vence Amanhã', 'info'
        elif p.data_vencimento <= limite_vencimentos:
            tipo, classe = 'Próximos 7 dias', 'info'
        else:
            continue
        alertas.append({
            "id": p.id, "tipo": tipo, "classe": classe,
            "credor": p.credor, "valor": p.valor,
            "vencimento": p.data_vencimento
        })

    # A soma do histórico não é o saldo bancário: pode faltar saldo inicial
    # ou haver meses sem importação. O fechamento contém o saldo do extrato.
    ultimo_fechamento = FechamentoMes.query.order_by(
        FechamentoMes.ano.desc(), FechamentoMes.mes.desc()
    ).first()
    if ultimo_fechamento:
        saldo_extrato = ultimo_fechamento.saldo_final
        saldo_titulo = 'Saldo do último extrato'
        saldo_referencia = f'Fechamento de {ultimo_fechamento.mes:02d}/{ultimo_fechamento.ano} · não é o saldo de hoje'
    else:
        saldo_extrato = db.session.query(
            db.func.coalesce(db.func.sum(ExtratoNubank.valor), 0.0)
        ).scalar() or 0.0
        saldo_titulo = 'Resultado das transações'
        saldo_referencia = 'Entradas − saídas importadas · saldo bancário indisponível'
    
    # Gastos do mês via extrato (saídas) com filtro SQL direto
    gastos_mes = abs(db.session.query(
        db.func.coalesce(db.func.sum(ExtratoNubank.valor), 0.0)
    ).filter(
        ExtratoNubank.valor < 0,
        ExtratoNubank.data >= inicio_mes
    ).scalar() or 0.0)
    
    # Metas em andamento
    metas_em_andamento = Meta.query.filter_by(status='em_andamento').count()
    
    # 5 últimas transações
    ultimas_transacoes = ExtratoNubank.query.order_by(ExtratoNubank.data.desc()).limit(5).all()
    
    # Sugestões de aporte para metas ativas
    metas_ativas = Meta.query.filter(Meta.status.in_(['em_andamento', 'atrasada'])).all()
    sugestoes_metas = []
    for m in metas_ativas:
        sug = calcular_sugestao_aporte(m)
        if sug:
            sugestoes_metas.append({
                "nome": m.nome,
                "icone": m.icone,
                "prioridade": m.prioridade,
                "cor_prioridade": obter_cor_prioridade(m.prioridade),
                "sugestao": sug
            })

    resumo = {
        "total_aberto": total_aberto,
        "total_atrasado": total_atrasado,
        "total_pago_mes": total_pago_mes,
        "contas_atrasadas": contas_atrasadas,
        "contas_abertas": contas_abertas,
        "contas_pagas": len(contas_pagas_mes),
        "saldo_extrato": round(saldo_extrato, 2),
        "saldo_titulo": saldo_titulo,
        "saldo_referencia": saldo_referencia,
        "saldo_apos_contas": round(saldo_extrato - total_aberto, 2) if ultimo_fechamento else None,
        "data_importacao": ultimo_fechamento.data_importacao if ultimo_fechamento else None,
        "gastos_mes": round(gastos_mes, 2),
        "metas_em_andamento": metas_em_andamento
    }

    return render_template('dashboard.html', resumo=resumo, alertas=alertas,
                           ultimas_transacoes=ultimas_transacoes,
                           sugestoes_metas=sugestoes_metas, hoje=hoje,
                           limite_vencimentos=limite_vencimentos)

# GET /pendencias → Listar pendências
@app.route('/pendencias')
def listar_pendencias():
    hoje = date.today()

    # Status já atualizados em atualizar_status_atrasadas() (before_request)
    pendencias_todas = Pendencia.query.filter(Pendencia.status.in_(['aberto', 'atrasado'])).all()

    # Métricas no topo
    total_atrasado = sum(p.valor for p in pendencias_todas if p.status == 'atrasado')
    count_atrasado = sum(1 for p in pendencias_todas if p.status == 'atrasado')
    
    proximos_7_dias = hoje + timedelta(days=7)
    vencendo_7dias = [p for p in pendencias_todas if hoje <= p.data_vencimento <= proximos_7_dias]
    total_7dias = sum(p.valor for p in vencendo_7dias)
    count_7dias = len(vencendo_7dias)

    vencendo_hoje = [p for p in pendencias_todas if p.data_vencimento == hoje]
    total_hoje = sum(p.valor for p in vencendo_hoje)
    count_hoje = len(vencendo_hoje)

    total_pendente_mes = sum(p.valor for p in pendencias_todas if p.data_vencimento.year == hoje.year and p.data_vencimento.month == hoje.month)
    total_geral = sum(p.valor for p in pendencias_todas)

    # Parâmetros de Filtro e Busca
    q = request.args.get('q', '').strip()
    filtro = request.args.get('filtro', 'todos')

    # 1) Consolidação dos Cartões de Crédito por fatura aberta mais recente (Fatura Atual ou Próxima Pendente)
    cartoes = CartaoCredito.query.order_by(CartaoCredito.nome).all()
    resumo_cartoes = []

    for c in cartoes:
        query_cartao = Pendencia.query.filter(
            Pendencia.cartao_id == c.id,
            Pendencia.status.in_(['aberto', 'atrasado'])
        )

        if q:
            search_filter = f"%{q}%"
            query_cartao = query_cartao.filter(db.or_(Pendencia.credor.ilike(search_filter), Pendencia.observacao.ilike(search_filter)))

        todos_itens_cartao = query_cartao.order_by(Pendencia.data_vencimento).all()

        if todos_itens_cartao:
            # Pega a menor data de vencimento em aberto para determinar a fatura pendente mais recente (atual ou próxima)
            primeiro_vencimento = min(p.data_vencimento for p in todos_itens_cartao)
            
            # Filtra apenas os itens da fatura deste mês/ano pendente
            itens_fatura = [
                p for p in todos_itens_cartao 
                if p.data_vencimento.year == primeiro_vencimento.year and p.data_vencimento.month == primeiro_vencimento.month
            ]
            
            total_fatura = sum(item.valor for item in itens_fatura)
            total_devedor = sum(item.valor for item in todos_itens_cartao)

            has_atrasado = any(item.status == 'atrasado' for item in itens_fatura) or (primeiro_vencimento < hoje)
            status_fatura = 'atrasado' if has_atrasado else 'aberto'
            data_venc_fatura = primeiro_vencimento
        else:
            itens_fatura = []
            total_fatura = 0.0
            total_devedor = 0.0
            status_fatura = 'aberto'
            max_day = calendar.monthrange(hoje.year, hoje.month)[1]
            data_venc_fatura = date(hoje.year, hoje.month, min(c.dia_vencimento, max_day))

        resumo_cartoes.append({
            'id': c.id,
            'nome': c.nome,
            'dia_vencimento': c.dia_vencimento,
            'data_vencimento_fatura': data_venc_fatura,
            'status_fatura': status_fatura,
            'cor': c.cor,
            'total_fatura': total_fatura,
            'total_devedor': total_devedor,
            'qtd_itens': len(itens_fatura),
            'qtd_itens_total': len(todos_itens_cartao),
            'itens': itens_fatura,
            'todos_itens': todos_itens_cartao
        })

    # 2) Lista Principal de Pendências (Exibe APENAS contas sem cartão vinculado)
    query = Pendencia.query.filter(Pendencia.status.in_(['aberto', 'atrasado']))

    if q:
        search_filter = f"%{q}%"
        query = query.filter(db.or_(Pendencia.credor.ilike(search_filter), Pendencia.observacao.ilike(search_filter)))

    if filtro == 'atrasado':
        query = query.filter(Pendencia.status == 'atrasado')
    elif filtro == 'hoje':
        query = query.filter(Pendencia.data_vencimento == hoje)
    elif filtro == 'proximos_7':
        query = query.filter(Pendencia.data_vencimento >= hoje, Pendencia.data_vencimento <= proximos_7_dias)
    elif filtro == 'mes':
        query = query.filter(
            db.extract('year', Pendencia.data_vencimento) == hoje.year,
            db.extract('month', Pendencia.data_vencimento) == hoje.month
        )

    todas_pendencias_filtradas = query.order_by(Pendencia.data_vencimento).all()

    # Oculta lançamentos com cartão da tabela geral de pendências
    pendencias = [p for p in todas_pendencias_filtradas if p.cartao_id is None]

    metricas = {
        'total_atrasado': total_atrasado,
        'count_atrasado': count_atrasado,
        'total_7dias': total_7dias,
        'count_7dias': count_7dias,
        'total_hoje': total_hoje,
        'count_hoje': count_hoje,
        'total_pendente_mes': total_pendente_mes,
        'total_geral': total_geral,
        'count_geral': len(pendencias_todas)
    }

    return render_template('pendencias.html', 
                           pendencias=pendencias, 
                           resumo_cartoes=resumo_cartoes,
                           cartoes=cartoes,
                           metricas=metricas, 
                           q=q, 
                           filtro_atual=filtro)

# POST/GET /compartilhar-comprovante → Web Share Target do Android / iOS
@app.route('/compartilhar-comprovante', methods=['GET', 'POST'])
def compartilhar_comprovante():
    if request.method == 'GET':
        return redirect(url_for('nova_pendencia_form'))

    file = request.files.get('comprovante')
    app.logger.warning('PWA compartilhamento: arquivos=%s campos_texto=%s arquivos_no_worker=%s',
                       len(list(request.files.items(multi=True))),
                       {key: len(request.form[key]) for key in ('title', 'text', 'url') if key in request.form},
                       request.headers.get('X-PWA-Shared-Files', 'sem-worker')[:8])
    if not file or not file.filename:
        file = next((item for _, item in request.files.items(multi=True) if item.filename), None)

    if file is None or not file.filename:
        flash('Nenhum arquivo foi recebido. Compartilhe a imagem ou PDF do comprovante, ou selecione o arquivo abaixo.', 'warning')
        return redirect(url_for('nova_pendencia_form', tipo='pago'), code=303)

    dados_extraidos = {
        'valor': None,
        'data': date.today(),
        'destinatario': '',
        'arquivo_nome': ''
    }

    if file and file.filename:
        nome_recebido = nome_comprovante_compartilhado(file)
        if not nome_recebido:
            flash('Formato de comprovante recebido inválido. Use .png, .jpg, .jpeg, .webp ou .pdf.', 'danger')
            return redirect(url_for('nova_pendencia_form', tipo='pago'), code=303)
        caminho_salvo = g.validated_upload
        try:
            deadline = g.document_deadline
            dados_extraidos = executar_cancelavel(extrair_dados_comprovante,
                                                  (caminho_salvo,), deadline)
        except (DocumentValidationError, ProcessingTimeout) as exc:
            db.session.rollback()
            try:
                os.remove(caminho_salvo)
            except OSError:
                pass
            flash(str(exc), 'danger')
            return redirect(url_for('nova_pendencia_form', tipo='pago'), code=303)

        dados_extraidos['arquivo_nome'] = promover_temporario_pendente(caminho_salvo)

    flash('Comprovante recebido! Confira os dados e confirme o lançamento.', 'info')
    return redirect(url_for('nova_pendencia_form',
                            origem='compartilhamento',
                            tipo='pago',
                            credor=dados_extraidos.get('destinatario') or dados_extraidos.get('descricao', ''),
                            valor=f"{dados_extraidos['valor']:.2f}" if dados_extraidos.get('valor') else '',
                            data=dados_extraidos.get('data', date.today()).strftime('%Y-%m-%d'),
                            comprovante_temp=dados_extraidos.get('arquivo_nome', '')), code=303)

# Exceção CSRF: Web Share Target (Android/iOS) não consegue enviar token CSRF
csrf.exempt(compartilhar_comprovante)

# GET /pendencias/nova → Formulário nova pendência
@app.route('/pendencias/nova', methods=['GET'])
def nova_pendencia_form():
    if not request.args.get('comprovante_temp'):
        descartar_compartilhamento_pendente()
    cartoes = CartaoCredito.query.order_by(CartaoCredito.nome).all()
    pre_dados = {
        'credor': request.args.get('credor', ''),
        'valor': request.args.get('valor', ''),
        'data': request.args.get('data', date.today().strftime('%Y-%m-%d')),
        'comprovante_temp': request.args.get('comprovante_temp', ''),
        'tipo': request.args.get('tipo', 'credito'),
        'origem': request.args.get('origem', '')
    }
    return render_template('nova_pendencia.html', cartoes=cartoes, pre_dados=pre_dados)

# POST /pendencias/nova → Salvar nova pendência (Crédito ou Já Pago)
@app.route('/pendencias/nova', methods=['POST'])
def nova_pendencia_salvar():
    try:
        tipo_lancamento = request.form.get('tipo_lancamento', 'credito')
        credor = request.form.get('credor')
        valor = float(request.form.get('valor'))
        observacao = request.form.get('observacao')

        if tipo_lancamento == 'pago':
            # Lançamento de conta já paga (Comprovante)
            data_pagamento_str = request.form.get('data_pagamento')
            data_pagto = datetime.strptime(data_pagamento_str, '%Y-%m-%d').date() if data_pagamento_str else date.today()

            comprovante_url = None
            if request.form.get('sem_anexo') == 'true':
                comprovante_url = "Comprovante manual (sem anexo)"
            else:
                file = request.files.get('comprovante')
                comprovante_temp = request.form.get('comprovante_temp')
                
                if file and file.filename:
                    if not allowed_proof_file(file.filename):
                        flash('Formato de comprovante inválido. Use .png, .jpg, .jpeg, .webp ou .pdf.', 'danger')
                        return redirect(url_for('nova_pendencia_form'))
                    filename = gerar_nome_arquivo_seguro("comprovante", file.filename)
                    guardar_anexo(file, filename)
                    comprovante_url = filename
                elif comprovante_temp:
                    try:
                        comprovante_url = consumir_temporario_pendente(comprovante_temp)
                    except DocumentValidationError:
                        flash('O comprovante anexado não foi encontrado ou já foi utilizado. Selecione o arquivo novamente.', 'danger')
                        return redirect(url_for('nova_pendencia_form', tipo='pago'))
                else:
                    comprovante_url = "Comprovante manual (sem anexo)"

            nova = Pendencia(
                credor=credor,
                valor=valor,
                data_vencimento=data_pagto,
                data_pagamento=data_pagto,
                observacao=observacao,
                status='pago',
                comprovante_url=comprovante_url
            )
            db.session.add(nova)
            db.session.commit()
            
            confirmar_anexo(comprovante_url)
            descartar_compartilhamento_pendente()
            flash('Compra cadastrada e salva em Comprovantes com sucesso!', 'success')
            return redirect(url_for('listar_historico'))
        else:
            # Lançamento Crédito (A Pagar com suporte a Cartão, Parcelas e Assinaturas)
            cartao_id_raw = request.form.get('cartao_id')
            cartao_id = int(cartao_id_raw) if cartao_id_raw else None
            cartao_obj = CartaoCredito.query.get(cartao_id) if cartao_id else None

            is_assinatura = request.form.get('is_assinatura') in ['true', '1', 'on']

            if is_assinatura:
                data_vencimento_str = request.form.get('data_vencimento')
                data_venc = datetime.strptime(data_vencimento_str, '%Y-%m-%d').date() if data_vencimento_str else date.today()

                status = 'aberto'
                if data_venc < date.today():
                    status = 'atrasado'

                nova = Pendencia(
                    credor=credor,
                    valor=valor,
                    data_vencimento=data_venc,
                    observacao=observacao,
                    status=status,
                    cartao_id=cartao_id,
                    is_assinatura=True,
                    assinatura_ativa=True,
                    parcela_atual=1,
                    total_parcelas=1
                )
                db.session.add(nova)
                db.session.commit()
                
                flash(f'Assinatura de "{credor}" (R$ {valor:.2f}/mês) cadastrada com sucesso! Ela ficará ativa na fatura até ser cancelada.', 'success')
                return redirect(url_for('listar_pendencias'))

            num_parcelas = int(request.form.get('num_parcelas') or 1)

            if num_parcelas > 1:
                valor_total = valor
                valor_parcela = round(valor_total / num_parcelas, 2)
                base_venc_str = request.form.get('data_vencimento')
                base_date = datetime.strptime(base_venc_str, '%Y-%m-%d').date() if base_venc_str else date.today()

                import calendar
                for i in range(1, num_parcelas + 1):
                    month_offset = i - 1
                    target_month = (base_date.month - 1 + month_offset) % 12 + 1
                    target_year = base_date.year + (base_date.month - 1 + month_offset) // 12
                    
                    day = cartao_obj.dia_vencimento if cartao_obj else base_date.day
                    max_days = calendar.monthrange(target_year, target_month)[1]
                    day = min(day, max_days)

                    data_venc_i = date(target_year, target_month, day)
                    status_i = 'atrasado' if data_venc_i < date.today() else 'aberto'

                    nova = Pendencia(
                        credor=f"{credor} ({i}/{num_parcelas})",
                        valor=valor_parcela,
                        data_vencimento=data_venc_i,
                        observacao=observacao,
                        status=status_i,
                        cartao_id=cartao_id,
                        parcela_atual=i,
                        total_parcelas=num_parcelas,
                        is_assinatura=False,
                        assinatura_ativa=False
                    )
                    db.session.add(nova)
                
                db.session.commit()
                flash(f'Compra em crédito de R$ {valor_total:.2f} parcelada em {num_parcelas}x (R$ {valor_parcela:.2f}/mês) cadastrada!', 'success')
                return redirect(url_for('listar_pendencias'))
            else:
                data_vencimento_str = request.form.get('data_vencimento')
                data_venc = datetime.strptime(data_vencimento_str, '%Y-%m-%d').date()

                status = 'aberto'
                if data_venc < date.today():
                    status = 'atrasado'

                nova = Pendencia(
                    credor=credor,
                    valor=valor,
                    data_vencimento=data_venc,
                    observacao=observacao,
                    status=status,
                    cartao_id=cartao_id,
                    parcela_atual=1,
                    total_parcelas=1,
                    is_assinatura=False,
                    assinatura_ativa=False
                )
                db.session.add(nova)
                db.session.commit()
                
                flash('Conta a pagar (Crédito) cadastrada com sucesso!', 'success')
                return redirect(url_for('listar_pendencias'))
    except Exception:
        db.session.rollback()
        correlation_id = registrar_erro_operacional('Falha ao cadastrar')
        flash(f'Não foi possível cadastrar. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('nova_pendencia_form'))

# POST /cartoes/novo → Criar novo Grupo de Cartão de Crédito
@app.route('/cartoes/novo', methods=['POST'])
def novo_cartao():
    try:
        nome = request.form.get('nome')
        dia_vencimento = int(request.form.get('dia_vencimento'))
        cor = request.form.get('cor', '#8b5cf6')

        cartao = CartaoCredito(nome=nome, dia_vencimento=dia_vencimento, cor=cor)
        db.session.add(cartao)
        db.session.commit()
        flash(f'Grupo de cartão "{nome}" (Vencimento dia {dia_vencimento}) criado com sucesso!', 'success')
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao criar cartão de crédito')
        flash(f'Não foi possível criar o cartão de crédito. Referência: {correlation_id}.', 'danger')
    return redirect(url_for('listar_pendencias'))

# POST /cartoes/deletar/<id> → Deletar cartão de crédito
@app.route('/cartoes/deletar/<int:id>', methods=['POST'])
def deletar_cartao(id):
    cartao = CartaoCredito.query.get_or_404(id)
    db.session.delete(cartao)
    db.session.commit()
    flash('Grupo de cartão de crédito removido com sucesso!', 'success')
    return redirect(url_for('listar_pendencias'))

# POST /cartoes/quitar_fatura/<id> → Confirmar pagamento da fatura do cartão
@app.route('/cartoes/quitar_fatura/<int:id>', methods=['POST'])
def quitar_fatura_cartao(id):
    cartao = CartaoCredito.query.get_or_404(id)
    data_pagamento_str = request.form.get('data_pagamento')
    data_pagto = datetime.strptime(data_pagamento_str, '%Y-%m-%d').date() if data_pagamento_str else date.today()

    comprovante_url = None
    if request.form.get('sem_anexo') == 'true':
        comprovante_url = f"Fatura {cartao.nome} quitada (sem anexo)"
    else:
        file = request.files.get('comprovante')
        if file and file.filename:
            if not allowed_proof_file(file.filename):
                flash(f'Formato de comprovante inválido ao quitar a fatura de "{cartao.nome}".', 'danger')
                return redirect(url_for('listar_pendencias'))
            filename = gerar_nome_arquivo_seguro(f"fatura_{secure_filename(cartao.nome)}", file.filename)
            guardar_anexo(file, filename)
            comprovante_url = filename
        else:
            comprovante_url = f"Fatura {cartao.nome} quitada manualmente"

    todos_itens_abertos = Pendencia.query.filter(
        Pendencia.cartao_id == cartao.id,
        Pendencia.status.in_(['aberto', 'atrasado'])
    ).order_by(Pendencia.data_vencimento).all()

    if todos_itens_abertos:
        primeiro_vencimento = min(item.data_vencimento for item in todos_itens_abertos)
        itens = [
            item for item in todos_itens_abertos 
            if item.data_vencimento.year == primeiro_vencimento.year and item.data_vencimento.month == primeiro_vencimento.month
        ]

        import calendar
        for item in itens:
            item.status = 'pago'
            item.data_pagamento = data_pagto
            item.comprovante_url = comprovante_url

            # Se for uma assinatura ativa, renova automaticamente gerando a cobrança do próximo mês
            if getattr(item, 'is_assinatura', False) and getattr(item, 'assinatura_ativa', True):
                target_month = (item.data_vencimento.month) % 12 + 1
                target_year = item.data_vencimento.year + (item.data_vencimento.month // 12)
                day = cartao.dia_vencimento if cartao else item.data_vencimento.day
                max_days = calendar.monthrange(target_year, target_month)[1]
                day = min(day, max_days)
                prox_venc = date(target_year, target_month, day)

                nova_sub = Pendencia(
                    credor=item.credor,
                    valor=item.valor,
                    data_vencimento=prox_venc,
                    observacao=item.observacao,
                    status='aberto',
                    cartao_id=item.cartao_id,
                    is_assinatura=True,
                    assinatura_ativa=True,
                    parcela_atual=1,
                    total_parcelas=1
                )
                db.session.add(nova_sub)

        db.session.commit()

        confirmar_anexo(comprovante_url)
        flash(f'Fatura do {cartao.nome} ({primeiro_vencimento.strftime("%m/%Y")}) quitada com sucesso! ({len(itens)} item(ns) baixado(s))', 'success')
    else:
        flash(f'Nenhum lançamento aberto encontrado para o cartão {cartao.nome}.', 'info')

    return redirect(url_for('listar_pendencias'))

# POST /pendencias/cancelar_assinatura/<id> → Cancelar assinatura recorrente
@app.route('/pendencias/cancelar_assinatura/<int:id>', methods=['POST'])
def cancelar_assinatura(id):
    pendencia = Pendencia.query.get_or_404(id)
    credor = pendencia.credor
    pendencia.assinatura_ativa = False
    if pendencia.status in ['aberto', 'atrasado']:
        db.session.delete(pendencia)
    db.session.commit()
    flash(f'Assinatura "{credor}" cancelada com sucesso! Ela foi removida da fatura do cartão.', 'success')
    return redirect(url_for('listar_pendencias'))

# GET /pendencias/editar/<id> → Formulário editar
@app.route('/pendencias/editar/<int:id>', methods=['GET'])
def editar_pendencia_form(id):
    pendencia = Pendencia.query.get_or_404(id)
    return render_template('editar_pendencia.html', pendencia=pendencia)

# POST /pendencias/editar/<id> → Salvar edição
@app.route('/pendencias/editar/<int:id>', methods=['POST'])
def editar_pendencia_salvar(id):
    pendencia = Pendencia.query.get_or_404(id)
    try:
        pendencia.credor = request.form.get('credor')
        pendencia.valor = float(request.form.get('valor'))
        data_venc_str = request.form.get('data_vencimento')
        pendencia.data_vencimento = datetime.strptime(data_venc_str, '%Y-%m-%d').date()
        pendencia.observacao = request.form.get('observacao')

        if pendencia.status != 'pago':
            pendencia.status = 'atrasado' if pendencia.data_vencimento < date.today() else 'aberto'

        db.session.commit()
        flash('Pendência atualizada com sucesso!', 'success')
        return redirect(url_for('listar_pendencias'))
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao editar pendência')
        flash(f'Não foi possível editar a pendência. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('editar_pendencia_form', id=id))

# POST /pendencias/deletar/<id> → Deletar pendência
@app.route('/pendencias/deletar/<int:id>', methods=['POST'])
def deletar_pendencia(id):
    pendencia = Pendencia.query.get_or_404(id)
    db.session.delete(pendencia)
    db.session.commit()
    flash('Pendência removida com sucesso!', 'success')
    return redirect(url_for('listar_pendencias'))

# GET /pendencias/pagar/<id> → Modal pagar pendência (Provisório para manter compatibilidade)
@app.route('/pendencias/pagar/<int:id>', methods=['GET'])
def pagar_pendencia_modal(id):
    # Como o pagamento agora é via modal na lista de pendências, apenas redireciona para lá
    return redirect(url_for('listar_pendencias'))

# POST /pendencias/pagar_rapido/<id> → Confirmar pagamento em 1 clique
@app.route('/pendencias/pagar_rapido/<int:id>', methods=['POST'])
def pagar_rapido(id):
    pendencia = Pendencia.query.get_or_404(id)
    try:
        pendencia.status = 'pago'
        pendencia.data_pagamento = date.today()
        pendencia.comprovante_url = "Pagamento rápido em 1 clique (sem comprovante)"
        db.session.commit()
        flash(f'Pagamento rápido da conta "{pendencia.credor}" confirmado!', 'success')
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao registrar pagamento rápido')
        flash(f'Não foi possível registrar o pagamento rápido. Referência: {correlation_id}.', 'danger')
    return redirect(url_for('listar_pendencias'))

# POST /pendencias/pagar/<id> → Salvar pagamento
@app.route('/pendencias/pagar/<int:id>', methods=['POST'])
def pagar_pendencia_salvar(id):
    pendencia = Pendencia.query.get_or_404(id)
    try:
        data_pag_str = request.form.get('data_pagamento')
        data_pag = datetime.strptime(data_pag_str, '%Y-%m-%d').date()

        comprovante_url = None
        file = request.files.get('comprovante')
        if file and allowed_proof_file(file.filename):
            filename = gerar_nome_arquivo_seguro(f"comprovante_{id}", file.filename)
            file_path = os.path.join(pasta_uploads(), filename)
            guardar_anexo(file, filename)
            comprovante_url = filename
        else:
            comprovante_url = "Comprovante manual (sem anexo)"

        pendencia.status = 'pago'
        pendencia.data_pagamento = data_pag
        pendencia.comprovante_url = comprovante_url

        # Criar pendência para próximo mês se checkbox marcado
        repetir_proximo = request.form.get('repetir_proximo')
        if repetir_proximo == '1':
            proximo_vencimento_str = request.form.get('proximo_vencimento')
            proximo_valor_str = request.form.get('proximo_valor')
            if proximo_vencimento_str and proximo_valor_str:
                nova_pendencia = Pendencia(
                    credor=pendencia.credor,
                    valor=float(proximo_valor_str),
                    data_vencimento=datetime.strptime(proximo_vencimento_str, '%Y-%m-%d').date(),
                    observacao=pendencia.observacao,
                    status='aberto'
                )
                db.session.add(nova_pendencia)
                flash(f'Pagamento registrado e nova pendência criada para {proximo_vencimento_str}!', 'success')
            else:
                flash(f'Pagamento da conta "{pendencia.credor}" registrado!', 'success')
        else:
            flash(f'Pagamento da conta "{pendencia.credor}" registrado!', 'success')

        db.session.commit()

        confirmar_anexo(comprovante_url)
        return redirect(url_for('listar_pendencias'))
    except Exception:
        db.session.rollback()
        correlation_id = registrar_erro_operacional('Falha ao registrar pagamento')
        flash(f'Não foi possível registrar o pagamento. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('listar_pendencias'))

# GET /historico → Listar contas pagas
@app.route('/historico')
def listar_historico():
    q = request.args.get('q', '').strip()
    mes_filtro = request.args.get('mes', '').strip()

    query = Pendencia.query.filter_by(status='pago')

    if q:
        search_filter = f"%{q}%"
        query = query.filter(db.or_(Pendencia.credor.ilike(search_filter), Pendencia.observacao.ilike(search_filter)))

    if mes_filtro:
        try:
            ano, mes = map(int, mes_filtro.split('-'))
            primeiro_dia = date(ano, mes, 1)
            if mes == 12:
                ultimo_dia = date(ano + 1, 1, 1) - timedelta(days=1)
            else:
                ultimo_dia = date(ano, mes + 1, 1) - timedelta(days=1)
            query = query.filter(Pendencia.data_pagamento >= primeiro_dia, Pendencia.data_pagamento <= ultimo_dia)
        except ValueError:
            pass

    pagos = query.order_by(Pendencia.data_pagamento.desc()).all()
    total_pago = sum(p.valor for p in pagos)

    return render_template('historico.html', pagos=pagos, total_pago=total_pago, q=q, mes_filtro=mes_filtro)

# GET /transacoes → Listar transações do extrato Nubank
@app.route('/transacoes')
def listar_transacoes():
    query = ExtratoNubank.query
    filtro_q = request.args.get('q', '').strip()
    filtro_categoria = request.args.get('categoria', '')
    if filtro_q:
        query = query.filter(ExtratoNubank.descricao.contains(filtro_q, autoescape=True))
    if filtro_categoria:
        query = query.filter(ExtratoNubank.categoria_id == filtro_categoria)

    filtro_tipo = request.args.get('tipo')
    filtro_data_inicio = request.args.get('data_inicio')
    filtro_data_fim = request.args.get('data_fim')
    filtro_mes = request.args.get('mes')
    sort = request.args.get('sort', 'data')
    sort_dir = request.args.get('sort_dir', 'desc')

    if filtro_tipo:
        query = query.filter_by(tipo=filtro_tipo)

    if filtro_mes:
        # Formato: YYYY-MM
        ano, mes = map(int, filtro_mes.split('-'))
        primeiro_dia = date(ano, mes, 1)
        # Último dia do mês
        if mes == 12:
            ultimo_dia = date(ano + 1, 1, 1) - timedelta(days=1)
        else:
            ultimo_dia = date(ano, mes + 1, 1) - timedelta(days=1)
        query = query.filter(ExtratoNubank.data >= primeiro_dia)
        query = query.filter(ExtratoNubank.data <= ultimo_dia)
    else:
        if filtro_data_inicio:
            data_inicio = datetime.strptime(filtro_data_inicio, '%Y-%m-%d').date()
            query = query.filter(ExtratoNubank.data >= data_inicio)
        if filtro_data_fim:
            data_fim = datetime.strptime(filtro_data_fim, '%Y-%m-%d').date()
            query = query.filter(ExtratoNubank.data <= data_fim)

    # Ordenação
    sort_columns = {
        'data': ExtratoNubank.data,
        'descricao': ExtratoNubank.descricao,
        'valor': ExtratoNubank.valor,
        'categoria': ExtratoNubank.categoria_id
    }
    sort_col = sort_columns.get(sort, ExtratoNubank.data)
    if sort_dir == 'asc':
        transacoes = query.order_by(sort_col.asc()).all()
    else:
        transacoes = query.order_by(sort_col.desc()).all()

    total_entradas = sum(t.valor for t in transacoes if t.valor > 0)
    total_saidas = abs(sum(t.valor for t in transacoes if t.valor < 0))
    saldo = total_entradas - total_saidas

    # Fechamento do último mês
    fechamento = FechamentoMes.query.order_by(
        FechamentoMes.ano.desc(),
        FechamentoMes.mes.desc()
    ).first()

    return render_template('transacoes.html',
                           transacoes=transacoes,
                           filtro_q=filtro_q,
                           filtro_categoria=filtro_categoria,
                           categorias=Categoria.query.order_by(Categoria.nome).all(),
                           total_entradas=round(total_entradas, 2),
                           total_saidas=round(total_saidas, 2),
                           saldo=round(saldo, 2),
                           fechamento=fechamento,
                           filtro_tipo=filtro_tipo or '',
                           filtro_data_inicio=filtro_data_inicio or '',
                           filtro_data_fim=filtro_data_fim or '',
                           filtro_mes=filtro_mes or '',
                           sort=sort,
                           sort_dir=sort_dir)

def detectar_mime_validado(file_path: str) -> str:
    """
    Deriva o MIME type a partir do conteúdo validado do arquivo.
    Mapeia formatos canônicos para MIME types.
    Em caso de falha de validação, levanta DocumentValidationError.
    """
    from importador import identificar_documento, DocumentValidationError
    ext = os.path.splitext(file_path)[1].lstrip('.').lower()
    if not ext:
        raise DocumentValidationError('Extensão de arquivo desconhecida.')
    limites = {
        'MAX_FILE_BYTES': app.config.get('MAX_FILE_BYTES', 10 * 1024 * 1024),
        'MAX_PDF_PAGES': app.config.get('MAX_PDF_PAGES', 25),
        'MAX_IMAGE_PIXELS': app.config.get('MAX_IMAGE_PIXELS', 25_000_000),
        'MAX_IMAGE_DIMENSION': app.config.get('MAX_IMAGE_DIMENSION', 10_000),
        'MAX_CSV_ROWS': app.config.get('MAX_CSV_ROWS', 100_000),
        'MAX_CSV_COLUMNS': app.config.get('MAX_CSV_COLUMNS', 100),
    }
    fmt = identificar_documento(file_path, ext, limites)
    mime_map = {
        'pdf': 'application/pdf',
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'webp': 'image/webp',
        'csv': 'text/csv',
    }
    return mime_map.get(fmt, 'application/octet-stream')


# GET /historico/<id> → Visualizar comprovante
@app.route('/historico/<int:id>')
def visualizar_comprovante(id):
    from importador import DocumentValidationError
    pendencia = Pendencia.query.get_or_404(id)
    if not pendencia.comprovante_url or pendencia.comprovante_url.startswith('Comprovante'):
        flash('Nenhum arquivo físico anexado para este pagamento.', 'info')
        return redirect(url_for('listar_historico'))

    file_path = os.path.join(pasta_uploads(), pendencia.comprovante_url)
    if not os.path.exists(file_path):
        flash('O arquivo do comprovante não foi localizado. Você pode anexá-lo novamente clicando no botão Editar (✂️).', 'warning')
        return redirect(url_for('listar_historico'))

    # Garante que o arquivo está dentro da pasta de uploads (evita path traversal)
    if not _caminho_seguro(file_path):
        flash('Caminho de arquivo inválido.', 'danger')
        return redirect(url_for('listar_historico'))

    try:
        mime = detectar_mime_validado(file_path)
    except DocumentValidationError:
        correlation_id = registrar_erro_operacional('Comprovante com formato inválido na entrega')
        flash(f'Arquivo de comprovante indisponível. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('listar_historico'))

    response = send_from_directory(
        pasta_uploads(),
        pendencia.comprovante_url,
        mimetype=mime,
        as_attachment=True,
        download_name=pendencia.comprovante_url,
    )
    response.headers['Cache-Control'] = 'no-store'
    return response

# POST /historico/editar/<id> → Editar comprovante
@app.route('/historico/editar/<int:id>', methods=['POST'])
def editar_comprovante(id):
    pendencia = Pendencia.query.get_or_404(id)
    old_name = pendencia.comprovante_url
    file = request.files.get('comprovante')
    if request.form.get('sem_anexo') == 'true':
        pendencia.comprovante_url = "Comprovante manual (sem anexo)"
    elif file and file.filename:
        filename = gerar_nome_arquivo_seguro(f"comprovante_{id}", file.filename)
        guardar_anexo(file, filename)
        pendencia.comprovante_url = filename
    if request.form.get('observacao') is not None:
        pendencia.observacao = request.form.get('observacao')
    db.session.commit()
    confirmar_anexo(pendencia.comprovante_url)
    if old_name and old_name != pendencia.comprovante_url and not old_name.startswith('Comprovante'):
        if not Pendencia.query.filter_by(comprovante_url=old_name).first():
            finalizar_temporario(os.path.join(pasta_uploads(), old_name))
    flash('Comprovante atualizado com sucesso!', 'success')
    return redirect(url_for('listar_historico'))

# POST /historico/apagar/<id> → Apagar item do histórico
@app.route('/historico/apagar/<int:id>', methods=['POST'])
def apagar_historico(id):
    pendencia = Pendencia.query.get_or_404(id)

    # Remove arquivo físico se existir
    if pendencia.comprovante_url and not pendencia.comprovante_url.startswith('Comprovante'):
        file_path = os.path.join(pasta_uploads(), pendencia.comprovante_url)
        if os.path.exists(file_path) and _caminho_seguro(file_path):
            os.remove(file_path)

    db.session.delete(pendencia)
    db.session.commit()
    flash('Item removido do histórico com sucesso!', 'success')
    return redirect(url_for('listar_historico'))

# POST /historico/desfazer/<id> → Reverter pagamento de volta para pendência em aberto
@app.route('/historico/desfazer/<int:id>', methods=['POST'])
def desfazer_historico(id):
    pendencia = Pendencia.query.get_or_404(id)
    if pendencia.status == 'pago':
        hoje = date.today()
        pendencia.status = 'atrasado' if pendencia.data_vencimento < hoje else 'aberto'
        pendencia.data_pagamento = None
        db.session.commit()
        flash(f'Pagamento de "{pendencia.credor}" desfeito com sucesso! A conta retornou para Pendências.', 'info')
    return redirect(url_for('listar_historico'))

# GET /importar → Página de upload
@app.route('/importar', methods=['GET'])
def importar_page():
    return render_template('importar.html')

# POST /importar → Fazer upload do arquivo
@app.route('/importar', methods=['POST'])
def importar_arquivo():
    file = request.files.get('extrato')
    if not file or file.filename == '':
        flash('Nenhum arquivo enviado.', 'danger')
        return redirect(url_for('importar_page'))
    
    if not allowed_statement_file(file.filename):
        flash('Formato de arquivo inválido. Apenas .csv ou .pdf são permitidos.', 'danger')
        return redirect(url_for('importar_page'))
    
    file_path = None
    try:
        deadline = _time.monotonic() + float(app.config['DOCUMENT_TIMEOUT_SECONDS'])
        # Salva o arquivo temporariamente na pasta de uploads com nome seguro e único
        file_path = reservar_temporario(file, 'extrato')
        filename = os.path.basename(file_path)
        extensao = file.filename.rsplit('.', 1)[1].lower()
        formato = executar_cancelavel(identificar_documento,
                                      (file_path, extensao, dict(app.config)), deadline)

        # Toda validação e extração termina antes da primeira consulta/criação ORM.
        if formato == 'csv':
            executar_cancelavel(validar_csv,
                                (file_path, app.config['MAX_CSV_ROWS'],
                                 app.config['MAX_CSV_COLUMNS']), deadline)
            transacoes = executar_cancelavel(importar_csv, (file_path,), deadline)
            fechamento = None
        else:
            executar_cancelavel(validar_pdf,
                                (file_path, app.config['MAX_PDF_PAGES']), deadline)
            transacoes = executar_cancelavel(importar_pdf, (file_path,), deadline)
            fechamento = executar_cancelavel(extrair_fechamento_pdf, (file_path,), deadline)

        novas_transacoes = 0
        import re
        from collections import defaultdict

        def desc_normalizada(text):
            cleaned = re.sub(r'^(compra no d[ée]bito|compra no cr[ée]dito|transfer[êe]ncia enviada pelo pix|transfer[êe]ncia recebida pelo pix|pagamento de boleto|pagamento de fatura)\s*[\-\s]*', '', text, flags=re.IGNORECASE)
            return re.sub(r'\s+', ' ', cleaned).strip().lower()

        # OTIMIZAÇÃO: Pré-carrega todas as regras de categorias em 1 única query
        regras_categorias = Categoria.query.all()

        # OTIMIZAÇÃO: Pré-carrega transações existentes no intervalo de datas em 1 única query
        datas_arquivo = [t['data'] for t in transacoes if t.get('data')]
        cache_candidatos = defaultdict(list)
        if datas_arquivo:
            min_data, max_data = min(datas_arquivo), max(datas_arquivo)
            existentes_intervalo = ExtratoNubank.query.filter(
                ExtratoNubank.data.between(min_data, max_data)
            ).all()
            for ex in existentes_intervalo:
                cache_candidatos[(ex.data, round(ex.valor, 2))].append(ex)

        for t in transacoes:
            candidatos = cache_candidatos.get((t['data'], round(t['valor'], 2)), [])

            existe = False
            t_norm = desc_normalizada(t['descricao'])

            for c in candidatos:
                c_norm = desc_normalizada(c.descricao)
                if c.descricao.strip().lower() == t['descricao'].strip().lower() or (t_norm and c_norm and (t_norm in c_norm or c_norm in t_norm)):
                    existe = True
                    break

            if not existe:
                novo_registro = ExtratoNubank(
                    data=t['data'],
                    descricao=t['descricao'],
                    valor=t['valor'],
                    tipo=t['tipo'],
                    arquivo_original=filename,
                    conciliado=False
                )
                # Busca regra de categorização na lista pré-carregada em memória
                t_desc_lower = t['descricao'].lower()
                for regra in regras_categorias:
                    if regra.credor.lower() in t_desc_lower:
                        novo_registro.categoria_id = regra.id
                        break
                db.session.add(novo_registro)
                cache_candidatos[(t['data'], round(t['valor'], 2))].append(novo_registro)
                novas_transacoes += 1
                
        if novas_transacoes > 0:
            db.session.commit()

        # Atualiza fechamento do mês (só se for PDF e for um mês mais recente)
        if fechamento:
            existente = FechamentoMes.query.filter_by(
                ano=fechamento['ano'],
                mes=fechamento['mes']
            ).first()

            if existente:
                # Se já existe, não fazer nada (dados do passado não substituem)
                pass
            else:
                # Só insere se for um novo mês (ou seja, mais recente que os existentes)
                ultimos_fechamentos = FechamentoMes.query.order_by(
                    FechamentoMes.ano.desc(),
                    FechamentoMes.mes.desc()
                ).limit(1).first()

                pode_inserir = True
                if ultimos_fechamentos:
                    if fechamento['ano'] < ultimos_fechamentos.ano:
                        pode_inserir = False
                    elif fechamento['ano'] == ultimos_fechamentos.ano and fechamento['mes'] <= ultimos_fechamentos.mes:
                        pode_inserir = False

                if pode_inserir:
                    novo_fechamento = FechamentoMes(
                        ano=fechamento['ano'],
                        mes=fechamento['mes'],
                        saldo_inicial=fechamento['saldo_inicial'],
                        saldo_final=fechamento['saldo_final'],
                        total_entradas=fechamento['total_entradas'],
                        total_saidas=fechamento['total_saidas'],
                        arquivo_original=filename
                    )
                    db.session.add(novo_fechamento)
                    db.session.commit()

        # Dispara a conciliação automática
        sugestoes = executar_conciliacao_automatica()
        
        if not transacoes:
            flash('Nenhuma transação foi identificada. Confira se o arquivo é um extrato Nubank em PDF ou CSV e tente novamente.', 'warning')
            return redirect(url_for('importar_page'))
        repetidas = len(transacoes) - novas_transacoes
        flash(f'Importação concluída: {len(transacoes)} transações lidas · {novas_transacoes} adicionadas · {repetidas} identificadas como duplicadas e ignoradas. {sugestoes} sugestões de conciliação encontradas. Confira as transações abaixo.', 'success')
        return redirect(url_for('listar_transacoes'))
        
    except (DocumentValidationError, ProcessingTimeout) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('importar_page'))
    except Exception:
        db.session.rollback()
        correlation_id = registrar_erro_operacional('Falha ao processar importação')
        flash(f'Não foi possível processar o arquivo. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('importar_page'))
    finally:
        if file_path:
            try:
                if os.path.exists(file_path) and _caminho_seguro(file_path):
                    os.remove(file_path)
            except OSError:
                pass


# ============================
# ROTAS DE CATEGORIAS
# ============================

@app.route('/categorias')
def listar_categorias():
    """Lista todas as categorias e obtém nomes distintos para o dropdown."""
    categorias = Categoria.query.order_by(Categoria.nome, Categoria.credor).all()
    # Nomes distintos para o select dropdown
    nomes_distintos = db.session.query(Categoria.nome).distinct().all()
    nomes_distintos = [n[0] for n in nomes_distintos]
    return render_template('categorias.html', categorias=categorias, nomes_distintos=nomes_distintos)

@app.route('/categorias', methods=['POST'])
def criar_categoria():
    """Cria uma nova regra de categorização."""
    credor = request.form.get('credor', '').strip()
    categoria_existente = request.form.get('categoria_existente', '').strip()
    categoria_nova = request.form.get('categoria_nova', '').strip()

    # Determina o nome da categoria
    nome = categoria_nova if categoria_nova else categoria_existente

    if not nome:
        flash('Informe uma categoria (nova ou existente).', 'warning')
        return redirect(url_for('listar_categorias'))

    if not credor:
        flash('Informe o credor/parceiro.', 'warning')
        return redirect(url_for('listar_categorias'))

    # Verifica se já existe regra igual (mesmo credor + nome, case insensitive)
    existe = Categoria.query.filter(
        db.func.lower(Categoria.credor) == credor.lower(),
        db.func.lower(Categoria.nome) == nome.lower()
    ).first()

    if existe:
        flash(f'Já existe uma regra para "{credor}" com a categoria "{nome}".', 'warning')
        return redirect(url_for('listar_categorias'))

    nova = Categoria(credor=credor, nome=nome)
    db.session.add(nova)
    db.session.commit()
    flash(f'Categoria "{nome}" para "{credor}" criada com sucesso!', 'success')
    return redirect(url_for('listar_categorias'))

@app.route('/categorias/editar/<int:id>', methods=['POST'])
def editar_categoria(id):
    """Edita uma categoria existente."""
    categoria = Categoria.query.get_or_404(id)
    credor = request.form.get('credor', '').strip()
    nome = request.form.get('nome', '').strip()

    if not credor or not nome:
        flash('Credor e nome são obrigatórios.', 'warning')
        return redirect(url_for('listar_categorias'))

    categoria.credor = credor
    categoria.nome = nome
    db.session.commit()
    flash(f'Categoria atualizada com sucesso!', 'success')
    return redirect(url_for('listar_categorias'))

@app.route('/categorias/excluir/<int:id>', methods=['POST'])
def excluir_categoria(id):
    """Exclui uma categoria."""
    categoria = Categoria.query.get_or_404(id)
    db.session.delete(categoria)
    db.session.commit()
    flash(f'Categoria removida com sucesso!', 'success')
    return redirect(url_for('listar_categorias'))

@app.route('/categorias/recategorizar', methods=['POST'])
def recategorizar_todas():
    """Aplica as regras de categorização em todas as transações sem categoria."""
    # Busca transações sem categoria
    transacoes_sem_cat = ExtratoNubank.query.filter(ExtratoNubank.categoria_id == None).all()
    regras = Categoria.query.all()

    if not regras:
        flash('Nenhuma regra de categorização encontrada. Cadastre regras primeiro.', 'info')
        return redirect(url_for('listar_categorias'))

    categorizadas = 0
    for t in transacoes_sem_cat:
        for regra in regras:
            if regra.credor.lower() in t.descricao.lower():
                t.categoria_id = regra.id
                categorizadas += 1
                break

    if categorizadas > 0:
        db.session.commit()
        flash(f'{categorizadas} transação(ões) categorizada(s) automaticamente!', 'success')
    else:
        flash('Nenhuma transação correspondeu às regras existentes.', 'info')

    return redirect(url_for('listar_categorias'))


@app.route('/transacoes/categorizar/<int:id>', methods=['POST'])
def categorizar_transacao(id):
    """Atualiza a categoria de uma transação."""
    transacao = ExtratoNubank.query.get_or_404(id)
    categoria_id = request.form.get('categoria_id')

    if categoria_id:
        transacao.categoria_id = int(categoria_id)
    else:
        transacao.categoria_id = None

    db.session.commit()
    return redirect(url_for('listar_transacoes'))


# GET /relatorios → Relatórios de gastos por categoria
@app.route('/relatorios')
def relatorios():
    # Obtém mes e ano dos query params
    mes = request.args.get('mes', type=int)
    ano = request.args.get('ano', type=int)

    # Obtém todos os anos disponíveis no extrato (ordenados decrescente)
    anos_disponiveis = sorted(
        db.session.query(db.func.distinct(db.extract('year', ExtratoNubank.data))).all(),
        reverse=True
    )
    anos_disponiveis = [a[0] for a in anos_disponiveis]

    # Se não há dados, retorna template vazio
    if not anos_disponiveis:
        return render_template('relatorios.html',
            dados=[], labels=[], valores=[], total_geral=0,
            mes_selecionado=None, ano_selecionado=None, anos_disponiveis=[])

    # Defaults: usa o último mês que tem dados se não especificado
    if not ano:
        ano = anos_disponiveis[0]
    if not mes:
        # Encontra o último mês com dados nesse ano
        ultimas_transacoes = ExtratoNubank.query.filter(
            db.extract('year', ExtratoNubank.data) == ano
        ).order_by(ExtratoNubank.data.desc()).first()
        if ultimas_transacoes:
            mes = ultimas_transacoes.data.month
        else:
            mes = 1

    # Obtém dados de gastos por categoria
    dados = obter_gastos_por_categoria(mes, ano)

    # Calcula total geral
    total_geral = sum(d['total'] for d in dados)

    # Adiciona percentual e cor a cada item
    CORES = ['#0d6efd', '#6610f2', '#6f42c1', '#d63384', '#dc3545', '#fd7e14', '#ffc107', '#198754', '#20c997', '#0dcaf0']
    for i, item in enumerate(dados):
        item['percentual'] = round((item['total'] / total_geral * 100), 1) if total_geral > 0 else 0
        item['cor'] = CORES[i % len(CORES)]

    return render_template('relatorios.html',
        dados=dados,
        labels=[d['categoria'] for d in dados],
        valores=[d['total'] for d in dados],
        total_geral=total_geral,
        mes_selecionado=mes,
        ano_selecionado=ano,
        anos_disponiveis=anos_disponiveis
    )


@app.route('/metas')
def listar_metas():
    metas_db = Meta.query.order_by(Meta.prioridade.desc(), Meta.data_limite).all()
    metas = []
    for m in metas_db:
        metas.append({
            "meta": m,
            "progresso": calcular_progresso(m),
            "sugestao": calcular_sugestao_aporte(m),
            "cor_prioridade": obter_cor_prioridade(m.prioridade),
            "cor_status": obter_cor_status(m.status)
        })
    return render_template('metas.html', metas=metas)

@app.route('/metas/nova', methods=['GET'])
def nova_meta_form():
    return render_template('nova_meta.html')

@app.route('/metas/nova', methods=['POST'])
def nova_meta_salvar():
    try:
        nova = Meta(
            nome=request.form.get('nome'),
            valor_objetivo=float(request.form.get('valor_objetivo')),
            valor_atual=float(request.form.get('valor_atual') or 0),
            data_limite=datetime.strptime(request.form.get('data_limite'), '%Y-%m-%d').date(),
            prioridade=request.form.get('prioridade', 'media'),
            icone=request.form.get('icone', 'bi-star'),
            observacao=request.form.get('observacao')
        )
        db.session.add(nova)
        db.session.commit()
        flash('Meta criada com sucesso!', 'success')
        return redirect(url_for('listar_metas'))
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao criar meta')
        flash(f'Não foi possível criar a meta. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('nova_meta_form'))

@app.route('/metas/editar/<int:id>', methods=['GET'])
def editar_meta_form(id):
    meta = Meta.query.get_or_404(id)
    return render_template('editar_meta.html', meta=meta)

@app.route('/metas/editar/<int:id>', methods=['POST'])
def editar_meta_salvar(id):
    meta = Meta.query.get_or_404(id)
    try:
        meta.nome = request.form.get('nome')
        meta.valor_objetivo = float(request.form.get('valor_objetivo'))
        meta.valor_atual = float(request.form.get('valor_atual') or 0)
        meta.data_limite = datetime.strptime(request.form.get('data_limite'), '%Y-%m-%d').date()
        meta.prioridade = request.form.get('prioridade', 'media')
        meta.icone = request.form.get('icone', 'bi-star')
        meta.observacao = request.form.get('observacao')
        db.session.commit()
        flash('Meta atualizada com sucesso!', 'success')
        return redirect(url_for('listar_metas'))
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao atualizar meta')
        flash(f'Não foi possível atualizar a meta. Referência: {correlation_id}.', 'danger')
        return redirect(url_for('editar_meta_form', id=id))

@app.route('/metas/deletar/<int:id>', methods=['POST'])
def deletar_meta(id):
    meta = Meta.query.get_or_404(id)
    db.session.delete(meta)
    db.session.commit()
    flash('Meta removida com sucesso!', 'success')
    return redirect(url_for('listar_metas'))

@app.route('/metas/aportar/<int:id>', methods=['POST'])
def aportar_meta(id):
    meta = Meta.query.get_or_404(id)
    try:
        valor = float(request.form.get('valor'))
        data_str = request.form.get('data')
        data_aporte = datetime.strptime(data_str, '%Y-%m-%d').date() if data_str else date.today()
        
        aporte = AporteMeta(
            meta_id=meta.id,
            valor=valor,
            data=data_aporte,
            origem=request.form.get('origem'),
            observacao=request.form.get('observacao')
        )
        db.session.add(aporte)
        
        meta.valor_atual = round(meta.valor_atual + valor, 2)
        if meta.valor_atual >= meta.valor_objetivo:
            meta.status = 'concluida'
            flash(f'Parabéns! Meta "{meta.nome}" foi concluída! 🎉', 'success')
        else:
            flash(f'Aporte de R$ {valor:.2f} registrado na meta "{meta.nome}"!', 'success')
        
        db.session.commit()
    except Exception:
        correlation_id = registrar_erro_operacional('Falha ao registrar aporte na meta')
        flash(f'Não foi possível registrar o aporte. Referência: {correlation_id}.', 'danger')
    return redirect(url_for('listar_metas'))


# ============================
# ROTAS DE API (Gráficos)
# ============================

@app.route('/api/dados_saldo')
def api_dados_saldo():
    return jsonify(obter_evolucao_saldo())

@app.route('/api/dados_gastos')
def api_dados_gastos():
    return jsonify(obter_distribuicao_gastos())

@app.route('/api/dados_metas')
def api_dados_metas():
    return jsonify(obter_progresso_metas())

@app.route('/api/fechamento_mes')
def api_fechamento_mes():
    """Retorna o fechamento do mês mais recente."""
    fechamento = FechamentoMes.query.order_by(
        FechamentoMes.ano.desc(),
        FechamentoMes.mes.desc()
    ).first()

    if not fechamento:
        return jsonify(None)

    return jsonify({
        "ano": fechamento.ano,
        "mes": fechamento.mes,
        "saldo_inicial": fechamento.saldo_inicial,
        "saldo_final": fechamento.saldo_final,
        "total_entradas": fechamento.total_entradas,
        "total_saidas": fechamento.total_saidas
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() in ('true', '1')
    app.run(debug=debug, host='0.0.0.0', port=port)


