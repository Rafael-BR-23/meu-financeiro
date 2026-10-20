import tempfile
import unittest
import io
import os
import time
import smtplib
from itsdangerous import URLSafeTimedSerializer
from datetime import date
from unittest.mock import patch
from authlib.integrations.base_client.errors import MismatchingStateError
import tests  # Configure disposable databases before importing the application.
from app import app, db
from models import Usuario, Pendencia


class AutenticacaoTest(unittest.TestCase):
    def setUp(self):
        self.config = dict(app.config)
        self.files = tempfile.TemporaryDirectory()
        self.limiter = next(iter(app.extensions['limiter']))
        self.limiter.enabled = False
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, GOOGLE_CLIENT_ID='client',
                          GOOGLE_CLIENT_SECRET='secret', ACCOUNT_DATABASE_FOLDER=self.files.name,
                          UPLOAD_FOLDER=self.files.name)
        with app.app_context():
            assert all(engine.url.database == ':memory:' for engine in db.engines.values()), 'Tests require disposable databases'
            db.create_all()
        self.client = app.test_client()
        self.google = app.extensions['authlib.integrations.flask_client'].create_client('google')

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        app.config.clear()
        app.config.update(self.config)
        self.files.cleanup()
        self.limiter.enabled = True

    def cadastrar(self, client=None, email='dono@example.com'):
        return (client or self.client).post('/cadastro', data={
            'nome': 'Pessoa de teste', 'email': email, 'senha': 'senha-segura-123', 'confirmacao': 'senha-segura-123'})

    def test_auth_pages_keep_standalone_accessible_layout(self):
        for path in ('/login', '/cadastro', '/esqueci-senha'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertEqual(html.count('<main>'), 1)
            self.assertNotIn('Navegação principal', html)
            self.assertIn('--font-body:', html)

    def test_protecao_e_google_ausente(self):
        self.assertEqual(self.client.get('/').location, '/login')
        self.assertEqual(self.client.get('/api/dados_saldo').status_code, 401)
        self.assertEqual(self.client.post('/compartilhar-comprovante').status_code, 401)
        self.assertEqual(self.client.get('/login').headers['Cache-Control'], 'no-store')
        app.config['GOOGLE_CLIENT_ID'] = ''
        self.assertIn(b'disabled', self.client.get('/login').data)
        self.assertEqual(self.client.post('/auth/google').location, '/login')
        self.assertEqual(self.cadastrar().location, '/')

    def test_cadastro_senha_persistencia_saida_e_csrf(self):
        response = self.cadastrar()
        self.assertEqual(response.location, '/')
        cookie = response.headers['Set-Cookie']
        for flag in ['Expires=', 'HttpOnly', 'SameSite=Lax']:
            self.assertIn(flag, cookie)
        with app.app_context():
            user = Usuario.query.first()
            self.assertNotEqual(user.senha_hash, 'senha-segura-123')
        self.assertEqual(self.client.get('/').status_code, 200)
        app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/logout').status_code, 400)
        app.config['WTF_CSRF_ENABLED'] = False
        self.client.post('/logout')
        self.assertEqual(self.client.get('/').location, '/login')
        self.assertEqual(self.client.post('/login', data={'email': 'dono@example.com', 'senha': 'errada'}).status_code, 200)
        self.assertEqual(self.client.post('/login', data={'email': 'dono@example.com', 'senha': 'senha-segura-123'}).location, '/')

    def test_validacao_e_duplicidade(self):
        for email, senha, confirmacao in [('invalido', '12345678', '12345678'),
                                         ('a@example.com', 'curta', 'curta'),
                                         ('a@example.com', '12345678', '87654321')]:
            self.client.post('/cadastro', data={'nome': 'Nome', 'email': email, 'senha': senha, 'confirmacao': confirmacao})
            with app.app_context():
                self.assertEqual(Usuario.query.count(), 0)
        self.cadastrar()
        self.client.post('/logout')
        self.assertEqual(self.cadastrar().status_code, 200)
        with app.app_context():
            self.assertEqual(Usuario.query.count(), 1)

    def test_limite_tentativas(self):
        self.limiter.enabled = True
        self.limiter.reset()
        for attempt in range(10):
            self.assertEqual(self.client.post('/login', data={'email': 'x@example.com', 'senha': 'errada'}).status_code, 200)
        self.assertEqual(self.client.post('/login').status_code, 429)
        self.limiter.reset()

    def test_google_cria_conta_e_valida_identidade(self):
        claims = {'sub': '123', 'email': 'google@example.com', 'email_verified': True}
        with patch.object(self.google, 'authorize_access_token', return_value={'userinfo': claims}):
            self.assertEqual(self.client.get('/auth/google/callback').location, '/conta/criar-senha')
            self.assertEqual(self.client.get('/').location, '/conta/criar-senha')
            self.assertEqual(self.client.get('/api/dados_saldo').status_code, 403)
            self.assertIn(b'google@example.com', self.client.get('/conta/criar-senha').data)
            self.assertEqual(self.client.post('/conta/criar-senha', data={'senha': 'curta', 'confirmacao': 'curta'}).status_code, 200)
            self.assertEqual(self.client.post('/conta/criar-senha', data={'senha': 'senha-segura-123', 'confirmacao': 'outra'}).status_code, 200)
            app.config['WTF_CSRF_ENABLED'] = True
            self.assertEqual(self.client.post('/conta/criar-senha', data={'senha': 'senha-segura-123', 'confirmacao': 'senha-segura-123'}).status_code, 400)
            app.config['WTF_CSRF_ENABLED'] = False
            self.assertEqual(self.client.post('/conta/criar-senha', data={'senha': 'senha-segura-123', 'confirmacao': 'senha-segura-123'}).location, '/')
            self.client.post('/logout')
            self.assertEqual(self.client.get('/auth/google/callback').location, '/')
        with app.app_context():
            self.assertEqual(Usuario.query.count(), 1)
        self.client.post('/logout')
        self.assertEqual(self.client.post('/login', data={'email': 'google@example.com', 'senha': 'senha-segura-123'}).location, '/')
        self.client.post('/logout')
        claims['email_verified'] = False
        with patch.object(self.google, 'authorize_access_token', return_value={'userinfo': claims}):
            self.assertEqual(self.client.get('/auth/google/callback').location, '/login')
        with patch.object(self.google, 'authorize_access_token', side_effect=MismatchingStateError()):
            self.assertEqual(self.client.get('/auth/google/callback').location, '/login')

    def test_google_vincula_email_verificado_e_preserva_conta_existente(self):
        self.cadastrar()
        with app.app_context():
            user = Usuario.query.first()
            original = (user.id, user.senha_hash, user.usa_dados_anteriores)
        self.client.post('/logout')
        claims = {'sub': '123', 'email': 'dono@example.com', 'email_verified': True}
        with patch.object(self.google, 'authorize_access_token', return_value={'userinfo': claims}):
            self.assertEqual(self.client.get('/auth/google/callback').location, '/')
        with app.app_context():
            user = Usuario.query.first()
            self.assertEqual(user.google_sub, '123')
            self.assertEqual((user.id, user.senha_hash, user.usa_dados_anteriores), original)
            self.assertEqual(Usuario.query.count(), 1)
        self.client.post('/logout')
        claims['sub'] = 'outra-identidade'
        with patch.object(self.google, 'authorize_access_token', return_value={'userinfo': claims}):
            self.assertEqual(self.client.get('/auth/google/callback').location, '/login')
        self.assertEqual(self.client.get('/conta/criar-senha').location, '/login')

    def test_isolamento_paginas_ids_graficos_uploads_e_dados_antigos(self):
        with app.app_context():
            db.session.add(Pendencia(credor='Dado antigo privado', valor=321, data_vencimento=date.today()))
            db.session.commit()
        self.cadastrar()
        self.assertNotIn(b'Dado antigo privado', self.client.get('/').data)
        from PIL import Image as _Image
        _buf = io.BytesIO()
        _Image.new('RGB', (10, 10), 'blue').save(_buf, format='PNG')
        _png_bytes = _buf.getvalue()
        self.client.post('/pendencias/nova', data={'tipo_lancamento': 'pago', 'credor': 'Privado A',
            'valor': '150', 'data_pagamento': date.today().isoformat(),
            'comprovante': (io.BytesIO(_png_bytes), 'comprovante.png')})
        self.assertIn(b'Privado A', self.client.get('/historico').data)
        with self.client.session_transaction() as session:
            uid = session['user']['id']
        self.assertEqual(len(os.listdir(os.path.join(self.files.name, 'contas', uid))), 1)
        response = self.client.get('/historico/1')
        # A entrega segura retorna o PNG com MIME validado e Content-Disposition: attachment
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        response.close()
        other = app.test_client()
        self.cadastrar(other, 'outra@example.com')
        self.assertNotIn(b'Privado A', other.get('/historico').data)
        self.assertEqual(other.get('/pendencias/editar/1').status_code, 404)
        self.assertEqual(other.get('/historico/1').status_code, 404)
        self.assertEqual(other.get('/api/dados_saldo').status_code, 200)
        for path in ['/pendencias', '/metas', '/categorias', '/transacoes', '/relatorios', '/importar']:
            self.assertEqual(other.get(path).status_code, 200, path)

    def test_associar_dados_antigos_exige_conta_vazia(self):
        self.cadastrar()
        runner = app.test_cli_runner()
        result = runner.invoke(args=['associar-dados-antigos', 'dono@example.com'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.client.post('/logout')
        self.cadastrar(email='outra@example.com')
        result = runner.invoke(args=['associar-dados-antigos', 'outra@example.com'])
        self.assertNotEqual(result.exit_code, 0)

    def test_recuperacao_envio_senha_nova_link_unico_e_sessoes_encerradas(self):
        self.cadastrar()
        with self.client.session_transaction() as old_session:
            saved = dict(old_session)
        self.client.post('/logout')
        app.config.update(SMTP_HOST='smtp.example.com', SMTP_USER='sender@example.com',
                          SMTP_PASSWORD='apenas-teste', SMTP_FROM='sender@example.com',
                          APP_PUBLIC_URL='https://financeiro.example.com')
        with patch('autenticacao.enviar_recuperacao') as enviar:
            conhecida = self.client.post('/esqueci-senha', data={'email': 'dono@example.com'})
            link = enviar.call_args.args[2]
            self.assertTrue(link.startswith('https://financeiro.example.com/redefinir-senha/'))
            desconhecida = self.client.post('/esqueci-senha', data={'email': 'inexistente@example.com'})
            self.assertEqual(conhecida.data, desconhecida.data)
            self.assertEqual(enviar.call_count, 1)
        path = '/redefinir-senha/' + link.rsplit('/', 1)[1]
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(path, data={'senha': 'curta', 'confirmacao': 'curta'}).status_code, 200)
        app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post(path).status_code, 400)
        app.config['WTF_CSRF_ENABLED'] = False
        response = self.client.post(path, data={'senha': 'senha-nova-123', 'confirmacao': 'senha-nova-123'})
        self.assertEqual(response.location, '/login')
        self.assertEqual(self.client.get(path).status_code, 400)
        old = app.test_client()
        with old.session_transaction() as session:
            session.update(saved)
        self.assertEqual(old.get('/').location, '/login')
        self.assertEqual(self.client.post('/login', data={'email': 'dono@example.com', 'senha': 'senha-segura-123'}).status_code, 200)
        self.assertEqual(self.client.post('/login', data={'email': 'dono@example.com', 'senha': 'senha-nova-123'}).location, '/')

    def test_recuperacao_links_expirados_invalidos_e_sem_configuracao(self):
        self.cadastrar()
        with app.app_context():
            from autenticacao import versao_senha
            user = Usuario.query.first()
            data = {'id': user.id, 'version': versao_senha(user)}
        serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='recuperacao-senha')
        with patch('itsdangerous.timed.time.time', return_value=time.time() - 1801):
            expired = serializer.dumps(data)
        self.assertEqual(self.client.get('/redefinir-senha/' + expired).status_code, 400)
        self.assertEqual(self.client.get('/redefinir-senha/invalido').status_code, 400)
        self.client.post('/logout')
        app.config['SMTP_PASSWORD'] = ''
        with patch('autenticacao.enviar_recuperacao') as enviar:
            self.assertEqual(self.client.post('/esqueci-senha', data={'email': 'dono@example.com'}).status_code, 503)
            enviar.assert_not_called()
        self.assertIn(b'Esqueci minha senha', self.client.get('/login').data)

    def test_recuperacao_falha_smtp_nao_expoe_conta(self):
        self.cadastrar()
        self.client.post('/logout')
        app.config.update(SMTP_HOST='smtp.example.com', SMTP_USER='sender@example.com',
                          SMTP_PASSWORD='apenas-teste', SMTP_FROM='sender@example.com')
        with patch('autenticacao.enviar_recuperacao', side_effect=smtplib.SMTPException()):
            existente = self.client.post('/esqueci-senha', data={'email': 'dono@example.com'})
            ausente = self.client.post('/esqueci-senha', data={'email': 'ausente@example.com'})
            self.assertEqual(existente.status_code, 200)
            self.assertEqual(existente.data, ausente.data)

    def test_configuracoes_exigem_login_e_movem_google_da_barra(self):
        self.assertEqual(self.client.get('/configuracoes').location, '/login')
        self.cadastrar()
        page = self.client.get('/configuracoes')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'dono@example.com', page.data)
        self.assertIn(b'Vincular Google', page.data)
        self.assertNotIn(b'Vincular Google', self.client.get('/').data)
        with app.app_context():
            user = Usuario.query.first()
            user.google_sub = '123'
            db.session.commit()
        self.assertIn('já está vinculada'.encode(), self.client.get('/configuracoes').data)

    def test_trocar_senha_valida_atual_preserva_conta_e_revoga_outras_sessoes(self):
        self.cadastrar()
        with self.client.session_transaction() as session:
            saved = dict(session)
        with app.app_context():
            uid = Usuario.query.first().id
        for atual, nova, confirmacao in [('errada', 'senha-nova-123', 'senha-nova-123'),
                                         ('senha-segura-123', 'curta', 'curta'),
                                         ('senha-segura-123', 'senha-nova-123', 'outra')]:
            self.assertEqual(self.client.post('/configuracoes', data={
                'senha_atual': atual, 'nova_senha': nova, 'confirmacao': confirmacao}).status_code, 200)
        app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/configuracoes').status_code, 400)
        app.config['WTF_CSRF_ENABLED'] = False
        self.assertEqual(self.client.post('/configuracoes', data={'senha_atual': 'senha-segura-123',
            'nova_senha': 'senha-nova-123', 'confirmacao': 'senha-nova-123'}).location, '/configuracoes')
        self.assertEqual(self.client.get('/configuracoes').status_code, 200)
        other = app.test_client()
        with other.session_transaction() as session:
            session.update(saved)
        self.assertEqual(other.get('/').location, '/login')
        with app.app_context():
            self.assertEqual(Usuario.query.first().id, uid)
        self.client.post('/logout')
        self.assertEqual(self.client.post('/login', data={'email': 'dono@example.com', 'senha': 'senha-nova-123'}).location, '/')
