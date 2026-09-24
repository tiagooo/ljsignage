# lj-signage — gestão central dos vídeos das TVs de frente de loja

Web app interna do Lugar da Jóia (LJ) para consultar e atualizar, a partir de um único painel, os vídeos que passam nas TVs de frente de loja (12 Raspberry Pi). Responsável: Tiago Magalhães (Diretor de Marketing).

Especificação completa: @docs/SPEC.md

## Restrição principal (não negociável)

- Os Raspberry Pi das lojas **não podem ser alterados**: sem SSH, sem instalar software, sem gravar imagens, sem mudar o script de reprodução.
- A única interface com cada Pi é **FTP**, sobre a pasta de vídeos lida pelo script documentado em `docs/player-script.sh`.
- Se uma funcionalidade exigir mais do que FTP, fica fora de âmbito: assinala-a, não a contornes.

## Contexto técnico

- Hardware e software antigos: alguns Pi 1, Raspbian desde 2014 (Wheezy), omxplayer, servidor FTP do próprio Pi.
- O login FTP entra em `/home/tmagalhaes`, mas a pasta de vídeos **pode variar de Pi para Pi** → descoberta por dispositivo (SPEC §7).
- Hostnames `raspservidor<sigla>.lugardajoia.internal`, listados em `config/devices.example.yaml`.
- Utilizadores: equipa de marketing (Design Gráfico, Visual Merchandising, Gestão de Conteúdo Online) — perfil não técnico.

## Stack

- Python 3.12, **Flask 3** (app factory `create_app()` + blueprints), servido por gunicorn.
- SQLAlchemy 2 via Flask-SQLAlchemy, migrações com Flask-Migrate (Alembic). SQLite em modo WAL com `busy_timeout`.
- Frontend server-side: Jinja2 + HTMX (sem build de frontend, sem SPA). CSRF com Flask-WTF.
- Autenticação: Flask-Login + Authlib (Google OAuth, domínio `lugardajoia.com`).
- Processo `worker` separado: APScheduler (reconciliação, backups) e fila de conversões guardada na base de dados (sem Redis).
- FTP: `ftplib` (modo passivo). Media: `ffmpeg` / `ffprobe`.
- Testes: `pytest` + `pyftpdlib` (servidor FTP simulado). Lint e formatação: `ruff`.
- Deploy: docker compose no host `docker-server`, dois serviços (`web`, `worker`) da mesma imagem, volume partilhado `/data`.

## Porquê dois processos

Com gunicorn há vários workers web. Se o agendador corresse dentro deles, cada worker lançaria a sua própria reconciliação e haveria operações FTP duplicadas. O agendador e as conversões correm **apenas** no serviço `worker`. O serviço `web` só lê e escreve na base de dados e cria tarefas.

## Estrutura prevista

```
lj_signage/
  __init__.py          # create_app()
  config.py            # settings (.env) + leitura de config/devices.yaml
  extensions.py        # db, migrate, login_manager, csrf, oauth
  models.py
  ftp/client.py        # wrapper ftplib: timeouts, retries, listagem incl. ocultos
  discovery.py         # descoberta read-only da pasta de vídeos
  media/transcode.py   # perfil ffmpeg compatível com omxplayer/Pi 1
  reconciler/planner.py   # FUNÇÃO PURA: estado pretendido + estado atual -> ações
  reconciler/executor.py  # aplica as ações por FTP (respeita DRY_RUN)
  worker.py            # entrypoint do serviço worker (scheduler + fila)
  cli.py               # comandos flask (ver abaixo)
  blueprints/          # dashboard, devices, library, schedule, plan, activity, auth, api
  templates/  static/
tests/
  player_sim.py        # simulador da regra do script ($VIDEOPATH/*) para testes
config/devices.yaml    # fonte única dos dispositivos
docs/SPEC.md  docs/player-script.sh
```

## Comandos (a criar na Fase 0)

- `docker compose up -d --build` — arrancar `web` + `worker`
- `flask --app lj_signage run --debug` — desenvolvimento local
- `flask devices sync` — carregar `config/devices.yaml` para a base de dados
- `flask discover <code>` — descoberta read-only da pasta de vídeos de um Pi
- `flask plan <code|all>` — mostrar o plano de ações sem executar
- `flask reconcile <code> --execute` — executar (recusa se `DRY_RUN=true` ou `enabled=false`)
- `pytest` · `ruff check . && ruff format .`

## Regras de trabalho

1. **Nunca escrever num Pi real sem confirmação explícita do Tiago.** Por omissão `DRY_RUN=true`: o reconciliador calcula e mostra o plano, mas não executa.
2. Testes que escrevem por FTP correm apenas contra o servidor simulado (`pyftpdlib`), nunca contra hostnames reais.
3. A escrita real começa num único Pi piloto (`enabled: true` só nesse dispositivo).
4. O planner é uma função pura e tem de ter testes às invariantes (abaixo), incluindo com `tests/player_sim.py`, antes de existir executor.
5. Credenciais apenas em `.env` (gitignored). Nunca em código, commits, logs ou mensagens de erro.
6. `config/devices.yaml` é a fonte única dos dispositivos: não duplicar hostnames, siglas ou caminhos no código.
7. Datas guardadas em UTC e com fuso; apresentadas em `Europe/Lisbon`. Testar a mudança de hora (ex.: 25/10/2026).
8. Código, identificadores e mensagens de commit em inglês. Interface, documentação e textos para o utilizador em **português europeu** (datas dd/mm/aaaa, 24 h).
9. Trabalhar por fases (SPEC §12). No fim de cada fase: parar, resumir o que foi feito, o que foi testado e o que falta validar em equipamento real.
10. Perante ambiguidade, perguntar em vez de assumir. Pontos em aberto: SPEC §14.
11. Dependências mínimas e estáveis. Justificar qualquer dependência nova.

## Invariantes do conteúdo no Pi (cobrir com testes)

- A pasta de vídeos de um Pi **nunca fica sem pelo menos um vídeo ativo** (pasta vazia = ecrã preto e ciclo contínuo de erro).
- Dentro da pasta de vídeos só existem ficheiros visíveis que sejam vídeos **completos e verificados**. Nada de subpastas visíveis, ficheiros temporários visíveis ou outros ficheiros.
- Ficheiros em transferência ou em preparação ficam sempre ocultos (nome começado por `.`).
- Nomes ativos seguem `^[0-9]{3}_[a-z0-9-]+\.mp4$` (sem espaços, sem acentos — o script não usa aspas).
- A app nunca apaga ficheiros que não criou, exceto por ação explícita de um utilizador no painel.
- Todas as operações do reconciliador são idempotentes: repetir um ciclo não produz efeitos adicionais.

## Identidade visual

- Tipografia Montserrat. Roxo principal `#9578D3`; tons claros `#D9D2E9` e `#EFEBF7`.
- Interface sóbria, legível e responsiva, pensada para quem não é técnico: estados claros (online, offline, erro) e linguagem simples.
