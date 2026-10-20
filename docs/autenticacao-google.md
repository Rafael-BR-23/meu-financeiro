# Contas e autenticação

O site permite criar uma conta com nome, e-mail e senha, entrar com essa senha ou continuar com Google. A senha é armazenada como hash scrypt. A sessão fica em cookie HttpOnly e SameSite=Lax por 30 dias, renovados durante o uso. Limpar cookies, usar navegação privada, ficar 30 dias sem acessar ou clicar em Sair exige novo login.

## Configurar Google

1. No Google Cloud Console, configure a tela de consentimento e crie um cliente OAuth do tipo Aplicativo da Web. Em modo de teste, inclua as contas nos usuários de teste.
2. Cadastre a URL exata do site seguida de `/auth/google/callback` em URIs de redirecionamento autorizados. No desenvolvimento: `http://localhost:5000/auth/google/callback`. Em produção use domínio HTTPS, pois o Google não aceita IP privado HTTP como URL de produção.
3. Preencha `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e `GOOGLE_REDIRECT_URI` no `.env`. Use `SESSION_COOKIE_SECURE=1` com HTTPS; `0` somente no desenvolvimento HTTP.
4. Instale `pip install -r requirements.txt` e reinicie o aplicativo. Com Docker Compose, reconstrua o contêiner.

Sem essas credenciais, cadastro e login com senha funcionam e o botão Google fica desabilitado. A primeira entrada com Google cria uma conta já vinculada e abre a tela para definir a senha do site. Se o mesmo e-mail já foi cadastrado com senha, o login pelo Google vincula e acessa automaticamente a conta existente, preservando a senha e os dados. O e-mail precisa estar verificado pelo Google; uma identidade Google diferente da já vinculada é recusada. A opção Vincular Google continua disponível.

## Dados de cada conta

Os usuários ficam em `instance/usuarios.db`. Cada conta tem seu banco SQLite em `instance/contas/<id>.db` e seus anexos em `uploads/contas/<id>`. As consultas e alterações financeiras usam o banco da conta autenticada, inclusive gráficos, relatórios, importações, categorias e comprovantes. Preserve as pastas instance e uploads nos backups.

O banco anterior `instance/financeiro.db` e seus anexos continuam preservados. Ele não pertence automaticamente a novos cadastros. Para associá-lo à sua conta, crie sua conta antes de cadastrar novos lançamentos e execute no console local:

```powershell
.\venv\Scripts\python.exe -m flask --app app associar-dados-antigos seu-email@exemplo.com
```

O comando exige uma conta existente, recusa contas com lançamentos novos e impede atribuir o mesmo histórico a uma segunda conta. A associação não apaga nem copia registros: a conta passa a usar o banco e os anexos anteriores.

## Sessão e limite de tentativas

Configure uma SECRET_KEY aleatória de pelo menos 32 caracteres. Se ausente ou reconhecidamente insegura, o aplicativo gera `instance/session.key`; preserve esse arquivo privado nos reinícios. Trocar a chave encerra sessões existentes. Senhas e tokens Google não são guardados no localStorage/cache. O service worker armazena apenas recursos estáticos.

Login aceita até 10 tentativas por minuto por IP; cadastro aceita 5. O armazenamento padrão desses limites é em memória, por processo e reinicia junto com ele. Para limites compartilhados entre vários processos, configure `RATELIMIT_STORAGE_URI` com um backend suportado pelo Flask-Limiter e instale o driver correspondente.

O compartilhamento de comprovantes exige sessão ativa. Se expirada, entre e compartilhe novamente.

Referências: [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect), [Authlib Flask](https://docs.authlib.org/en/latest/oauth2/client/web/flask.html), [Flask-SQLAlchemy](https://flask-sqlalchemy.palletsprojects.com/en/stable/customizing/), [Flask-Limiter](https://flask-limiter.readthedocs.io/en/stable/configuration.html).

## Recuperação de senha pelo Gmail

A tela de login contém Esqueci minha senha. O usuário informa seu e-mail e recebe um link válido por 30 minutos. Após redefinir a senha, o link não pode ser reutilizado e as sessões anteriores deixam de funcionar. Os dados financeiros e o vínculo com Google são preservados.

Configure SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_SSL=0, SMTP_USER e SMTP_FROM com o Gmail remetente. Em SMTP_PASSWORD use uma senha de app da conta Google, não a senha normal nem o segredo OAuth. Ative a verificação em duas etapas e crie a senha em https://myaccount.google.com/apppasswords. Coloque-a diretamente no .env e reinicie o aplicativo. Confira a disponibilidade de senhas de app em https://support.google.com/accounts/answer/185833?hl=pt-BR.

APP_PUBLIC_URL deve ser o endereço usado para abrir o site: http://localhost:5000 em testes, ou a URL HTTPS real do ZimaOS em produção. O link é montado a partir dessa configuração, sem usar o Host enviado pelo navegador. As variáveis SMTP e APP_PUBLIC_URL também precisam estar no .env do servidor; o deploy não envia o .env local.

Sem SMTP configurado, a tela informa que a recuperação por e-mail está indisponível. As credenciais de login Google não enviam e-mail. O fluxo de recuperação foi testado com envio simulado; a entrega real depende da configuração do Gmail.

## Configurações da conta

A página /configuracoes fica no menu Configurações do computador e em Mais → Configurações no celular/PWA. A opção Vincular Google foi movida da barra superior para essa página; contas já vinculadas mostram seu estado atual. A troca de senha exige a senha atual, uma nova senha de 8 a 128 caracteres e a confirmação. Ao trocar a senha, a sessão atual é mantida, as demais são invalidadas e os dados financeiros permanecem associados à mesma conta.
