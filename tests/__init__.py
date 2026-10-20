"""Select the disposable database before any test imports the application."""
import os

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['AUTH_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['AUTH_PROVIDER'] = 'local'


def cliente_autenticado(app):
    """Cliente dos testes financeiros, vinculado apenas ao banco descartável."""
    from models import db, Usuario
    from autenticacao import versao_senha
    with app.app_context():
        assert all(engine.url.database == ':memory:' for engine in db.engines.values()), 'Tests require disposable databases'
        db.create_all(bind_key='auth')
        user = db.session.get(Usuario, 'usuario-testes')
        if user is None:
            user = Usuario(id='usuario-testes', nome='Teste', email='teste@example.com', usa_dados_anteriores=True)
            db.session.add(user)
            db.session.commit()
        version = versao_senha(user)
    client = app.test_client()
    with client.session_transaction() as session:
        session['user'] = {'id': 'usuario-testes', 'email': 'teste@example.com'}
        session['password_version'] = version
    return client
