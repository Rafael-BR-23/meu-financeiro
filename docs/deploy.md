# Deploy para ZimaOS

O deploy envia todos os módulos usados pelo aplicativo (incluindo autenticacao.py), requirements.txt, Dockerfile, .dockerignore, docker-compose.yml, .env.example e as pastas templates e static completas, incluindo subpastas. O destino é ZIMAOS_DIR, configurado no .env local.

Instale a dependência no computador que executa o deploy:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-deploy.txt
```

## Primeira transferência das contas locais

Para levar as contas cadastradas neste computador ao servidor que ainda não possui instance/usuarios.db:

```powershell
.\venv\Scripts\python.exe deploy.py --migrar-contas
```

Também é possível executar `deploy.bat --migrar-contas` no terminal. Durante a transferência, não crie contas nem altere lançamentos no aplicativo local.

Essa opção envia snapshots SQLite dos usuários e dos bancos em instance/contas e envia anexos que ainda não existem no servidor. Inclui os registros gravados no WAL do SQLite. Se o servidor não tiver financeiro.db, envia também esse banco. Se o servidor já tiver financeiro.db, mantém seu histórico; as contas locais associadas ao histórico anterior passam a acessar o banco financeiro que já está no servidor. Anexos com o mesmo caminho no servidor são preservados.

A migração é recusada se usuarios.db já existir no servidor. Não remova esse arquivo para forçar a operação: contas de dois ambientes precisam de uma migração específica.

## Atualizações seguintes

```powershell
.\venv\Scripts\python.exe deploy.py
```

O deploy normal atualiza somente código e recursos. Não envia nem substitui bancos, contas, comprovantes ou chaves de sessão. Os volumes ./instance e ./uploads preservam usuários, bancos por conta, histórico, chave de sessão e anexos ao reconstruir o contêiner. A chave é gerada no servidor se necessária; sessões do ambiente local não são transportadas e o primeiro acesso ao servidor exige login.

## Configuração do servidor

O .env local contém credenciais SSH e não é enviado. O .env.example é enviado como referência: configure o .env do próprio servidor em ZIMAOS_DIR. Para Google, preencha GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET e GOOGLE_REDIRECT_URI com a URL HTTPS do servidor seguida de /auth/google/callback; localhost é apenas para testes no computador. Configure SESSION_COOKIE_SECURE=1 quando acessar o servidor por HTTPS.

O script cria instance/contas e uploads/contas, transfere os arquivos e executa Docker Compose com rebuild. Uma falha retorna código de erro, sem anunciar sucesso. A senha de sudo é enviada pela entrada do processo, fora do texto do comando remoto.
