# Meu Financeiro

Aplicação web de finanças pessoais construída com **Python e Flask**, com interface responsiva, temas claro/escuro e recursos de **PWA**. Projeto de portfólio de Rafael Ortencio da Silva.

## Funcionalidades

- Contas a pagar, pagamentos, parcelas, recorrência e cartões de crédito.
- Comprovantes em imagem ou PDF, com leitura de dados por OCR.
- Importação de extratos Nubank em CSV/PDF e conciliação de movimentações.
- Metas de economia, aportes e sugestões de contribuição.
- Dashboard com gráficos, categorias, relatórios e fechamento mensal.
- Login por e-mail/senha e Google, com integração opcional ao Supabase Auth.
- Separação dos dados e anexos por conta de usuário.
- Instalação como PWA e recebimento de comprovantes pelo menu de compartilhamento em dispositivos compatíveis.
- Formatação de moeda durante a digitação: `1234` → `12,34`.

## Tecnologias

| Camada | Tecnologias |
| --- | --- |
| Aplicação | Python 3.13, Flask, Jinja, SQLAlchemy, SQLite |
| Interface | Bootstrap, JavaScript, Chart.js |
| Autenticação | Supabase Auth ou autenticação local, OAuth Google |
| Documentos | Tesseract OCR, Pillow, pdfplumber, pandas |
| Infraestrutura | Docker Compose, Gunicorn, Redis |

A aplicação usa renderização no servidor com Jinja e JavaScript para interações. Os dados financeiros ficam no SQLite da aplicação; o Supabase é usado para autenticação quando esse provedor está habilitado.

## Executar localmente

Requisitos: Python 3.13 e Tesseract OCR com o idioma português para leitura de imagens. Os testes JavaScript usam Node.js.

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -c requirements-lock.txt -r requirements.txt
Copy-Item .env.example .env
```

O aplicativo lê `.env` automaticamente, sem substituir variáveis já definidas no ambiente. Para iniciar um ambiente de desenvolvimento local com autenticação local:

```powershell
$env:APP_ENV = "development"
$env:AUTH_PROVIDER = "local"
$env:APP_PUBLIC_URL = "http://localhost:5000"
$env:SESSION_COOKIE_SECURE = "0"
$env:RATELIMIT_STORAGE_URI = "memory://"
.\venv\Scripts\python.exe app.py
```

Abra `http://localhost:5000` e crie uma conta. Você também pode preencher essas configurações no `.env` em vez de defini-las no terminal.

## Docker e produção

Copie `.env.example` para `.env` e configure as variáveis antes de executar:

```sh
docker compose up -d --build
```

O Compose usa produção por padrão. São necessários `APP_ENV=production`, `SECRET_KEY` aleatória com pelo menos 32 caracteres, `APP_PUBLIC_URL` HTTPS e `SESSION_COOKIE_SECURE=1`. Configure um proxy HTTPS; o serviço fica disponível na porta 5005. O Redis do Compose fornece o armazenamento compartilhado para os limites de requisições.

Para Supabase, configure `AUTH_PROVIDER=supabase`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` e uma `SUPABASE_TOKEN_ENCRYPTION_KEY` Fernet. Autorize os endereços de retorno no provedor. Não use chaves `service_role`.

Os volumes `instance/` e `uploads/` guardam bancos e anexos. Em instalações existentes, confirme que o usuário do contêiner (UID/GID 10001) tem acesso de leitura e escrita a esses diretórios. Preserve backups antes de atualizar.

Veja [deploy](docs/deploy.md), [Supabase](docs/autenticacao-supabase.md), [Google](docs/autenticacao-google.md) e [validação](docs/validacao.md).

## Testes

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -q
node tests/test_currency.cjs
node tests/test_interface.cjs
node tests/test_pwa_share.cjs
```

A suíte usa bancos em memória e arquivos temporários. Inclui autenticação, isolamento por usuário, configuração de produção, validação de documentos, limites de uso e comportamento da interface.

## Privacidade do repositório

Este repositório contém código e exemplos de configuração. Credenciais, bancos, extratos, comprovantes, cópias de recuperação e registros de sessões são mantidos fora do versionamento. Os exemplos usados nos testes são fictícios.

As configurações de serviços externos e os dados de uma instalação devem permanecer no ambiente privado de quem executa a aplicação.
