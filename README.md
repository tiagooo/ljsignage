# lj-signage — Vídeos das TVs de frente de loja

Painel interno do Lugar da Jóia para ver e atualizar, num só sítio, os vídeos que passam nas TVs de frente de loja (12 Raspberry Pi). A app fala com cada Pi **apenas por FTP** e nunca altera nada no Pi além dos ficheiros da pasta de vídeos. Especificação: [docs/SPEC.md](docs/SPEC.md). Regras de trabalho: [CLAUDE.md](CLAUDE.md).

> **Estado:** MVP (fases F0, F1 e F2 da SPEC §12, mais o executor). Por omissão `DRY_RUN=true`: o sistema lê as lojas e mostra o plano, mas **não escreve nada nos Pis**. A escrita real (F3, piloto) só começa com confirmação explícita do Tiago — ver [Passar ao piloto](#passar-ao-piloto-f3).

## Como funciona

- **web** (gunicorn): o painel. Só lê e escreve na base de dados e cria tarefas; nunca fala FTP.
- **worker**: o único processo que fala com os Pis. A cada `RECONCILE_INTERVAL_MIN` lê cada loja, calcula o plano e (se permitido) aplica-o. Também converte os vídeos (um de cada vez), faz os backups e regista um sinal de vida visível no painel.
- **Planner** (`lj_signage/reconciler/planner.py`): função pura que, a partir da programação e do que está no Pi, decide as ações FTP por uma ordem que nunca deixa a TV sem vídeo, mesmo que uma ação falhe a meio.
- No Pi, os ficheiros em preparação ficam na pasta oculta `.lj-staging` (o leitor não a vê); os vídeos a passar chamam-se `NNN_nome-abc123.mp4`, e o `NNN` define a ordem.

## Instalação no docker-server

1. **Configuração**

   ```sh
   cp .env.example .env                                # preencher (ver abaixo)
   cp config/devices.example.yaml config/devices.yaml  # rever grupos e lojas
   ```

   No `.env`, no mínimo: `SECRET_KEY`, `FTP_USER`, `FTP_PASSWORD`, `ADMIN_EMAILS`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e os DNS internos (`DNS_PRIMARY`, `DNS_SECONDARY`). Se algum Pi tiver credenciais diferentes: `FTP_USER__<SIGLA>` / `FTP_PASSWORD__<SIGLA>` (ex.: `FTP_PASSWORD__UBBO`).
2. **Google OAuth** (Google Cloud Console → APIs e serviços → Credenciais): criar um cliente OAuth do tipo *Aplicação Web*, no ecrã de consentimento escolher *Interno* (só contas da Workspace) e registar o URI de redirecionamento `https://<endereço-do-painel>/auth/callback`.
3. **Arrancar**

   ```sh
   docker compose up -d --build
   docker compose ps          # web "healthy", worker "running"
   ```

   O `web` aplica as migrações e carrega o `devices.yaml` no arranque; o `worker` só arranca depois de o `web` estar saudável.
4. **Acesso**: `http://docker-server:8080` (porta em `WEB_PORT`). Fora da rede interna, só através de proxy com HTTPS e autenticação; nesse caso `SESSION_COOKIE_SECURE=true` e `TRUST_PROXY=true`.
5. **Utilizadores**: os administradores vêm de `ADMIN_EMAILS`. Os editores (Design Gráfico, VM, Conteúdo Online) são autorizados por um administrador em **Utilizadores**. Outras contas @lugardajoia.com são recusadas.

## Primeiros passos (F0: inventário, só leitura)

```sh
docker compose exec web flask devices list        # lojas e estado
docker compose exec web flask discover all        # procura a pasta de vídeos de cada Pi (só leitura)
```

Depois, no painel, em cada loja: **Descobrir pasta** → **Confirmar esta pasta**. A primeira leitura regista os ficheiros existentes como **legados** — nunca são apagados sem pedido explícito de um administrador. A F0 fica aceite quando todas as lojas aparecem no painel com a pasta confirmada.

## Comandos

| Comando | O que faz |
| --- | --- |
| `docker compose up -d --build` | Arranca `web` + `worker` |
| `flask devices sync` | Carrega `config/devices.yaml` para a base de dados |
| `flask devices list` | Lista as lojas e o estado |
| `flask discover <sigla\|all>` | Descoberta (só leitura) da pasta de vídeos |
| `flask plan <sigla\|all> [--offline]` | Mostra o plano sem executar (`--offline`: usa a última leitura, sem FTP) |
| `flask reconcile <sigla> --execute` | Aplica o plano numa loja. Recusa se `DRY_RUN=true` ou `enabled: false`; pede confirmação |
| `flask backup now \| list \| restore <ficheiro>` | Backups da base de dados |
| `pytest` · `ruff check . && ruff format .` | Testes e lint |

No Docker, prefixar com `docker compose exec web` (ex.: `docker compose exec web flask plan all`).

## Desenvolvimento local

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env    # DATA_DIR=./data, FLASK_DEBUG=1, AUTH_DEV_LOGIN=true, SECRET_KEY=qualquer
.venv/bin/flask --app lj_signage db upgrade
.venv/bin/flask --app lj_signage devices sync
.venv/bin/flask --app lj_signage run --debug        # painel em http://127.0.0.1:5000
.venv/bin/python -m lj_signage.worker               # noutro terminal
.venv/bin/python -m pytest
```

Com `FLASK_DEBUG=1` e `AUTH_DEV_LOGIN=true`, o ecrã de entrada permite entrar sem Google (só em desenvolvimento; a app recusa arrancar com `AUTH_DEV_LOGIN=true` fora do modo debug).

Os testes nunca contactam Pis reais: o FTP é simulado com `pyftpdlib` e qualquer ligação fora de `127.0.0.1` faz o teste falhar. Para correr os testes no mesmo ambiente da produção (Python 3.12 + ffmpeg da Debian):

```sh
docker build --target test -t lj-signage:test . && docker run --rm lj-signage:test
```

## Backups e reposição

O worker faz um backup diário da base de dados às 03:30 (API de backup do SQLite) para `/data/backups`, com retenção de 30 dias. Backup manual: `docker compose exec web flask backup now`.

**Repor um backup** (procedimento testado):

```sh
docker compose exec web flask backup list                 # escolher o ficheiro
docker compose stop web worker
docker compose run --rm --no-deps web flask backup restore /data/backups/lj-signage-AAAAMMDD-HHMMSS.db --yes
docker compose start web worker
```

A reposição recusa-se a correr se o worker estiver ativo. Os dados (base de dados, vídeos, miniaturas, backups) estão no volume Docker `lj-signage_lj-data` (`docker volume inspect lj-signage_lj-data`); confirmar que está incluído nos backups do docker-server (SPEC §14.9).

## Passar ao piloto (F3)

Só com confirmação explícita do Tiago (CLAUDE.md, regra 1):

1. Escolher o Pi piloto e confirmar a sua pasta no painel.
2. No `config/devices.yaml`, `enabled: true` **só** nesse dispositivo; `docker compose exec web flask devices sync`.
3. Rever o plano: `docker compose exec web flask plan <sigla>` e a página **Plano**.
4. No `.env`, `DRY_RUN=false`; `docker compose up -d` (recria os serviços com o novo `.env`).
5. Validar na TV: entrada de vídeo novo, ativação agendada, fim de campanha, reordenação e reserva.
6. Confirmar no servidor FTP desse Pi (o banner aparece na página da loja): listagem de ocultos e renomeação entre pastas (`.lj-staging` → pasta de vídeos).

Com `DRY_RUN=false`, as lojas com `enabled: false` continuam só em leitura.

## Decisões de implementação

- **Ordem com poucas renomeações.** O `NNN` é escolhido para manter o maior número possível de nomes atuais (cada renomeação faz o leitor saltar esse vídeo uma volta). Numa loja nova fica 010, 020, 030…; inserir no topo usa, por exemplo, 005 sem mexer nos outros.
- **Vídeo de reserva sempre pronto.** Fica guardado (oculto) em `.lj-staging` enquanto há campanhas; quando a última termina, a reserva é ativada **antes** de a campanha sair. Quando voltam campanhas, a reserva regressa à preparação em vez de ser apagada.
- **Nunca deixar a TV sem vídeo.** Se, depois das alterações, nada ficasse a passar (sem programação e sem reserva), não se retira nenhum vídeo e o painel avisa.
- **Espaço (`max_bytes`).** Quando definido, o que tem de passar já é enviado primeiro; o que só cabe depois de sair o conteúdo antigo é enviado a seguir às remoções (e só se algo continuar a passar); a preparação antecipada vem por último.
- **Legados.** Nunca são apagados sem pedido de um administrador; o pedido só é executado se continuar a haver um vídeo reproduzível. O pedido vale para o ficheiro que o administrador viu: é anulado se o ficheiro mudar no Pi (tamanho ou data) ou se a pasta da loja mudar, e é confirmado de novo (pedido ainda ativo, mesmo tamanho) imediatamente antes de apagar. A app não apaga pastas. "Adotar" descarrega o ficheiro (leitura) e converte-o para a biblioteca.
- **Nomes.** Um ficheiro legado que tenha exatamente o nome que a app usaria não bloqueia o vídeo: é usado outro prefixo na mesma posição da ordem.
- **Nada é substituído.** Antes de cada renomeação confirma-se que o destino não existe; cada envio é verificado (`SIZE`) e o ficheiro local é verificado por SHA-256 antes de sair.
- **Escrita ativa só por configuração.** `DRY_RUN` (no `.env`) e `enabled` (no `devices.yaml`) não se alteram no painel, de propósito; o painel mostra o modo de cada loja. Uma loja retirada do `devices.yaml` nunca é escrita.
- **Uma sincronização de cada vez por loja.** Um lock na base de dados (renovado durante os envios longos) garante que o worker e a linha de comandos nunca escrevem no mesmo Pi em simultâneo; quem perde o lock para de imediato. Uma loja lenta não atrasa as outras.
- **Mudança de hora.** Horas escritas no painel são de Portugal continental. Uma hora que não existe (fim de março) é recusada; uma hora repetida (25/10/2026, 01:00–02:00) usa a primeira ocorrência (hora de verão) e o painel avisa.
- **Perfil de vídeo.** Sem som por omissão (`AUDIO_ENABLED=false`); fps de origem até 30, acima disso 25; fontes entrelaçadas são desentrelaçadas.
- **`config/devices.yaml`** está no `.gitignore` (tal como o `.env`), por ser configuração de cada instalação. Se preferirem versioná-lo, basta retirar a linha.

## Limitações conhecidas

- "Online" significa apenas que o FTP responde: não é possível saber que vídeo está a passar nem se o omxplayer está a funcionar (SPEC §13).
- A entrada no ar tem um atraso de até um ciclo (`RECONCILE_INTERVAL_MIN`) mais uma volta do carrossel.
- O FTP transmite credenciais sem encriptação: usar só na rede interna.
- Pastas de vídeo fora do alcance do FTP (por exemplo, com FTP em chroot e `VIDEOPATH` fora da pasta de login) ficam fora de âmbito: a descoberta assinala-as.
- A duração dos ficheiros legados é desconhecida (conta como "≥" na duração da volta).
- A idade dos envios incompletos (`.part`) vem do relógio do Pi: se estiver muito errado, a limpeza dos `.part` com mais de 24 h pode não acontecer (não bloqueia envios novos).

## Pontos em aberto (SPEC §14)

| # | Ponto | Como ficou no MVP |
| --- | --- | --- |
| 1 | Credenciais FTP iguais em todos os Pis? | Credencial comum no `.env`, com sobreposição por loja |
| 2 | Servidor FTP dos Pis | Suporta MLSD e `LIST -a`, e deteta a pasta oculta com `CWD` (funciona mesmo com vsftpd). Validar `RNFR/RNTO` entre pastas no piloto |
| 3 | Capacidade dos cartões SD | `max_bytes` por loja no `devices.yaml` (opcional) |
| 4 | As TVs passam som? | Som desligado por omissão (`AUDIO_ENABLED`) |
| 5 | Resolução das TVs | 1080p |
| 6 | Autenticação | Google OAuth @lugardajoia.com, só contas autorizadas |
| 7 | Grupos de lojas | Os do exemplo; editar no `devices.yaml` |
| 8 | Pi piloto | Por decidir |
| 9 | `/data` nos backups do docker-server? | Por confirmar (volume `lj-signage_lj-data`) |
| 10 | DNS internos | `10.0.2.2` e `10.0.2.20` por omissão (`DNS_PRIMARY`, `DNS_SECONDARY`) |

## Estrutura

```text
lj_signage/
  __init__.py            create_app()
  config.py              .env + config/devices.yaml (validação)
  models.py              modelos (datas sempre em UTC)
  ftp/client.py          FTP passivo, timeouts, só leitura por omissão
  discovery.py           descoberta da pasta de vídeos (só leitura)
  media/transcode.py     perfil ffmpeg para Pi 1 + omxplayer
  reconciler/planner.py  função pura: programação + estado do Pi -> ações
  reconciler/executor.py leitura do Pi e aplicação das ações por FTP
  reconciler/cycle.py    um ciclo por loja (lock, estado, auditoria)
  reconciler/state.py    ponte base de dados <-> planner
  worker.py              serviço worker (agendador + fila de tarefas)
  cli.py                 comandos flask
  blueprints/            painel, lojas, biblioteca, programação, plano, atividade, auth, api
tests/
  player_sim.py          simulador do script dos Pis ($VIDEOPATH/*)
  test_planner*.py       invariantes do planner (inclui centenas de cenários aleatórios)
  test_executor.py       executor real vs. simulador, sobre FTP simulado
```

## Dependências

Todas fixadas em `requirements.txt` / `requirements-dev.txt`.

| Dependência | Porquê |
| --- | --- |
| Flask, Flask-SQLAlchemy, SQLAlchemy, Flask-Migrate (Alembic), Flask-WTF (WTForms), Flask-Login, Authlib, APScheduler 3, gunicorn, pytest, pyftpdlib, ruff | Stack definida no CLAUDE.md |
| requests | Usado pelo cliente OAuth do Authlib |
| PyYAML | Leitura do `config/devices.yaml` |
| python-dotenv | Carrega o `.env` ao correr `flask`/worker localmente (no Docker é o compose que o faz) |
| htmx 2.0.11, fonte Montserrat | Servidos localmente (`lj_signage/static/vendor`, `static/fonts`), sem CDN |

Imagem Docker: `python:3.12-slim-trixie` + `ffmpeg` da Debian.
