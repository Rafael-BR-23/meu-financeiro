# Autenticação pelo Supabase

A aplicação suporta Supabase Auth mantendo os bancos financeiros SQLite e comprovantes existentes. A tabela local usuario continua identificando a conta financeira. O primeiro login Supabase com e-mail confirmado vincula a identidade remota à conta local existente pelo mesmo e-mail, preservando id, pasta de anexos e usa_dados_anteriores. Novas contas recebem um banco separado.

## Ativar

No painel do projeto, em Settings → API Keys, copie a Publishable key (ou a chave anon legada). Não use a chave secreta/service_role nem a conexão PostgreSQL.

Preencha o .env local:

```dotenv
AUTH_PROVIDER=supabase
SUPABASE_URL=https://SEU_PROJETO.supabase.co
SUPABASE_PUBLISHABLE_KEY=cole_a_chave_publicavel
SUPABASE_GOOGLE_ENABLED=1
APP_PUBLIC_URL=http://localhost:5000
```

Depois reinicie o Flask. O deploy envia o novo módulo autenticacao_supabase.py e o Compose recebe essas variáveis a partir do .env do servidor. O .env local não é enviado pelo deploy. Para ZimaOS, configure APP_PUBLIC_URL com o endereço HTTPS real e SESSION_COOKIE_SECURE=1.

A autenticação anterior segue disponível com AUTH_PROVIDER=local enquanto a configuração Supabase está sendo preparada. Com AUTH_PROVIDER=supabase, a aplicação exige URL/chave válidas e não usa a senha local como alternativa.

## Painel Supabase

1. Authentication → URL Configuration: configure Site URL como http://localhost:5000 no desenvolvimento (URL HTTPS do ZimaOS em produção).
2. Em Redirect URLs, autorize http://localhost:5000/auth/supabase/callback e a mesma rota no domínio HTTPS real do servidor.
3. Authentication → Sign In / Providers: habilite Email e Google.
4. No provedor Google, coloque o Client ID e Client Secret OAuth. No Google Cloud, cadastre o retorno **https://SEU_PROJETO.supabase.co/auth/v1/callback**. Esse é o retorno do Google para o Supabase; /auth/supabase/callback é o retorno do Supabase para nosso site.
5. Configure SMTP próprio em Authentication para enviar confirmação e recuperação aos usuários. O SMTP padrão do Supabase tem restrições; SMTP_USER/SMTP_PASSWORD no .env do Flask não configuram o SMTP do Supabase.

## Senhas e migração

Os hashes Werkzeug anteriores não são importados para o Supabase. Contas existentes devem entrar pelo Google com o mesmo e-mail confirmado e definir uma senha no Supabase, ou cadastrar o mesmo e-mail no Supabase, confirmá-lo e entrar com a senha nova. O vínculo mantém todos os dados financeiros da conta local. Recuperação funciona para usuários já existentes no Supabase; ela não cria automaticamente uma identidade remota para contas apenas locais.

Usuários novos pelo Google definem uma senha ao entrar pela primeira vez. Usuários de e-mail/senha confirmam o e-mail quando essa opção está ativada no painel. Google e e-mail são associados conforme as regras de vinculação do Supabase.

## Sessões e recuperação

OAuth, confirmação e recuperação usam PKCE. Abra o link de confirmação/recuperação no mesmo navegador em que iniciou o processo. O link de recuperação precisa ser concluído em até 30 minutos no aplicativo; depois da confirmação, a tela de senha também expira em 30 minutos. Configure o prazo dos e-mails no painel conforme sua política.

Tokens de acesso e renovação ficam somente na tabela local sessao_supabase em instance/usuarios.db. O cookie HttpOnly contém um identificador aleatório de sessão, nunca esses tokens. Preserve esse banco, trate seus backups como privados e mantenha-o fora da imagem Docker e do Git. Não existem tabelas financeiras expostas pela API Postgres do Supabase nesta alteração.

O servidor renova tokens próximos do vencimento e consulta /user no Supabase para validar a identidade em cada requisição autenticada. Alterar senha revoga as outras sessões locais e pede o encerramento das demais sessões remotas. Falhas temporárias do serviço bloqueiam o acesso sem apagar a sessão ou os dados. O prazo local sem acesso permanece em 30 dias; políticas mais restritivas do projeto Supabase podem exigir login antes disso.

## Validação

Os testes test_supabase_auth cobrem login, e-mail não confirmado, associação ao histórico anterior, PKCE, criação de senha, refresh, recuperação, troca de senha, CSRF e indisponibilidade. Não enviam e-mails ou criam contas no projeto real. A ativação real exige verificar as configurações no painel e testar um login com sua conta.

Referências: [Auth REST API](https://github.com/supabase/auth/blob/master/openapi.yaml), [Google](https://supabase.com/docs/guides/auth/social-login/auth-google), [Senhas](https://supabase.com/docs/guides/auth/passwords), [SMTP](https://supabase.com/docs/guides/auth/auth-smtp), [Chaves](https://supabase.com/docs/guides/api/api-keys).
