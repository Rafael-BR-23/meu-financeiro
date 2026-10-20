"""Supabase Auth REST (PKCE); identidade validada no servidor, tokens fora do cookie."""
import base64
import hashlib
import os
import re
import secrets
import time
import uuid
import click
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from models import db, Usuario, VinculoSupabase, SessaoSupabase


from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken


class TokenCipher:
    """Criptografia autenticada de tokens Supabase usando Fernet (AES-128-CBC + HMAC-SHA256)."""
    PREFIX = 'enc:v1:'

    def __init__(self, key: str):
        try:
            self._fernet = _Fernet(key.encode('ascii'))
        except (ValueError, TypeError):
            raise RuntimeError('SUPABASE_TOKEN_ENCRYPTION_KEY inválida.') from None

    def encrypt(self, token: str) -> str:
        if not token:
            raise ValueError('Token vazio.')
        return self.PREFIX + self._fernet.encrypt(token.encode()).decode('ascii')

    def decrypt(self, value: str) -> tuple:
        """
        Retorna (plaintext, legacy_plaintext).
        Se `value` não começa com PREFIX, é plaintext legado: retorna (value, True).
        # Remove legacy plaintext support after active sessions have migrated
        # and a test proves no plaintext rows remain.
        """
        if not value.startswith(self.PREFIX):
            return value, True
        try:
            return self._fernet.decrypt(value[len(self.PREFIX):].encode('ascii')).decode(), False
        except (_InvalidToken, UnicodeError):
            raise ValueError('Token cifrado inválido.') from None


class AuthError(Exception):
    def __init__(self, status=400):
        self.status = status


class SupabaseAPI:
    def __init__(self, base, key):
        self.base = base.rstrip('/') + '/auth/v1'
        self.key = key

    def call(self, method, path, data=None, token=None, params=None):
        headers = {'apikey': self.key}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            response = requests.request(method, self.base + path, headers=headers,
                                        json=data, params=params, timeout=15)
            if not response.ok:
                raise AuthError(response.status_code)
            return response.json() if response.content else {}
        except (requests.RequestException, ValueError):
            raise AuthError(503) from None

    def login(self, email, password):
        return self.call('POST', '/token', {'email': email, 'password': password},
                         params={'grant_type': 'password'})


def configurar_supabase(app):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or not key or key.startswith('sb_secret_'):
        raise RuntimeError('Configure SUPABASE_URL HTTPS e SUPABASE_PUBLISHABLE_KEY publicável, sem chave secreta.')
    if key.startswith('eyJ'):
        try:
            import json
            payload = key.split('.')[1]
            role = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('role')
            if role != 'anon':
                raise ValueError()
        except (ValueError, IndexError):
            raise RuntimeError('Use a chave publicável ou anon, não service_role.') from None
    app.config.setdefault('APP_PUBLIC_URL', os.environ.get('APP_PUBLIC_URL', 'http://localhost:5000').rstrip('/'))
    app.config.setdefault('SESSION_COOKIE_SECURE', os.environ.get('SESSION_COOKIE_SECURE', '0') == '1')
    enc_key = os.environ.get('SUPABASE_TOKEN_ENCRYPTION_KEY', '')
    if not enc_key:
        raise RuntimeError('SUPABASE_TOKEN_ENCRYPTION_KEY obrigatória para o provider Supabase.')
    cipher = TokenCipher(enc_key)
    app.extensions['supabase_token_cipher'] = cipher
    app.config.update(AUTH_PROVIDER='supabase', SUPABASE_URL=base,
                      SUPABASE_GOOGLE_ENABLED=os.environ.get('SUPABASE_GOOGLE_ENABLED', '1') == '1',
                      ACCOUNT_DATABASE_FOLDER=os.path.join(app.instance_path, 'contas'),
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30), SESSION_REFRESH_EACH_REQUEST=True)
    api = SupabaseAPI(base, key)
    app.extensions['supabase_auth'] = api
    auth = Blueprint('auth', __name__)
    from autenticacao import criar_limiter
    limiter = criar_limiter(app)

    def gravar_tokens(record, access_token, refresh_token):
        record.access_token = cipher.encrypt(access_token)
        record.refresh_token = cipher.encrypt(refresh_token)

    def ler_tokens(record):
        access, access_legacy = cipher.decrypt(record.access_token)
        refresh, refresh_legacy = cipher.decrypt(record.refresh_token)
        if access_legacy or refresh_legacy:
            gravar_tokens(record, access, refresh)
            db.session.commit()
        return access, refresh

    def engine_conta(uid):
        os.makedirs(app.config['ACCOUNT_DATABASE_FOLDER'], exist_ok=True)
        return create_engine('sqlite:///' + os.path.join(app.config['ACCOUNT_DATABASE_FOLDER'], uid + '.db'), poolclass=NullPool)

    def google_pronto():
        return app.config['SUPABASE_GOOGLE_ENABLED']

    def descartar_sessao(record):
        if record:
            db.session.delete(record)
            db.session.commit()
        session.clear()
        g.sb_current = None

    def atual():
        if hasattr(g, 'sb_current'):
            return g.sb_current
        g.sb_current = None
        record = db.session.get(SessaoSupabase, session.get('sb_session')) if session.get('sb_session') else None
        if not record:
            return None
        if record.ultimo_acesso < time.time() - 30 * 86400:
            descartar_sessao(record)
            return None
        try:
            access, refresh = ler_tokens(record)
        except ValueError:
            app.logger.warning('Supabase: token cifrado inválido; sessão local encerrada.')
            descartar_sessao(record)
            return None
        try:
            if record.expires_at <= time.time() + 60:
                tokens = api.call('POST', '/token', {'refresh_token': refresh}, params={'grant_type': 'refresh_token'})
                gravar_tokens(record, tokens['access_token'], tokens['refresh_token'])
                access = tokens['access_token']
                record.expires_at = time.time() + tokens.get('expires_in', 3600)
            identity = api.call('GET', '/user', token=access)
            if identity.get('id') != record.supabase_id or not identity.get('email_confirmed_at'):
                raise AuthError(401)
            vinculo = db.session.get(VinculoSupabase, identity['id'])
            user = db.session.get(Usuario, vinculo.usuario_id) if vinculo else None
            if not user:
                raise AuthError(401)
            record.ultimo_acesso = time.time()
            db.session.commit()
            g.sb_current = (user, vinculo, record, identity)
        except AuthError as error:
            if error.status in (400, 401, 403, 422):
                descartar_sessao(record)
            else:
                raise
        return g.sb_current

    def entrar(tokens, senha_definida=False, vincular_id=None):
        identity = api.call('GET', '/user', token=tokens['access_token'])
        if not identity.get('id') or not identity.get('email') or not identity.get('email_confirmed_at'):
            raise AuthError(401)
        email = identity['email'].strip().lower()
        vinculo = db.session.get(VinculoSupabase, identity['id'])
        user = db.session.get(Usuario, vinculo.usuario_id) if vinculo else Usuario.query.filter_by(email=email).first()
        if user and vincular_id and user.id != vincular_id:
            raise AuthError(403)
        if not user:
            if vincular_id:
                raise AuthError(403)
            metadata = identity.get('user_metadata') or {}
            # Nome é apenas apresentação; autorização usa id e e-mail verificados.
            user = Usuario(id=uuid.uuid4().hex, nome=str(metadata.get('name') or metadata.get('full_name') or email)[:100], email=email)
            engine = engine_conta(user.id)
            try:
                db.metadata.create_all(engine)
            finally:
                engine.dispose()
            db.session.add(user)
            db.session.flush()
        if not vinculo:
            existing = VinculoSupabase.query.filter_by(usuario_id=user.id).first()
            if existing:
                raise AuthError(403)
            vinculo = VinculoSupabase(id=identity['id'], usuario_id=user.id, senha_definida=senha_definida)
            db.session.add(vinculo)
        elif senha_definida:
            vinculo.senha_definida = True
        google = next((item for item in identity.get('identities', []) if item.get('provider') == 'google'), None)
        if google:
            user.google_sub = google['id']
        previous = db.session.get(SessaoSupabase, session.get('sb_session')) if session.get('sb_session') else None
        if previous:
            db.session.delete(previous)
        SessaoSupabase.query.filter(SessaoSupabase.ultimo_acesso < time.time() - 30 * 86400).delete()
        record = SessaoSupabase(id=uuid.uuid4().hex, supabase_id=identity['id'],
                               access_token=cipher.encrypt(tokens['access_token']),
                               refresh_token=cipher.encrypt(tokens['refresh_token']),
                               expires_at=time.time() + tokens.get('expires_in', 3600))
        db.session.add(record)
        db.session.commit()
        session.clear()
        session.permanent = True
        session['sb_session'] = record.id
        session['user'] = {'id': user.id, 'email': user.email, 'name': user.nome}
        g.sb_current = (user, vinculo, record, identity)
        return redirect(url_for('dashboard' if vinculo.senha_definida else 'auth.criar_senha'))

    def preparar_pkce(purpose, vincular_id=None):
        verifier = secrets.token_urlsafe(48)
        session['sb_pkce'] = {'verifier': verifier, 'purpose': purpose, 'created': time.time(), 'link_id': vincular_id}
        return {'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(),
                'code_challenge_method': 's256'}

    def callback_url():
        return app.config['APP_PUBLIC_URL'] + url_for('auth.callback')

    @app.errorhandler(AuthError)
    def erro_supabase(error):
        db.session.rollback()
        flash('Não foi possível concluir a autenticação. Tente novamente.', 'danger')
        return render_template('login.html', configurado=google_pronto(), cadastro=False), 503 if error.status >= 500 else 400

    @app.errorhandler(429)
    def limite(error):
        return render_template('login.html', configurado=google_pronto(), cadastro=False, limite_atingido=True), 429

    @app.errorhandler(IntegrityError)
    def conflito_conta(error):
        db.session.rollback()
        flash('Não foi possível vincular a conta. Tente entrar novamente.', 'danger')
        return redirect(url_for('auth.login'))

    @auth.route('/login', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def login():
        if atual():
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            email, password = request.form.get('email', '').strip().lower(), request.form.get('senha', '')
            if len(email) <= 254 and 1 <= len(password) <= 128:
                try:
                    return entrar(api.login(email, password), senha_definida=True)
                except AuthError as error:
                    if error.status >= 500:
                        raise
            flash('E-mail ou senha incorretos. Se sua conta era do login anterior, entre pelo Google ou recupere sua senha.', 'danger')
        return render_template('login.html', configurado=google_pronto(), cadastro=False)

    @auth.route('/cadastro', methods=['GET', 'POST'])
    @limiter.limit('5 per minute', methods=['POST'])
    def cadastro():
        if request.method == 'POST':
            nome, email = request.form.get('nome', '').strip(), request.form.get('email', '').strip().lower()
            password = request.form.get('senha', '')
            if not 1 <= len(nome) <= 100 or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                flash('Informe nome e e-mail válidos.', 'danger')
            elif not 8 <= len(password) <= 128 or password != request.form.get('confirmacao', ''):
                flash('Confirme uma senha de 8 a 128 caracteres.', 'danger')
            else:
                challenge = preparar_pkce('signup')
                tokens = api.call('POST', '/signup', {'email': email, 'password': password, 'data': {'name': nome}, **challenge},
                                  params={'redirect_to': callback_url()})
                if tokens.get('access_token'):
                    return entrar(tokens, senha_definida=True)
                flash('Confira seu e-mail para confirmar o cadastro e abrir o link neste navegador.', 'success')
                return redirect(url_for('auth.login'))
        return render_template('login.html', configurado=google_pronto(), cadastro=True)

    @auth.route('/auth/google', methods=['POST'])
    def google_login():
        if not google_pronto():
            return redirect(url_for('auth.login'))
        return redirect(api.base + '/authorize?' + urlencode({'provider': 'google', 'scopes': 'openid email profile',
                                                             'redirect_to': callback_url(), **preparar_pkce('google')}))

    @auth.route('/conta/vincular-google', methods=['POST'])
    def vincular_google():
        current = atual()
        if not current:
            return redirect(url_for('auth.login'))
        if not google_pronto():
            return redirect(url_for('auth.configuracoes'))
        return redirect(api.base + '/authorize?' + urlencode({'provider': 'google', 'scopes': 'openid email profile',
                        'redirect_to': callback_url(), **preparar_pkce('google', current[0].id)}))

    @auth.route('/auth/supabase/callback')
    def callback():
        pending = session.pop('sb_pkce', None)
        code = request.args.get('code')
        max_age = 1800 if pending and pending['purpose'] == 'recovery' else 3600
        if not pending or not code or time.time() - pending['created'] > max_age:
            flash('O link expirou ou foi aberto em outro navegador. Inicie o processo novamente.', 'danger')
            return redirect(url_for('auth.login'))
        if pending.get('link_id'):
            current = atual()
            if not current or current[0].id != pending['link_id']:
                raise AuthError(403)
        tokens = api.call('POST', '/token', {'auth_code': code, 'code_verifier': pending['verifier']}, params={'grant_type': 'pkce'})
        response = entrar(tokens, senha_definida=pending['purpose'] == 'signup', vincular_id=pending.get('link_id'))
        if pending['purpose'] == 'recovery':
            session['sb_recovery'] = True
            session['sb_recovery_created'] = time.time()
            return redirect(url_for('auth.redefinir_senha'))
        return response

    def trocar_senha(current, password):
        user, vinculo, record, identity = current
        access, _ = ler_tokens(record)
        api.call('PUT', '/user', {'password': password}, token=access)
        try:
            api.call('POST', '/logout', token=access, params={'scope': 'others'})
        except AuthError:
            app.logger.warning('Supabase: não foi possível encerrar sessões remotas; sessões locais revogadas.')
        SessaoSupabase.query.filter(SessaoSupabase.supabase_id == vinculo.id, SessaoSupabase.id != record.id).delete()
        vinculo.senha_definida = True
        db.session.commit()

    def senha_valida(password, confirmation):
        return 8 <= len(password) <= 128 and password == confirmation

    @auth.route('/conta/criar-senha', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def criar_senha():
        current = atual()
        if not current:
            return redirect(url_for('auth.login'))
        if current[1].senha_definida:
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            password = request.form.get('senha', '')
            if senha_valida(password, request.form.get('confirmacao', '')):
                trocar_senha(current, password)
                return redirect(url_for('dashboard'))
            flash('Confirme uma senha de 8 a 128 caracteres.', 'danger')
        return render_template('login.html', senha_google=True, user=current[0], cadastro=False)

    @auth.route('/configuracoes', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def configuracoes():
        current = atual()
        if not current:
            return redirect(url_for('auth.login'))
        if not current[1].senha_definida:
            return redirect(url_for('auth.criar_senha'))
        if request.method == 'POST':
            original = request.form.get('senha_atual', '')
            password = request.form.get('nova_senha', '')
            if not senha_valida(password, request.form.get('confirmacao', '')) or not 1 <= len(original) <= 128:
                flash('Confirme uma nova senha de 8 a 128 caracteres.', 'danger')
            elif password == original:
                flash('Escolha uma senha diferente da atual.', 'danger')
            else:
                try:
                    tokens = api.login(current[3]['email'], original)
                    check = api.call('GET', '/user', token=tokens['access_token'])
                    if check.get('id') != current[1].id:
                        raise AuthError(403)
                    gravar_tokens(current[2], tokens['access_token'], tokens['refresh_token'])
                    current[2].expires_at = time.time() + tokens.get('expires_in', 3600)
                    trocar_senha(current, password)
                    flash('Senha alterada. As outras sessões foram encerradas.', 'success')
                    return redirect(url_for('auth.configuracoes'))
                except AuthError as error:
                    if error.status >= 500:
                        raise
                    flash('Não foi possível alterar a senha. Confira sua senha atual.', 'danger')
        return render_template('configuracoes.html', user=current[0], google_disponivel=google_pronto())

    @auth.route('/esqueci-senha', methods=['GET', 'POST'])
    @limiter.limit('3 per minute; 10 per hour', methods=['POST'])
    def esqueci_senha():
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            if len(email) <= 254 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                try:
                    api.call('POST', '/recover', {'email': email, **preparar_pkce('recovery')}, params={'redirect_to': callback_url()})
                except AuthError as error:
                    if error.status >= 500:
                        raise
            return render_template('recuperar_senha.html', disponivel=True, enviado=True)
        return render_template('recuperar_senha.html', disponivel=True)

    @auth.route('/redefinir-senha', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def redefinir_senha():
        current = atual()
        if not current or not session.get('sb_recovery') or time.time() - session.get('sb_recovery_created', 0) > 1800:
            return render_template('recuperar_senha.html', invalido=True), 400
        if request.method == 'POST':
            password = request.form.get('senha', '')
            if senha_valida(password, request.form.get('confirmacao', '')):
                trocar_senha(current, password)
                return logout()
            flash('Confirme uma senha de 8 a 128 caracteres.', 'danger')
        return render_template('recuperar_senha.html', redefinir=True)

    @auth.route('/logout', methods=['POST'])
    def logout():
        record = db.session.get(SessaoSupabase, session.get('sb_session')) if session.get('sb_session') else None
        if record:
            try:
                access, _ = ler_tokens(record)
                api.call('POST', '/logout', token=access, params={'scope': 'local'})
            except AuthError:
                pass
        descartar_sessao(record)
        return redirect(url_for('auth.login'))

    @app.before_request
    def exigir_login():
        if request.endpoint in {'static', 'pwa_manifest', 'pwa_service_worker', 'pwa_icon'} or request.blueprint == 'auth':
            return
        current = atual()
        if not current:
            if request.path.startswith('/api/') or request.method != 'GET':
                return jsonify(error='Autenticação necessária', login_url=url_for('auth.login')), 401
            return redirect(url_for('auth.login'))
        user, vinculo, record, identity = current
        if not vinculo.senha_definida or session.get('sb_recovery'):
            return redirect(url_for('auth.redefinir_senha' if session.get('sb_recovery') else 'auth.criar_senha'))
        g.finance_engine = db.engines[None] if user.usa_dados_anteriores else engine_conta(user.id)
        g.uploads_folder = app.config['UPLOAD_FOLDER'] if user.usa_dados_anteriores else os.path.join(app.config['UPLOAD_FOLDER'], 'contas', user.id)
        os.makedirs(g.uploads_folder, exist_ok=True)

    @app.teardown_request
    def liberar_engine(error):
        engine = getattr(g, 'finance_engine', None)
        if engine is not None and engine is not db.engines[None]:
            db.session.remove()
            engine.dispose()

    @app.after_request
    def sem_cache(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    app.register_blueprint(auth)

    @app.cli.command('associar-dados-antigos')
    @click.argument('email')
    def associar_dados_antigos(email):
        from sqlalchemy import select, func
        user = Usuario.query.filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException('Crie a conta antes de associar os dados.')
        owner = Usuario.query.filter_by(usa_dados_anteriores=True).first()
        if owner and owner.id != user.id:
            raise click.ClickException('O histórico já pertence a outra conta.')
        engine = engine_conta(user.id)
        try:
            with engine.connect() as connection:
                if any(connection.execute(select(func.count()).select_from(table)).scalar() for table in db.metadata.sorted_tables):
                    raise click.ClickException('A conta já tem dados novos; migração manual necessária.')
        finally:
            engine.dispose()
        user.usa_dados_anteriores = True
        db.session.commit()
        click.echo('Dados antigos associados à conta informada.')
