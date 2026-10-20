import os
import re
import uuid
import click
import hashlib
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from datetime import timedelta
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
from authlib.integrations.flask_client import OAuth
from authlib.common.errors import AuthlibBaseError
from joserfc.errors import JoseError
from requests.exceptions import RequestException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from werkzeug.security import check_password_hash, generate_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from models import db, Usuario


def criar_limiter(app):
    """Retorna (e armazena) o Limiter compartilhado da aplicação.

    - Em desenvolvimento (IS_PRODUCTION=False): usa memory:// por padrão.
    - Em produção: exige RATELIMIT_STORAGE_URI diferente de memory://.
    Armazena a instância em app.extensions['financeiro_limiter'].
    """
    existing = app.extensions.get('financeiro_limiter')
    if existing is not None:
        return existing

    is_production = app.config.get('IS_PRODUCTION', False)
    uri = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')

    if is_production and (not uri or uri == 'memory://'):
        raise RuntimeError(
            'Produção exige RATELIMIT_STORAGE_URI configurado com Redis (não memory://).'
        )

    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=uri,
        default_limits=[],
    )
    app.extensions['financeiro_limiter'] = limiter
    return limiter


def versao_senha(user):
    return hashlib.sha256((user.senha_hash or '').encode()).hexdigest()


def enviar_recuperacao(app, email, link):
    mensagem = EmailMessage()
    mensagem['Subject'] = 'Redefina sua senha · Meu Financeiro'
    mensagem['From'] = app.config['SMTP_FROM']
    mensagem['To'] = email
    mensagem.set_content(f'Para criar uma nova senha, acesse:\n\n{link}\n\n'
                         'Este link vale por 30 minutos e só pode ser usado uma vez. '
                         'Se você não pediu a alteração, ignore este e-mail.')
    context = ssl.create_default_context()
    connection = (smtplib.SMTP_SSL(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=15, context=context)
                  if app.config['SMTP_SSL'] else smtplib.SMTP(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=15))
    with connection as server:
        if not app.config['SMTP_SSL']:
            server.starttls(context=context)
        server.login(app.config['SMTP_USER'], app.config['SMTP_PASSWORD'])
        server.send_message(mensagem)


def configurar_autenticacao(app):
    provider = os.environ.get('AUTH_PROVIDER') or (
        'supabase' if os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_PUBLISHABLE_KEY') else 'local')
    if provider == 'supabase':
        from autenticacao_supabase import configurar_supabase
        return configurar_supabase(app)
    app.config['AUTH_PROVIDER'] = 'local'
    app.config.setdefault('APP_PUBLIC_URL', os.environ.get('APP_PUBLIC_URL', 'http://localhost:5000').rstrip('/'))
    app.config.setdefault('SESSION_COOKIE_SECURE', os.environ.get('SESSION_COOKIE_SECURE', '0') == '1')
    app.config.update(
        GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID', ''),
        GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        GOOGLE_REDIRECT_URI=os.environ.get('GOOGLE_REDIRECT_URI', ''),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_REFRESH_EACH_REQUEST=True,
        ACCOUNT_DATABASE_FOLDER=os.path.join(app.instance_path, 'contas'),
        SMTP_HOST=os.environ.get('SMTP_HOST', ''),
        SMTP_PORT=int(os.environ.get('SMTP_PORT') or '587'),
        SMTP_USER=os.environ.get('SMTP_USER', ''),
        SMTP_PASSWORD=os.environ.get('SMTP_PASSWORD', ''),
        SMTP_FROM=os.environ.get('SMTP_FROM') or os.environ.get('SMTP_USER', ''),
        SMTP_SSL=os.environ.get('SMTP_SSL', '0') == '1',
    )
    oauth = OAuth(app)
    google = oauth.register('google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256', 'timeout': 10})
    auth = Blueprint('auth', __name__)
    # ponytail: limite por processo; usar RATELIMIT_STORAGE_URI com Redis ao escalar.
    limiter = criar_limiter(app)

    @app.errorhandler(429)
    def muitas_tentativas(error):
        return render_template('login.html', configurado=google_pronto(), cadastro=False,
                               limite_atingido=True), 429

    def google_pronto():
        return bool(app.config['GOOGLE_CLIENT_ID'] and app.config['GOOGLE_CLIENT_SECRET'])

    def usuario_atual():
        uid = session.get('user', {}).get('id')
        user = db.session.get(Usuario, uid) if uid else None
        return user if user and session.get('password_version') == versao_senha(user) else None

    def email_pronto():
        return all(app.config[key] for key in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_FROM'))

    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='recuperacao-senha')

    @auth.route('/esqueci-senha', methods=['GET', 'POST'])
    @limiter.limit('3 per minute; 10 per hour', methods=['POST'])
    def esqueci_senha():
        if request.method == 'POST':
            if not email_pronto():
                return render_template('recuperar_senha.html', disponivel=False), 503
            email = request.form.get('email', '').strip().lower()
            user = Usuario.query.filter_by(email=email).first() if len(email) <= 254 else None
            if user:
                token = serializer.dumps({'id': user.id, 'version': versao_senha(user)})
                base = app.config['APP_PUBLIC_URL']
                parsed = urlsplit(base)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                    return render_template('recuperar_senha.html', disponivel=False), 503
                link = base + url_for('auth.redefinir_senha', token=token)
                try:
                    enviar_recuperacao(app, user.email, link)
                except (smtplib.SMTPException, OSError):
                    app.logger.warning('Não foi possível enviar um e-mail de recuperação de senha.')
            # Não revela se o e-mail possui uma conta, inclusive em falhas de envio.
            return render_template('recuperar_senha.html', disponivel=True, enviado=True)
        return render_template('recuperar_senha.html', disponivel=email_pronto())

    @auth.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def redefinir_senha(token):
        try:
            data = serializer.loads(token, max_age=1800)
            user = db.session.get(Usuario, data['id'])
            if not user or data['version'] != versao_senha(user):
                raise BadSignature('Link já utilizado')
        except (BadSignature, SignatureExpired, KeyError, TypeError):
            return render_template('recuperar_senha.html', invalido=True), 400
        if request.method == 'POST':
            senha = request.form.get('senha', '')
            if not 8 <= len(senha) <= 128:
                flash('A senha deve ter entre 8 e 128 caracteres.', 'danger')
            elif senha != request.form.get('confirmacao', ''):
                flash('As senhas não coincidem.', 'danger')
            else:
                alterados = Usuario.query.filter_by(id=user.id, senha_hash=user.senha_hash).update(
                    {'senha_hash': generate_password_hash(senha)})
                if alterados != 1:
                    db.session.rollback()
                    return render_template('recuperar_senha.html', invalido=True), 400
                db.session.commit()
                session.clear()
                flash('Senha alterada. Entre com sua nova senha.', 'success')
                return redirect(url_for('auth.login'))
        return render_template('recuperar_senha.html', redefinir=True)

    def engine_conta(uid):
        folder = app.config['ACCOUNT_DATABASE_FOLDER']
        os.makedirs(folder, exist_ok=True)
        return create_engine(f'sqlite:///{os.path.join(folder, uid + ".db")}', poolclass=NullPool)

    def criar_conta(nome, email, senha=None, google_sub=None):
        user = Usuario(id=uuid.uuid4().hex, nome=nome, email=email,
                       senha_hash=generate_password_hash(senha) if senha else None, google_sub=google_sub)
        db.session.add(user)
        db.session.flush()
        engine = engine_conta(user.id)
        try:
            db.metadata.create_all(engine)
        finally:
            engine.dispose()
        db.session.commit()
        return user

    def entrar(user):
        session.clear()
        session['user'] = {'id': user.id, 'email': user.email, 'name': user.nome}
        session['password_version'] = versao_senha(user)
        session.permanent = True
        return redirect(url_for('auth.criar_senha' if not user.senha_hash else 'dashboard'))

    @auth.route('/configuracoes', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def configuracoes():
        user = usuario_atual()
        if not user:
            return redirect(url_for('auth.login'))
        if not user.senha_hash:
            return redirect(url_for('auth.criar_senha'))
        if request.method == 'POST':
            atual = request.form.get('senha_atual', '')
            nova = request.form.get('nova_senha', '')
            if len(atual) > 128 or not check_password_hash(user.senha_hash, atual):
                flash('A senha atual está incorreta.', 'danger')
            elif not 8 <= len(nova) <= 128:
                flash('A nova senha deve ter entre 8 e 128 caracteres.', 'danger')
            elif nova != request.form.get('confirmacao', ''):
                flash('As novas senhas não coincidem.', 'danger')
            elif nova == atual:
                flash('Escolha uma senha diferente da atual.', 'danger')
            else:
                original = user.senha_hash
                nova_hash = generate_password_hash(nova)
                alterados = Usuario.query.filter_by(id=user.id, senha_hash=original).update({'senha_hash': nova_hash})
                if alterados != 1:
                    db.session.rollback()
                    session.clear()
                    return redirect(url_for('auth.login'))
                db.session.commit()
                session['password_version'] = versao_senha(user)
                flash('Senha alterada. As outras sessões da sua conta foram encerradas.', 'success')
                return redirect(url_for('auth.configuracoes'))
        return render_template('configuracoes.html', user=user, google_disponivel=google_pronto())

    @auth.route('/conta/criar-senha', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def criar_senha():
        user = usuario_atual()
        if not user or not user.google_sub:
            return redirect(url_for('auth.login'))
        if user.senha_hash:
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            senha = request.form.get('senha', '')
            if not 8 <= len(senha) <= 128:
                flash('A senha deve ter entre 8 e 128 caracteres.', 'danger')
            elif senha != request.form.get('confirmacao', ''):
                flash('As senhas não coincidem.', 'danger')
            else:
                user.senha_hash = generate_password_hash(senha)
                db.session.commit()
                return entrar(user)
        return render_template('login.html', senha_google=True, user=user, cadastro=False)

    @auth.route('/login', methods=['GET', 'POST'])
    @limiter.limit('10 per minute', methods=['POST'])
    def login():
        if usuario_atual():
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            senha = request.form.get('senha', '')
            if len(senha) > 128:
                flash('E-mail ou senha incorretos.', 'danger')
                return render_template('login.html', configurado=google_pronto(), cadastro=False), 400
            user = Usuario.query.filter_by(email=email).first()
            valid = check_password_hash(user.senha_hash if user and user.senha_hash else app.config['AUTH_DUMMY_HASH'], senha)
            if user and user.senha_hash and valid:
                return entrar(user)
            flash('E-mail ou senha incorretos.', 'danger')
        return render_template('login.html', configurado=google_pronto(), cadastro=False)

    @auth.route('/cadastro', methods=['GET', 'POST'])
    @limiter.limit('5 per minute', methods=['POST'])
    def cadastro():
        if usuario_atual():
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip().lower()
            senha = request.form.get('senha', '')
            confirmacao = request.form.get('confirmacao', '')
            if not 1 <= len(nome) <= 100 or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                flash('Informe um nome e e-mail válidos.', 'danger')
            elif not 8 <= len(senha) <= 128:
                flash('A senha deve ter entre 8 e 128 caracteres.', 'danger')
            elif senha != confirmacao:
                flash('As senhas não coincidem.', 'danger')
            else:
                try:
                    return entrar(criar_conta(nome, email, senha))
                except IntegrityError:
                    db.session.rollback()
                    flash('Este e-mail já possui uma conta. Entre para continuar.', 'danger')
        return render_template('login.html', configurado=google_pronto(), cadastro=True)

    @auth.route('/auth/google', methods=['POST'])
    def google_login():
        if not google_pronto():
            flash('O acesso com Google ainda precisa ser configurado.', 'warning')
            return redirect(url_for('auth.login'))
        session.clear()
        callback = app.config['GOOGLE_REDIRECT_URI'] or url_for('auth.callback', _external=True)
        try:
            return google.authorize_redirect(callback)
        except (AuthlibBaseError, RequestException, ValueError):
            flash('Não foi possível conectar ao Google. Tente novamente.', 'danger')
            return redirect(url_for('auth.login'))

    @auth.route('/auth/google/callback')
    def callback():
        if not google_pronto():
            return redirect(url_for('auth.login'))
        try:
            claims = google.authorize_access_token().get('userinfo', {})
            email = str(claims.get('email', '')).lower()
            if not claims.get('sub') or claims.get('email_verified') is not True or not email:
                raise ValueError('Identidade inválida')
            link_id = session.pop('link_google_id', None)
            user = Usuario.query.filter_by(google_sub=claims['sub']).first()
            if link_id:
                current = usuario_atual()
                if not current or current.id != link_id or current.email != email or (user and user.id != link_id):
                    raise ValueError('Vínculo inválido')
                current.google_sub = claims['sub']
                db.session.commit()
                return entrar(current)
            if user is None:
                user = Usuario.query.filter_by(email=email).first()
                if user:
                    if user.google_sub and user.google_sub != claims['sub']:
                        raise ValueError('Conta vinculada a outra identidade Google')
                    user.google_sub = claims['sub']
                    db.session.commit()
                else:
                    user = criar_conta(str(claims.get('name') or email)[:100], email, google_sub=claims['sub'])
            return entrar(user)
        except (AuthlibBaseError, JoseError, RequestException, ValueError, KeyError, IntegrityError):
            db.session.rollback()
            session.clear()
            flash('Não foi possível concluir o login. Tente novamente.', 'danger')
            return redirect(url_for('auth.login'))

    @auth.route('/conta/vincular-google', methods=['POST'])
    def vincular_google():
        user = usuario_atual()
        if not user or not google_pronto():
            return redirect(url_for('auth.login'))
        session['link_google_id'] = user.id
        try:
            callback = app.config['GOOGLE_REDIRECT_URI'] or url_for('auth.callback', _external=True)
            return google.authorize_redirect(callback)
        except (AuthlibBaseError, RequestException, ValueError):
            session.pop('link_google_id', None)
            flash('Não foi possível conectar ao Google.', 'danger')
            return redirect(url_for('dashboard'))

    @auth.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        return redirect(url_for('auth.login'))

    app.config['AUTH_DUMMY_HASH'] = generate_password_hash(uuid.uuid4().hex)
    app.register_blueprint(auth)

    @app.cli.command('associar-dados-antigos')
    @click.argument('email')
    def associar_dados_antigos(email):
        """Associa o banco anterior a uma conta já cadastrada, pelo console local."""
        user = Usuario.query.filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException('Cadastre a conta antes de associar os dados.')
        owner = Usuario.query.filter_by(usa_dados_anteriores=True).first()
        if owner and owner.id != user.id:
            raise click.ClickException('Os dados antigos já pertencem a outra conta.')
        engine = engine_conta(user.id)
        try:
            with engine.connect() as connection:
                from sqlalchemy import select, func
                if any(connection.execute(select(func.count()).select_from(table)).scalar()
                       for table in db.metadata.sorted_tables):
                    raise click.ClickException('Esta conta já tem dados novos. Faça a migração manual para preservá-los.')
        finally:
            engine.dispose()
        user.usa_dados_anteriores = True
        db.session.commit()
        click.echo('Dados antigos associados à conta informada.')

    @app.before_request
    def exigir_login():
        if request.endpoint in {'static', 'pwa_manifest', 'pwa_service_worker', 'pwa_icon', '_test_proxy_info'} or request.blueprint == 'auth':
            return
        user = usuario_atual()
        if user:
            if not user.senha_hash and user.google_sub:
                if request.path.startswith('/api/') or request.method != 'GET':
                    return jsonify(error='Crie sua senha para concluir o cadastro', cadastro_url=url_for('auth.criar_senha')), 403
                return redirect(url_for('auth.criar_senha'))
            g.finance_engine = db.engines[None] if user.usa_dados_anteriores else engine_conta(user.id)
            g.uploads_folder = app.config['UPLOAD_FOLDER'] if user.usa_dados_anteriores else os.path.join(app.config['UPLOAD_FOLDER'], 'contas', user.id)
            os.makedirs(g.uploads_folder, exist_ok=True)
            return
        if request.path.startswith('/api/') or request.method != 'GET':
            return jsonify(error='Autenticação necessária', login_url=url_for('auth.login')), 401
        return redirect(url_for('auth.login'))

    @app.teardown_request
    def liberar_engine(error):
        engine = getattr(g, 'finance_engine', None)
        if engine is not None and engine is not db.engines[None]:
            db.session.remove()
            engine.dispose()

    @app.after_request
    def impedir_cache_privado(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response
