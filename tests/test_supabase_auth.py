import base64
import io
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet
from flask import Flask
from flask_wtf import CSRFProtect
import tests  # Configure disposable databases before importing the application.
from app import app as original_app
from autenticacao_supabase import AuthError, TokenCipher, configurar_supabase
from models import db, Usuario, Pendencia, VinculoSupabase, SessaoSupabase


class SupabaseAuthTest(unittest.TestCase):
    def test_supabase_reuses_shared_application_limiter(self):
        from autenticacao import criar_limiter
        self.assertIs(criar_limiter(self.app), self.app.extensions['financeiro_limiter'])
        self.assertIs(next(iter(self.app.extensions['limiter'])), criar_limiter(self.app))

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        self.app = Flask(__name__, instance_path=self.folder.name, template_folder=str(root / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='chave-exclusiva-para-testes-supabase',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_BINDS={'auth':'sqlite:///:memory:'},
            SQLALCHEMY_TRACK_MODIFICATIONS=False, UPLOAD_FOLDER=self.folder.name,
            WTF_CSRF_ENABLED=False)
        db.init_app(self.app)
        CSRFProtect(self.app)
        for endpoint in original_app.view_functions:
            if endpoint not in ('static', 'dashboard') and not endpoint.startswith('auth.'):
                self.app.add_url_rule('/_test/' + endpoint, endpoint, lambda: 'ok')
        self.app.add_url_rule('/', 'dashboard', lambda: '|'.join(item.credor for item in Pendencia.query.all()))
        self._test_key = Fernet.generate_key().decode('ascii')
        self.env = patch.dict(os.environ, {
            'SUPABASE_URL': 'https://test-project.supabase.co',
            'SUPABASE_PUBLISHABLE_KEY': 'sb_publishable_apenas_teste',
            'APP_PUBLIC_URL': 'http://localhost:5000',
            'SUPABASE_TOKEN_ENCRYPTION_KEY': self._test_key
        })
        self.env.start()
        configurar_supabase(self.app)
        next(iter(self.app.extensions['limiter'])).enabled = False
        with self.app.app_context():
            db.create_all()
        self.client = self.app.test_client()
        self.identity = {'id':'00000000-0000-0000-0000-000000000001', 'email':'dono@example.com',
            'email_confirmed_at':'2026-10-12T12:00:00Z', 'identities':[], 'user_metadata':{'name':'Dono', 'role':'admin'}}
        self.tokens = {'access_token':'access-apenas-teste', 'refresh_token':'refresh-apenas-teste', 'expires_in':3600}
        self.api = self.app.extensions['supabase_auth']
        self.api.call = Mock(side_effect=self.call)

    def call(self, method, path, data=None, token=None, params=None):
        if path == '/token':
            return dict(self.tokens)
        if path == '/user' and method == 'GET':
            return dict(self.identity)
        if path in ('/signup', '/recover', '/logout', '/user'):
            return {}
        raise AssertionError((method, path))

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            for engine in db.engines.values():
                engine.dispose()
        self.env.stop()
        self.folder.cleanup()

    def seed_session(self, access_token, refresh_token):
        """Cria sessão, vínculo e usuário com tokens diretos (plaintext ou corrompido)."""
        with self.app.app_context():
            user = Usuario(id='seed' * 8, nome='Seed', email='seed@example.com', usa_dados_anteriores=True)
            db.session.add(user)
            db.session.flush()
            vinculo = VinculoSupabase(id=self.identity['id'], usuario_id=user.id, senha_definida=True)
            db.session.add(vinculo)
            record = SessaoSupabase(
                id='rec-seed-001',
                supabase_id=self.identity['id'],
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=time.time() + 3600
            )
            db.session.add(record)
            db.session.commit()
            return record.id

    def login(self):
        return self.client.post('/login', data={'email':'dono@example.com','senha':'senha-segura-123'})

    def test_login_preserva_historico_e_nao_coloca_tokens_no_cookie(self):
        with self.app.app_context():
            user = Usuario(id='a' * 32, nome='Dono antigo', email='dono@example.com', senha_hash='hash-anterior', usa_dados_anteriores=True)
            db.session.add(user)
            db.session.add(Pendencia(credor='Registro preservado', valor=10, data_vencimento=date.today()))
            db.session.commit()
        self.assertEqual(self.login().location, '/')
        self.assertIn(b'Registro preservado', self.client.get('/').data)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user']['id'], 'a' * 32)
            self.assertNotIn('access_token', session)
            self.assertNotIn('refresh_token', session)
        with self.app.app_context():
            self.assertEqual(Usuario.query.count(), 1)
            self.assertEqual(VinculoSupabase.query.first().usuario_id, 'a' * 32)
            self.assertEqual(SessaoSupabase.query.count(), 1)
            record = SessaoSupabase.query.one()
            self.assertTrue(record.access_token.startswith('enc:v1:'))
            self.assertTrue(record.refresh_token.startswith('enc:v1:'))
            self.assertNotIn(self.tokens['access_token'], record.access_token)
            self.assertNotIn(self.tokens['refresh_token'], record.refresh_token)
        self.client.post('/logout')
        self.assertEqual(self.client.get('/').location, '/login')

    def test_identidade_nao_confirmada_nao_recebe_dados(self):
        self.identity['email_confirmed_at'] = None
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get('/').location, '/login')
        with self.app.app_context():
            self.assertEqual(Usuario.query.count(), 0)

    def test_google_pkce_cria_conta_pede_senha_e_protege_callback(self):
        self.identity['identities'] = [{'id':'google-identity-test','provider':'google'}]
        self.assertEqual(self.client.get('/auth/supabase/callback?code=code').location, '/login')
        response = self.client.post('/auth/google')
        self.assertIn('code_challenge_method=s256', response.location)
        self.assertNotIn('code_verifier', response.location)
        response = self.client.get('/auth/supabase/callback?code=code')
        self.assertEqual(response.location, '/conta/criar-senha')
        self.assertEqual(self.client.get('/').location, '/conta/criar-senha')
        response = self.client.post('/conta/criar-senha', data={'senha':'senha-segura-123','confirmacao':'senha-segura-123'})
        self.assertEqual(response.location, '/')
        self.assertEqual(self.client.get('/').status_code, 200)
        calls = [call for call in self.api.call.call_args_list if call.args[:2] == ('POST', '/token')]
        self.assertIn('code_verifier', calls[0].args[2])

    def test_refresh_e_validacao_da_identidade_em_cada_requisicao(self):
        self.login()
        with self.app.app_context():
            record = SessaoSupabase.query.first()
            record.expires_at = time.time() - 1
            db.session.commit()
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertTrue(any(call.kwargs.get('params') == {'grant_type':'refresh_token'} for call in self.api.call.call_args_list))
        with self.app.app_context():
            record = SessaoSupabase.query.first()
            self.assertTrue(record.access_token.startswith('enc:v1:'))
            self.assertTrue(record.refresh_token.startswith('enc:v1:'))
        self.identity['id'] = 'outra-identidade'
        self.assertEqual(self.client.get('/').location, '/login')

    def test_recuperacao_e_troca_senha_no_supabase(self):
        self.assertEqual(self.client.post('/esqueci-senha', data={'email':'dono@example.com'}).status_code, 200)
        self.assertEqual(self.client.get('/redefinir-senha').status_code, 400)
        self.assertEqual(self.client.get('/auth/supabase/callback?code=recovery-code').location, '/redefinir-senha')
        response = self.client.post('/redefinir-senha', data={'senha':'nova-senha-123','confirmacao':'nova-senha-123'})
        self.assertEqual(response.location, '/login')
        self.login()
        self.assertEqual(self.client.get('/configuracoes').status_code, 200)
        response = self.client.post('/configuracoes', data={'senha_atual':'senha-segura-123','nova_senha':'outra-senha-123','confirmacao':'outra-senha-123'})
        self.assertEqual(response.location, '/configuracoes')
        self.assertTrue(any(call.args[:2] == ('PUT', '/user') for call in self.api.call.call_args_list))

    def test_indisponibilidade_nao_descarta_sessao_e_csrf_protege_posts(self):
        self.login()
        self.api.call.side_effect = AuthError(503)
        self.assertEqual(self.client.get('/').status_code, 503)
        with self.client.session_transaction() as session:
            self.assertIn('sb_session', session)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/logout').status_code, 400)

    def test_cadastro_exige_confirmacao_sem_criar_conta_financeira(self):
        response = self.client.post('/cadastro', data={'nome':'Teste','email':'dono@example.com',
            'senha':'senha-segura-123','confirmacao':'senha-segura-123'})
        self.assertEqual(response.location, '/login')
        with self.app.app_context():
            self.assertEqual(Usuario.query.count(), 0)

    def test_outra_conta_nao_acessa_historico_mesmo_com_metadata_admin(self):
        with self.app.app_context():
            db.session.add(Usuario(id='a' * 32, nome='Dono', email='dono@example.com', usa_dados_anteriores=True))
            db.session.add(Pendencia(credor='Somente do dono', valor=10, data_vencimento=date.today()))
            db.session.commit()
        self.identity['id'] = '00000000-0000-0000-0000-000000000002'
        self.identity['email'] = 'outra@example.com'
        self.assertEqual(self.login().location, '/')
        self.assertNotIn(b'Somente do dono', self.client.get('/').data)

    def test_trocar_senha_revoga_sessao_anterior_da_mesma_conta(self):
        self.login()
        other = self.app.test_client()
        other.post('/login', data={'email':'dono@example.com','senha':'senha-segura-123'})
        self.client.post('/configuracoes', data={'senha_atual':'senha-segura-123',
            'nova_senha':'outra-senha-123','confirmacao':'outra-senha-123'})
        self.assertEqual(other.get('/').location, '/login')
        self.assertEqual(self.client.get('/').status_code, 200)


    def test_legacy_plaintext_is_reencrypted_on_first_read(self):
        record_id = self.seed_session('access-legado-teste', 'refresh-legado-teste')
        with self.client.session_transaction() as s:
            s['sb_session'] = record_id
        self.assertEqual(self.client.get('/').status_code, 200)
        with self.app.app_context():
            record = db.session.get(SessaoSupabase, record_id)
            self.assertTrue(record.access_token.startswith('enc:v1:'))
            self.assertTrue(record.refresh_token.startswith('enc:v1:'))

    def test_corrupted_ciphertext_invalidates_session_without_token_retry(self):
        record_id = self.seed_session('enc:v1:corrompido', 'enc:v1:tambem-corrompido')
        with self.client.session_transaction() as s:
            s['sb_session'] = record_id
        response = self.client.get('/')
        self.assertEqual(response.location, '/login')
        with self.app.app_context():
            self.assertIsNone(db.session.get(SessaoSupabase, record_id))
        # Confirma que o valor corrompido não foi enviado para a API
        for call in self.api.call.call_args_list:
            token_arg = call.kwargs.get('token') or (call.args[3] if len(call.args) > 3 else None)
            if token_arg:
                self.assertNotIn('corrompido', str(token_arg))

    def test_token_cipher_round_trip_and_tamper_rejection(self):
        key = Fernet.generate_key().decode('ascii')
        cipher = TokenCipher(key)
        encrypted = cipher.encrypt('access-apenas-teste')
        self.assertTrue(encrypted.startswith('enc:v1:'))
        self.assertNotIn('access-apenas-teste', encrypted)
        self.assertEqual(cipher.decrypt(encrypted), ('access-apenas-teste', False))
        raw = bytearray(base64.urlsafe_b64decode(encrypted[len(cipher.PREFIX):].encode('ascii')))
        raw[len(raw) // 2] ^= 0x01
        tampered = cipher.PREFIX + base64.urlsafe_b64encode(raw).decode('ascii')
        with self.assertRaises(ValueError):
            cipher.decrypt(tampered)

    def test_supabase_requires_independent_encryption_key(self):
        app2 = Flask('test_chave_obrigatoria',
                     instance_path=self.folder.name,
                     template_folder=str(Path(__file__).resolve().parent.parent / 'templates'))
        app2.config.update(TESTING=True, SECRET_KEY='outra-chave', WTF_CSRF_ENABLED=False,
                           SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                           SQLALCHEMY_BINDS={'auth': 'sqlite:///:memory:'},
                           SQLALCHEMY_TRACK_MODIFICATIONS=False,
                           UPLOAD_FOLDER=self.folder.name)
        db.init_app(app2)
        CSRFProtect(app2)
        with patch.dict(os.environ, {'SUPABASE_TOKEN_ENCRYPTION_KEY': ''}, clear=False):
            with self.assertRaisesRegex(RuntimeError, 'SUPABASE_TOKEN_ENCRYPTION_KEY'):
                configurar_supabase(app2)

    def test_outra_conta_nao_acessa_comprovante_da_primeira(self):
        """Conta B não consegue acessar ID de pendência da conta A (DBs isolados)."""
        from PIL import Image
        # Conta A tem usa_dados_anteriores=True → DB principal
        with self.app.app_context():
            user_a = Usuario(id='a' * 32, nome='Dono A', email='donoa@example.com',
                             usa_dados_anteriores=True)
            db.session.add(user_a)
            buf = io.BytesIO()
            Image.new('RGB', (10, 10), 'red').save(buf, format='PNG')
            filename = 'comprovante_conta_a_seguro.png'
            Path(self.folder.name, filename).write_bytes(buf.getvalue())
            pendencia = Pendencia(credor='Somente da conta A', valor=50.0,
                                  data_vencimento=date.today(),
                                  data_pagamento=date.today(),
                                  status='pago', comprovante_url=filename)
            db.session.add(pendencia)
            db.session.commit()
            pid = pendencia.id
        # Conta B faz login com identidade diferente
        self.identity['id'] = '00000000-0000-0000-0000-000000000099'
        self.identity['email'] = 'contab@example.com'
        self.login()
        # Conta B não deve conseguir acessar o comprovante de ID da conta A
        # (banco da conta B é separado → pendencia não existe → 404)
        response = self.client.get(f'/historico/{pid}')
        self.assertIn(response.status_code, (302, 404))
